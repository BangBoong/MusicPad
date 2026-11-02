"""Shared memory bridge between MusicPad (producer) and MusicPadAPO (consumer in audiodg.exe).

Binary layout matches ``native/include/musicpad_shm.h`` byte-for-byte.
Default file path: ``C:\\ProgramData\\MusicPad\\injection.bin``.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import mmap
import os
from pathlib import Path
import struct
import time
from typing import Optional
import numpy as np

MPAD_SHM_DEFAULT_PATH = Path(r"C:\ProgramData\MusicPad\injection.bin")

MPAD_SHM_MAGIC = 0x4441504D  # 'MPAD'
MPAD_SHM_VERSION = 1
MPAD_APO_BUILD_VERSION = 0x00010000

MPAD_FLAG_APP_ACTIVE = 1 << 0
MPAD_FLAG_MIC_MUTE = 1 << 1
MPAD_FLAG_PASSTHROUGH = 1 << 2
MPAD_FLAG_TRACK_PLAYING = 1 << 3

MPAD_FX_NONE = 0
MPAD_FX_HPF = 1 << 0
MPAD_FX_NR = 1 << 1
MPAD_FX_AGC2 = 1 << 2

MPAD_HEARTBEAT_TIMEOUT_NS = 300_000_000  # 300 ms
MPAD_WRITER_LEASE_TIMEOUT_NS = 600_000_000  # 600 ms
MPAD_RING_CAPACITY_SAMPLES = 96_000
MPAD_HEADER_SIZE_BYTES = 512
MPAD_SHM_TOTAL_BYTES = MPAD_HEADER_SIZE_BYTES + MPAD_RING_CAPACITY_SAMPLES * 4

# Field byte offsets inside MusicPadShmHeader (512 bytes)
OFF_MAGIC = 0
OFF_PROTOCOL_VERSION = 4
OFF_APO_BUILD_VERSION = 8
OFF_ACTIVE_FLAGS = 12
OFF_WRITER_PID = 16
OFF_FLUSH_SEQ = 20
OFF_APP_HEARTBEAT_NS = 24
OFF_APO_HEARTBEAT_NS = 32
OFF_APO_SAMPLE_RATE = 40
OFF_APO_CHANNELS = 44
OFF_MIC_GAIN = 48
OFF_VOICE_FX_FLAGS = 52
OFF_VOICE_FX_PARAM1 = 56
OFF_VOICE_FX_PARAM2 = 60
OFF_UNDERRUN_COUNT = 64
OFF_TARGET_ENDPOINT_GUID = 72  # 80 bytes (40 uint16_t)
OFF_CAPACITY_SAMPLES = 152
OFF_WRITE_SAMPLE_IDX = 160
OFF_READ_SAMPLE_IDX = 168
OFF_APO_PROCESSED_BLOCKS = 176
OFF_SAMPLES = 512


def qpc_now_ns() -> int:
    """Return monotonic timestamp in nanoseconds matching Win32 QueryPerformanceCounter."""
    if os.name == "nt":
        freq = ctypes.c_int64()
        counter = ctypes.c_int64()
        if (
            ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
            and freq.value > 0
            and ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(counter))
        ):
            seconds = counter.value // freq.value
            rem = counter.value % freq.value
            return int(seconds * 1_000_000_000 + (rem * 1_000_000_000) // freq.value)
    return time.monotonic_ns()


def _is_pid_running(pid: int) -> bool:
    """Check if a Windows process with the given PID is still alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class ApoRuntimeStatus:
    """Live telemetry reported by MusicPadAPO from inside audiodg.exe."""

    is_alive: bool
    protocol_version: int
    apo_build_version: int
    sample_rate: int
    channels: int
    underrun_count: int
    processed_blocks: int
    apo_heartbeat_age_ms: float


class InjectionShm:
    """Manages the memory-mapped shared memory file for microphone injection."""

    def __init__(self, path: Optional[Path | str] = None, pid: Optional[int] = None) -> None:
        self.path = Path(path) if path is not None else MPAD_SHM_DEFAULT_PATH
        self.pid = int(pid if pid is not None else os.getpid())
        self._file = None
        self._mmap: Optional[mmap.mmap] = None
        self._owns_lease = False

    @property
    def is_open(self) -> bool:
        return self._mmap is not None

    @property
    def owns_lease(self) -> bool:
        return self._owns_lease

    @property
    def has_lease(self) -> bool:
        return self._owns_lease

    def open(self, acquire_lease: bool = True) -> bool:
        """Open or create the shared memory file and optionally acquire the single-writer lease."""
        if self._mmap is not None:
            if acquire_lease and not self._owns_lease:
                return self.acquire_writer_lease()
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "r+b" if self.path.exists() else "w+b")
        f.seek(0, os.SEEK_END)
        if f.tell() < MPAD_SHM_TOTAL_BYTES:
            f.truncate(MPAD_SHM_TOTAL_BYTES)
            f.flush()

        mm = mmap.mmap(f.fileno(), MPAD_SHM_TOTAL_BYTES, access=mmap.ACCESS_WRITE)
        self._file = f
        self._mmap = mm

        magic = struct.unpack_from("<I", mm, OFF_MAGIC)[0]
        if magic != MPAD_SHM_MAGIC:
            mm[:MPAD_HEADER_SIZE_BYTES] = b"\x00" * MPAD_HEADER_SIZE_BYTES
            struct.pack_into("<I", mm, OFF_MAGIC, MPAD_SHM_MAGIC)
            struct.pack_into("<I", mm, OFF_PROTOCOL_VERSION, MPAD_SHM_VERSION)
            struct.pack_into("<f", mm, OFF_MIC_GAIN, 1.0)
            struct.pack_into("<I", mm, OFF_CAPACITY_SAMPLES, MPAD_RING_CAPACITY_SAMPLES)

        if acquire_lease:
            return self.acquire_writer_lease()
        return True

    def acquire_writer_lease(self, now_ns: Optional[int] = None) -> bool:
        """Acquire single-writer ownership if no other live MusicPad instance holds it."""
        if self._mmap is None:
            return False
        now = qpc_now_ns() if now_ns is None else int(now_ns)
        current_writer = struct.unpack_from("<I", self._mmap, OFF_WRITER_PID)[0]
        last_hb = struct.unpack_from("<q", self._mmap, OFF_APP_HEARTBEAT_NS)[0]

        if current_writer not in (0, self.pid):
            hb_fresh = 0 <= (now - last_hb) <= MPAD_WRITER_LEASE_TIMEOUT_NS
            if hb_fresh and _is_pid_running(current_writer):
                self._owns_lease = False
                return False

        struct.pack_into("<I", self._mmap, OFF_PROTOCOL_VERSION, MPAD_SHM_VERSION)
        struct.pack_into("<I", self._mmap, OFF_WRITER_PID, self.pid)
        struct.pack_into("<q", self._mmap, OFF_APP_HEARTBEAT_NS, now)
        self._owns_lease = True
        return True

    def release_writer_lease(self) -> None:
        """Deactivate injection and release single-writer ownership cleanly."""
        if self._mmap is not None and self._owns_lease:
            flags = struct.unpack_from("<I", self._mmap, OFF_ACTIVE_FLAGS)[0]
            flags &= ~(MPAD_FLAG_APP_ACTIVE | MPAD_FLAG_TRACK_PLAYING)
            struct.pack_into("<I", self._mmap, OFF_ACTIVE_FLAGS, flags)
            current_writer = struct.unpack_from("<I", self._mmap, OFF_WRITER_PID)[0]
            if current_writer == self.pid:
                struct.pack_into("<I", self._mmap, OFF_WRITER_PID, 0)
        self._owns_lease = False

    def reset_storage(self) -> None:
        """Completely zero-out all audio samples and reset cursors/stats for clean installation."""
        if self._mmap is None:
            return
        # Zero out the sample ring buffer completely
        self._mmap[OFF_SAMPLES:] = b"\x00" * (MPAD_RING_CAPACITY_SAMPLES * 4)
        # Reset header cursors and telemetry
        struct.pack_into("<I", self._mmap, OFF_MAGIC, MPAD_SHM_MAGIC)
        struct.pack_into("<I", self._mmap, OFF_PROTOCOL_VERSION, MPAD_SHM_VERSION)
        struct.pack_into("<I", self._mmap, OFF_APO_BUILD_VERSION, MPAD_APO_BUILD_VERSION)
        struct.pack_into("<I", self._mmap, OFF_ACTIVE_FLAGS, 0)
        struct.pack_into("<I", self._mmap, OFF_WRITER_PID, 0)
        struct.pack_into("<I", self._mmap, OFF_FLUSH_SEQ, 0)
        struct.pack_into("<q", self._mmap, OFF_APP_HEARTBEAT_NS, 0)
        struct.pack_into("<f", self._mmap, OFF_MIC_GAIN, 1.0)
        struct.pack_into("<I", self._mmap, OFF_VOICE_FX_FLAGS, 0)
        struct.pack_into("<f", self._mmap, OFF_VOICE_FX_PARAM1, 0.0)
        struct.pack_into("<f", self._mmap, OFF_VOICE_FX_PARAM2, 0.0)
        struct.pack_into("<Q", self._mmap, OFF_UNDERRUN_COUNT, 0)
        struct.pack_into("<I", self._mmap, OFF_CAPACITY_SAMPLES, MPAD_RING_CAPACITY_SAMPLES)
        struct.pack_into("<Q", self._mmap, OFF_WRITE_SAMPLE_IDX, 0)
        struct.pack_into("<Q", self._mmap, OFF_READ_SAMPLE_IDX, 0)
        struct.pack_into("<Q", self._mmap, OFF_APO_PROCESSED_BLOCKS, 0)

    def close(self) -> None:
        """Release lease and close memory mapping."""
        self.release_writer_lease()
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def touch_heartbeat(self, now_ns: Optional[int] = None) -> None:
        """Update app_heartbeat_ns to keep the APO out of passthrough mode."""
        if self._mmap is None or not self._owns_lease:
            return
        now = qpc_now_ns() if now_ns is None else int(now_ns)
        struct.pack_into("<q", self._mmap, OFF_APP_HEARTBEAT_NS, now)

    def set_state(
        self,
        *,
        app_active: Optional[bool] = None,
        mic_mute: Optional[bool] = None,
        passthrough: Optional[bool] = None,
        track_playing: Optional[bool] = None,
        mic_gain: Optional[float] = None,
        voice_fx_flags: Optional[int] = None,
        target_endpoint_guid: Optional[str] = None,
        now_ns: Optional[int] = None,
    ) -> None:
        """Update control block flags, gain, target endpoint GUID, and heartbeat."""
        if self._mmap is None or not self._owns_lease:
            return

        flags = struct.unpack_from("<I", self._mmap, OFF_ACTIVE_FLAGS)[0]
        if app_active is not None:
            flags = (flags | MPAD_FLAG_APP_ACTIVE) if app_active else (flags & ~MPAD_FLAG_APP_ACTIVE)
        if mic_mute is not None:
            flags = (flags | MPAD_FLAG_MIC_MUTE) if mic_mute else (flags & ~MPAD_FLAG_MIC_MUTE)
        if passthrough is not None:
            flags = (flags | MPAD_FLAG_PASSTHROUGH) if passthrough else (flags & ~MPAD_FLAG_PASSTHROUGH)
        if track_playing is not None:
            flags = (flags | MPAD_FLAG_TRACK_PLAYING) if track_playing else (flags & ~MPAD_FLAG_TRACK_PLAYING)
        struct.pack_into("<I", self._mmap, OFF_ACTIVE_FLAGS, flags)

        if mic_gain is not None:
            clamped_gain = float(max(0.0, min(4.0, mic_gain)))
            struct.pack_into("<f", self._mmap, OFF_MIC_GAIN, clamped_gain)

        if voice_fx_flags is not None:
            struct.pack_into("<I", self._mmap, OFF_VOICE_FX_FLAGS, int(voice_fx_flags) & 0xFFFFFFFF)

        if target_endpoint_guid is not None:
            guid_str = target_endpoint_guid.strip()
            if guid_str and not guid_str.startswith("{"):
                guid_str = "{" + guid_str + "}"
            encoded = guid_str.encode("utf-16-le")[:78]
            padded = encoded.ljust(80, b"\x00")
            self._mmap[OFF_TARGET_ENDPOINT_GUID : OFF_TARGET_ENDPOINT_GUID + 80] = padded

        self.touch_heartbeat(now_ns=now_ns)

    def update_control(
        self,
        *,
        app_active: Optional[bool] = True,
        mic_mute: Optional[bool] = None,
        passthrough: Optional[bool] = None,
        track_playing: Optional[bool] = None,
        mic_gain: Optional[float] = None,
        voice_fx_flags: Optional[int] = None,
        target_endpoint_guid: Optional[str] = None,
        now_ns: Optional[int] = None,
    ) -> None:
        """Alias for set_state used by AudioEngine."""
        self.set_state(
            app_active=app_active,
            mic_mute=mic_mute,
            passthrough=passthrough,
            track_playing=track_playing,
            mic_gain=mic_gain,
            voice_fx_flags=voice_fx_flags,
            target_endpoint_guid=target_endpoint_guid,
            now_ns=now_ns,
        )

    def get_target_endpoint_guid(self) -> str:
        """Read target_endpoint_guid string from the control block."""
        if self._mmap is None:
            return ""
        raw = bytes(self._mmap[OFF_TARGET_ENDPOINT_GUID : OFF_TARGET_ENDPOINT_GUID + 80])
        text = raw.decode("utf-16-le", errors="ignore")
        return text.split("\x00", 1)[0]

    def flush(self) -> None:
        """Signal the APO to immediately discard any buffered samples in the ring buffer."""
        if self._mmap is None or not self._owns_lease:
            return
        seq = struct.unpack_from("<I", self._mmap, OFF_FLUSH_SEQ)[0]
        struct.pack_into("<I", self._mmap, OFF_FLUSH_SEQ, (seq + 1) & 0xFFFFFFFF)

    def available_to_read(self) -> int:
        """Return number of unread float32 samples currently buffered in the ring."""
        if self._mmap is None:
            return 0
        cap = struct.unpack_from("<I", self._mmap, OFF_CAPACITY_SAMPLES)[0] or MPAD_RING_CAPACITY_SAMPLES
        w = struct.unpack_from("<Q", self._mmap, OFF_WRITE_SAMPLE_IDX)[0]
        r = struct.unpack_from("<Q", self._mmap, OFF_READ_SAMPLE_IDX)[0]
        if w <= r:
            return 0
        avail = w - r
        return int(min(avail, cap))

    def available_to_write(self) -> int:
        """Return number of free float32 sample slots in the ring."""
        if self._mmap is None:
            return 0
        cap = struct.unpack_from("<I", self._mmap, OFF_CAPACITY_SAMPLES)[0] or MPAD_RING_CAPACITY_SAMPLES
        used = self.available_to_read()
        return max(0, int(cap) - used)

    def write_samples(self, samples: np.ndarray, now_ns: Optional[int] = None) -> int:
        """Write interleaved float32 samples into the SPSC ring buffer.

        Returns the number of float32 samples written.
        """
        if self._mmap is None or not self._owns_lease:
            return 0

        if not isinstance(samples, np.ndarray):
            samples = np.asarray(samples)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        elif samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        samples = np.clip(samples, -1.0, 1.0)

        flat = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        count = int(flat.size)
        if count == 0:
            self.touch_heartbeat(now_ns=now_ns)
            return 0

        cap = struct.unpack_from("<I", self._mmap, OFF_CAPACITY_SAMPLES)[0] or MPAD_RING_CAPACITY_SAMPLES
        r = struct.unpack_from("<Q", self._mmap, OFF_READ_SAMPLE_IDX)[0]
        w = struct.unpack_from("<Q", self._mmap, OFF_WRITE_SAMPLE_IDX)[0]

        if w < r or (w - r) > cap:
            w = r

        used = int(w - r)
        free_slots = max(0, int(cap) - used)
        to_write = min(count, free_slots)
        if to_write <= 0:
            self.touch_heartbeat(now_ns=now_ns)
            return 0

        write_pos = int(w % cap)
        first_chunk = min(to_write, int(cap) - write_pos)
        second_chunk = to_write - first_chunk

        start1 = OFF_SAMPLES + write_pos * 4
        self._mmap[start1 : start1 + first_chunk * 4] = flat[:first_chunk].tobytes()
        if second_chunk > 0:
            self._mmap[OFF_SAMPLES : OFF_SAMPLES + second_chunk * 4] = flat[first_chunk:to_write].tobytes()

        struct.pack_into("<Q", self._mmap, OFF_WRITE_SAMPLE_IDX, w + to_write)
        self.touch_heartbeat(now_ns=now_ns)
        return to_write

    def get_apo_status(self, now_ns: Optional[int] = None) -> ApoRuntimeStatus:
        """Read live APO telemetry from the shared memory header."""
        if self._mmap is None:
            return ApoRuntimeStatus(
                is_alive=False,
                protocol_version=0,
                apo_build_version=0,
                sample_rate=48000,
                channels=2,
                underrun_count=0,
                processed_blocks=0,
                apo_heartbeat_age_ms=float("inf"),
            )

        now = qpc_now_ns() if now_ns is None else int(now_ns)
        proto_ver = struct.unpack_from("<I", self._mmap, OFF_PROTOCOL_VERSION)[0]
        build_ver = struct.unpack_from("<I", self._mmap, OFF_APO_BUILD_VERSION)[0]
        apo_hb = struct.unpack_from("<q", self._mmap, OFF_APO_HEARTBEAT_NS)[0]
        sr = struct.unpack_from("<I", self._mmap, OFF_APO_SAMPLE_RATE)[0] or 48000
        ch = struct.unpack_from("<I", self._mmap, OFF_APO_CHANNELS)[0] or 2
        underruns = struct.unpack_from("<Q", self._mmap, OFF_UNDERRUN_COUNT)[0]
        blocks = struct.unpack_from("<Q", self._mmap, OFF_APO_PROCESSED_BLOCKS)[0]

        if apo_hb <= 0:
            age_ms = float("inf")
            alive = False
        else:
            age_ms = max(0.0, (now - apo_hb) / 1_000_000.0)
            alive = age_ms <= 1000.0

        return ApoRuntimeStatus(
            is_alive=alive,
            protocol_version=int(proto_ver),
            apo_build_version=int(build_ver),
            sample_rate=int(sr),
            channels=int(ch),
            underrun_count=int(underruns),
            processed_blocks=int(blocks),
            apo_heartbeat_age_ms=float(age_ms),
        )
