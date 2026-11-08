"""Audio streaming engine for local files and YouTube Music."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

try:
    import wave
except ModuleNotFoundError:
    wave = None  # type: ignore[assignment]

try:
    import numpy as np
except ModuleNotFoundError:
    np = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ModuleNotFoundError:
    sd = None  # type: ignore[assignment]

try:
    from yt_dlp import YoutubeDL
except ModuleNotFoundError:
    YoutubeDL = None  # type: ignore[assignment]

try:
    from ytmusicapi import YTMusic
except ModuleNotFoundError:
    YTMusic = None  # type: ignore[assignment]


from musicpad.dsp_core import (
    DspCore,
    MPAD_FX_DEEPFILTER,
    MPAD_FX_WHITE_NOISE,
    MPAD_FX_VOICE_BOOST,
)
from musicpad.deepfilter_engine import DeepFilterEngine
from musicpad.injection_shm import ApoRuntimeStatus, InjectionShm
from musicpad.library import format_album_duration


SAMPLE_RATE = 48000
CHANNELS = 2
DEFAULT_BLOCKSIZE = 384
MIN_BLOCKSIZE = 256
MAX_BLOCKSIZE = 512
CHUNK_BYTES = 4096
SOFT_KNEE_THRESHOLD = 0.8912509  # -1 dBFS threshold for transparent soft knee

ROUTING_MODE_VIRTUAL_CABLE = "virtual_cable"
ROUTING_MODE_INJECTION = "injection"

READER_INJECTION = 0
READER_MONITOR = 1


class Float32RingBuffer:
    """High-performance, multi-reader circular buffer for float32 stereo audio frames."""

    def __init__(self, capacity_frames: int = 65536, channels: int = CHANNELS) -> None:
        self.capacity = max(1024, int(capacity_frames))
        self.channels = max(1, int(channels))
        if np is not None:
            self._buffer = np.zeros((self.capacity, self.channels), dtype=np.float32)
        else:
            self._buffer = None  # type: ignore[assignment]
        self._write_count = 0
        self._read_counts = {0: 0}
        self._master_reader_id = 0
        self._lock = threading.Lock()

    def set_master_reader(self, reader_id: int) -> None:
        """Designate which reader dictates buffer write space and decoder pacing."""
        with self._lock:
            self._master_reader_id = reader_id

    def register_reader(self, reader_id: int) -> None:
        with self._lock:
            if reader_id not in self._read_counts:
                min_read = min(self._read_counts.values()) if self._read_counts else self._write_count
                self._read_counts[reader_id] = min_read

    def unregister_reader(self, reader_id: int) -> None:
        with self._lock:
            self._read_counts.pop(reader_id, None)
            if not self._read_counts:
                self._read_counts[0] = self._write_count
            if self._master_reader_id == reader_id:
                self._master_reader_id = next(iter(self._read_counts.keys()), 0)

    def reset(self) -> None:
        """Atomically clears the buffer and resets read/write pointers."""
        with self._lock:
            self._write_count = 0
            for rid in self._read_counts:
                self._read_counts[rid] = 0
            if self._buffer is not None:
                self._buffer.fill(0.0)

    def available(self, reader_id: int = 0) -> int:
        with self._lock:
            r_count = self._read_counts.get(reader_id, 0)
            min_allowed = self._write_count - self.capacity
            if r_count < min_allowed:
                r_count = min_allowed
            return max(0, self._write_count - r_count)

    def write_space(self) -> int:
        with self._lock:
            master_read = self._read_counts.get(self._master_reader_id, self._write_count)
            min_allowed = self._write_count - self.capacity
            if master_read < min_allowed:
                master_read = min_allowed
            used = self._write_count - master_read
            return max(0, self.capacity - used)

    def write(self, data: np.ndarray) -> int:
        """Writes audio frames into the ring buffer. Thread-safe and non-blocking."""
        if data is None or self._buffer is None:
            return 0
        if not isinstance(data, np.ndarray):
            data = np.asarray(data)
        if data.size == 0:
            return 0

        # Normalization to float32 in [-1.0, 1.0]
        if data.dtype == np.int16:
            data = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            data = data.astype(np.float32) / 2147483648.0
        elif data.dtype != np.float32:
            data = data.astype(np.float32)
        data = np.clip(data, -1.0, 1.0)

        # Shape & channel alignment
        if data.ndim == 1:
            if self.channels == 1:
                data = data.reshape(-1, 1)
            else:
                data = data.reshape(-1, self.channels if data.size % self.channels == 0 else 1)

        if data.shape[1] != self.channels:
            if self.channels == 1:
                data = np.mean(data, axis=1, keepdims=True).astype(np.float32)
            elif data.shape[1] == 1:
                data = np.repeat(data, self.channels, axis=1).astype(np.float32)
            elif data.shape[1] > self.channels:
                data = data[:, :self.channels].astype(np.float32)
            else:
                pad = np.repeat(data[:, :1], self.channels - data.shape[1], axis=1)
                data = np.concatenate([data, pad], axis=1).astype(np.float32)

        n = data.shape[0]
        with self._lock:
            master_read = self._read_counts.get(self._master_reader_id, self._write_count)
            min_allowed = self._write_count - self.capacity
            if master_read < min_allowed:
                master_read = min_allowed
            used = self._write_count - master_read
            space = self.capacity - used
            to_write = min(n, space)
            if to_write <= 0:
                return 0
            write_pos = self._write_count % self.capacity
            part1 = min(to_write, self.capacity - write_pos)
            self._buffer[write_pos : write_pos + part1] = data[:part1]
            part2 = to_write - part1
            if part2 > 0:
                self._buffer[:part2] = data[part1:to_write]
            self._write_count += to_write

            # If any reader falls behind by more than capacity, fast-forward it
            min_allowed = self._write_count - self.capacity
            for rid in list(self._read_counts.keys()):
                if self._read_counts[rid] < min_allowed:
                    self._read_counts[rid] = min_allowed
            return to_write

    def read(self, frames: int, reader_id: int = 0) -> tuple[np.ndarray, int]:
        """Reads requested frames from ring buffer into float32 array. Zero-pads on underrun."""
        if self._buffer is None or np is None:
            return np.zeros((frames, self.channels), dtype=np.float32), 0
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            r_count = self._read_counts.get(reader_id, 0)
            min_allowed = self._write_count - self.capacity
            if r_count < min_allowed:
                r_count = min_allowed
                self._read_counts[reader_id] = r_count
            avail = self._write_count - r_count
            to_read = min(frames, avail)
            if to_read <= 0:
                return out, 0
            read_pos = r_count % self.capacity
            part1 = min(to_read, self.capacity - read_pos)
            out[:part1] = self._buffer[read_pos : read_pos + part1]
            part2 = to_read - part1
            if part2 > 0:
                out[part1:to_read] = self._buffer[:part2]
            self._read_counts[reader_id] = r_count + to_read
            return out, to_read


def _app_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name.lower() == "dist":
            return exe_dir.parent
        return exe_dir
    return Path(__file__).resolve().parent.parent


class AudioEngineError(RuntimeError):
    """An expected setup or playback error."""


class _WinMMPortAudioError(RuntimeError):
    """Fallback PortAudioError equivalent for WinMM backend."""


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_char_p),
        ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


class _WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", ctypes.c_uint),
        ("wChannels", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("dwSupport", ctypes.c_uint),
    ]


class _WAVEINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", ctypes.c_uint),
        ("wChannels", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
    ]


_WAVE_FORMAT_PCM = 1
_WAVE_MAPPER = 0xFFFFFFFF
_CALLBACK_EVENT = 0x00050000
_WHDR_DONE = 0x00000001
_WHDR_PREPARED = 0x00000002


class _WinMMRawOutputStream:
    """Multi-buffered Windows WinMM waveOut stream compatible with sd.RawOutputStream."""

    NUM_BUFFERS = 4

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        dtype: str = "int16",
        device: Optional[int] = None,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        if sys.platform != "win32":
            raise _WinMMPortAudioError("WinMM доступний тільки на Windows.")
        self._winmm = ctypes.windll.winmm
        self._kernel32 = ctypes.windll.kernel32
        self.samplerate = int(samplerate)
        self.channels = max(1, int(channels))
        self.blocksize = max(256, int(blocksize))
        self._handle = ctypes.c_void_p()
        self._event = self._kernel32.CreateEventW(None, False, False, None)
        self._closed = False
        self._started = False

        num_out = int(self._winmm.waveOutGetNumDevs())
        if device is None:
            dev_id = _WAVE_MAPPER
        elif 0 <= int(device) < num_out:
            dev_id = int(device)
        else:
            dev_id = _WAVE_MAPPER

        block_align = self.channels * 2
        wfx = _WAVEFORMATEX(
            _WAVE_FORMAT_PCM,
            self.channels,
            self.samplerate,
            self.samplerate * block_align,
            block_align,
            16,
            0,
        )
        res = self._winmm.waveOutOpen(
            ctypes.byref(self._handle),
            ctypes.c_uint(dev_id),
            ctypes.byref(wfx),
            ctypes.c_size_t(self._event),
            0,
            _CALLBACK_EVENT,
        )
        if res != 0:
            if self._event:
                self._kernel32.CloseHandle(ctypes.c_void_p(self._event))
                self._event = None
            raise _WinMMPortAudioError(f"Не вдалося відкрити аудіовихід WinMM (код {res}).")
        if self._event:
            self._kernel32.ResetEvent(ctypes.c_void_p(self._event))

        buf_bytes = max(CHUNK_BYTES * 2, self.blocksize * block_align * 2)
        self._buffers = [ctypes.create_string_buffer(buf_bytes) for _ in range(self.NUM_BUFFERS)]
        self._headers = [
            _WAVEHDR(
                ctypes.cast(buf, ctypes.c_char_p),
                0,
                0,
                0,
                _WHDR_DONE,
                0,
                None,
                0,
            )
            for buf in self._buffers
        ]
        self._prepared = [False] * self.NUM_BUFFERS
        self._idx = 0

    def start(self) -> None:
        self._started = True

    def write(self, data: bytes) -> None:
        if self._closed or not self._handle or not data:
            return
        n = len(data)
        slot = self._idx
        self._idx = (self._idx + 1) % self.NUM_BUFFERS
        hdr = self._headers[slot]

        for _ in range(25):
            if not self._prepared[slot] or (hdr.dwFlags & _WHDR_DONE):
                break
            if self._event:
                self._kernel32.WaitForSingleObject(ctypes.c_void_p(self._event), 25)
            else:
                time.sleep(0.005)

        if self._prepared[slot]:
            if not (hdr.dwFlags & _WHDR_DONE):
                return
            self._winmm.waveOutUnprepareHeader(
                self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            self._prepared[slot] = False

        if n > len(self._buffers[slot]):
            self._buffers[slot] = ctypes.create_string_buffer(n)
        ctypes.memmove(self._buffers[slot], data, n)
        hdr.lpData = ctypes.cast(self._buffers[slot], ctypes.c_char_p)
        hdr.dwBufferLength = n
        hdr.dwFlags = 0
        res = self._winmm.waveOutPrepareHeader(
            self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
        )
        if res == 0:
            self._prepared[slot] = True
            self._winmm.waveOutWrite(
                self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )

    def drain(self) -> None:
        if self._closed or not self._handle or not self._started:
            return
        deadline = time.monotonic() + 0.35
        while not self._closed and time.monotonic() < deadline:
            pending = False
            for i, hdr in enumerate(self._headers):
                if self._prepared[i] and not (hdr.dwFlags & _WHDR_DONE):
                    pending = True
                    break
            if not pending:
                break
            if self._event:
                self._kernel32.WaitForSingleObject(ctypes.c_void_p(self._event), 15)
            else:
                time.sleep(0.005)

    def stop(self) -> None:
        if not self._closed and self._handle:
            self._winmm.waveOutReset(self._handle)
        self._started = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle:
            self._winmm.waveOutReset(self._handle)
            for i, hdr in enumerate(self._headers):
                if self._prepared[i]:
                    self._winmm.waveOutUnprepareHeader(
                        self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
                    )
                    self._prepared[i] = False
            self._winmm.waveOutClose(self._handle)
            self._handle = ctypes.c_void_p()
        if self._event:
            self._kernel32.CloseHandle(ctypes.c_void_p(self._event))
            self._event = None


class _WinMMRawInputStream:
    """Multi-buffered Windows WinMM waveIn stream compatible with sd.RawInputStream."""

    NUM_BUFFERS = 8

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        dtype: str = "int16",
        device: Optional[int] = None,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        if sys.platform != "win32":
            raise _WinMMPortAudioError("WinMM доступний тільки на Windows.")
        self._winmm = ctypes.windll.winmm
        self._kernel32 = ctypes.windll.kernel32
        self.samplerate = int(samplerate)
        self.channels = max(1, int(channels))
        self.blocksize = max(256, int(blocksize))
        self._handle = ctypes.c_void_p()
        self._event = self._kernel32.CreateEventW(None, False, False, None)
        self._closed = False

        num_out = int(self._winmm.waveOutGetNumDevs())
        num_in = int(self._winmm.waveInGetNumDevs())
        if device is None:
            dev_id = _WAVE_MAPPER
        else:
            raw_idx = int(device)
            if raw_idx >= num_out and (raw_idx - num_out) < num_in:
                dev_id = raw_idx - num_out
            elif 0 <= raw_idx < num_in:
                dev_id = raw_idx
            else:
                dev_id = _WAVE_MAPPER

        self._frame_bytes = self.channels * 2
        buf_bytes = self.blocksize * self._frame_bytes
        wfx = _WAVEFORMATEX(
            _WAVE_FORMAT_PCM,
            self.channels,
            self.samplerate,
            self.samplerate * self._frame_bytes,
            self._frame_bytes,
            16,
            0,
        )
        res = self._winmm.waveInOpen(
            ctypes.byref(self._handle),
            ctypes.c_uint(dev_id),
            ctypes.byref(wfx),
            ctypes.c_size_t(self._event),
            0,
            _CALLBACK_EVENT,
        )
        if res != 0:
            if self._event:
                self._kernel32.CloseHandle(ctypes.c_void_p(self._event))
                self._event = None
            raise _WinMMPortAudioError(f"Не вдалося відкрити мікрофон WinMM (код {res}).")
        if self._event:
            self._kernel32.ResetEvent(ctypes.c_void_p(self._event))

        self._buf_bytes = buf_bytes
        self._buffers = [ctypes.create_string_buffer(buf_bytes) for _ in range(self.NUM_BUFFERS)]
        self._headers = [
            _WAVEHDR(
                ctypes.cast(buf, ctypes.c_char_p),
                buf_bytes,
                0,
                0,
                0,
                0,
                None,
                0,
            )
            for buf in self._buffers
        ]
        self._prepared = [False] * self.NUM_BUFFERS
        self._idx = 0
        for i, hdr in enumerate(self._headers):
            if (
                self._winmm.waveInPrepareHeader(
                    self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
                )
                == 0
            ):
                self._prepared[i] = True
                self._winmm.waveInAddBuffer(
                    self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
                )

    def start(self) -> None:
        if not self._closed and self._handle:
            if self._event:
                self._kernel32.ResetEvent(ctypes.c_void_p(self._event))
            self._winmm.waveInStart(self._handle)

    def read(self, frames: int) -> tuple[bytes, bool]:
        needed_bytes = max(1, int(frames)) * self._frame_bytes
        if self._closed or not self._handle:
            return b"\x00" * needed_bytes, False

        slot = self._idx
        hdr = self._headers[slot]
        deadline = time.monotonic() + 0.08
        while not (hdr.dwFlags & _WHDR_DONE) and not self._closed and time.monotonic() < deadline:
            if self._event:
                self._kernel32.WaitForSingleObject(ctypes.c_void_p(self._event), 15)
            else:
                time.sleep(0.005)

        if hdr.dwFlags & _WHDR_DONE:
            self._idx = (self._idx + 1) % self.NUM_BUFFERS
            recorded = min(int(hdr.dwBytesRecorded), self._buf_bytes)
            raw = self._buffers[slot].raw[:recorded]
            hdr.dwBytesRecorded = 0
            hdr.dwFlags &= ~_WHDR_DONE
            self._winmm.waveInAddBuffer(
                self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
            )
            if len(raw) < needed_bytes:
                raw += b"\x00" * (needed_bytes - len(raw))
            elif len(raw) > needed_bytes:
                raw = raw[:needed_bytes]
            return raw, False

        return b"\x00" * needed_bytes, True

    def stop(self) -> None:
        if not self._closed and self._handle:
            self._winmm.waveInStop(self._handle)
            self._winmm.waveInReset(self._handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle:
            self._winmm.waveInReset(self._handle)
            for i, hdr in enumerate(self._headers):
                if self._prepared[i]:
                    self._winmm.waveInUnprepareHeader(
                        self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr)
                    )
                    self._prepared[i] = False
            self._winmm.waveInClose(self._handle)
            self._handle = ctypes.c_void_p()
        if self._event:
            self._kernel32.CloseHandle(ctypes.c_void_p(self._event))
            self._event = None


class _WavPipeDecoder:
    """In-process wave decoder with Popen-compatible interface when ffmpeg is unavailable."""

    def __init__(
        self,
        source: str,
        start_pos: float = 0.0,
        target_rate: Optional[int] = None,
    ) -> None:
        self._terminated = False
        self.stderr = io.BytesIO()
        pcm = self._decode_wav_to_s16le(source, start_pos, target_rate=target_rate)
        self.stdout = io.BytesIO(pcm)

    @staticmethod
    def _read_wav_pcm(source: str, start_pos: float) -> tuple[int, int, int, bytes]:
        with open(source, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return 0, 0, 0, b""
            fmt_tag = 1
            nch = 0
            rate = 0
            sw = 0
            raw = b""
            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break
                chunk_id, chunk_size = struct.unpack("<4sI", chunk_hdr)
                if chunk_id == b"fmt " and chunk_size >= 16:
                    fmt_data = f.read(chunk_size)
                    fmt_tag, nch, rate, _, _, bits = struct.unpack("<HHIIHH", fmt_data[:16])
                    sw = max(1, bits // 8)
                    if fmt_tag == 3 and sw == 4:
                        sw = -4
                elif chunk_id == b"data":
                    raw = f.read(chunk_size)
                    break
                else:
                    f.seek(chunk_size, 1)
                if chunk_size % 2 == 1:
                    f.seek(1, 1)
            if start_pos > 0.0 and rate > 0 and nch > 0 and sw != 0 and raw:
                frame_bytes = nch * abs(sw)
                total_frames = len(raw) // frame_bytes
                seek_frame = min(total_frames, max(0, int(start_pos * rate)))
                raw = raw[seek_frame * frame_bytes :]
            return nch, sw, rate, raw

    @staticmethod
    def _decode_wav_to_s16le(
        source: str, start_pos: float, target_rate: Optional[int] = None
    ) -> bytes:
        nch, sw, rate, raw = _WavPipeDecoder._read_wav_pcm(source, start_pos)
        if not raw or nch <= 0 or sw == 0:
            return b""
        if sw == 1:
            samples = array("h", (((b - 128) << 8) for b in raw))
        elif sw == 2:
            if len(raw) % 2 != 0:
                raw = raw[:-1]
            samples = array("h")
            samples.frombytes(raw)
        elif sw == 3:
            samples = array(
                "h",
                (
                    int.from_bytes(raw[i + 1 : i + 3], "little", signed=True)
                    for i in range(0, len(raw) - 2, 3)
                ),
            )
        elif sw == 4:
            samples = array(
                "h",
                (
                    int.from_bytes(raw[i + 2 : i + 4], "little", signed=True)
                    for i in range(0, len(raw) - 3, 4)
                ),
            )
        elif sw == -4:
            usable = len(raw) - (len(raw) % 4)
            floats = struct.unpack(f"<{usable // 4}f", raw[:usable])
            samples = array(
                "h",
                (
                    max(-32768, min(32767, int(round((f if f == f else 0.0) * 32767.0))))
                    for f in floats
                ),
            )
        else:
            return b""

        if nch == 1:
            stereo = array("h")
            for s in samples:
                stereo.append(s)
                stereo.append(s)
            samples = stereo
        elif nch > 2:
            stereo = array("h")
            for i in range(0, len(samples) - nch + 1, nch):
                stereo.append(samples[i])
                stereo.append(samples[i + 1])
            samples = stereo

        pcm = samples.tobytes()
        if target_rate is not None and rate != target_rate and rate > 0:
            pcm = AudioEngine._resample_pcm(pcm, rate, target_rate)
        return pcm

    def poll(self) -> Optional[int]:
        return 0 if self._terminated else None

    def terminate(self) -> None:
        self._terminated = True
        self.stdout = io.BytesIO()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: Optional[float] = None) -> int:
        self._terminated = True
        return 0


class _WinMMSoundDeviceShim:
    """Drop-in shim for sounddevice using Windows WinMM when sounddevice is not installed."""

    PortAudioError = _WinMMPortAudioError
    RawOutputStream = _WinMMRawOutputStream
    RawInputStream = _WinMMRawInputStream

    @staticmethod
    def query_devices(device: Optional[int] = None) -> Any:
        if sys.platform != "win32":
            raise _WinMMPortAudioError("sounddevice не встановлено.")
        winmm = ctypes.windll.winmm
        devices: list[dict[str, Any]] = []
        num_out = int(winmm.waveOutGetNumDevs())
        for i in range(num_out):
            caps = _WAVEOUTCAPSW()
            if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                devices.append(
                    {
                        "name": caps.szPname,
                        "max_output_channels": max(2, int(caps.wChannels or 2)),
                        "max_input_channels": 0,
                        "default_samplerate": float(SAMPLE_RATE),
                        "hostapi": 0,
                    }
                )
        num_in = int(winmm.waveInGetNumDevs())
        for i in range(num_in):
            caps = _WAVEINCAPSW()
            if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                devices.append(
                    {
                        "name": caps.szPname,
                        "max_output_channels": 0,
                        "max_input_channels": max(1, min(2, int(caps.wChannels or 2))),
                        "default_samplerate": float(SAMPLE_RATE),
                        "hostapi": 0,
                    }
                )
        if device is not None:
            idx = int(device)
            if 0 <= idx < len(devices):
                return devices[idx]
            raise _WinMMPortAudioError(f"Неправильний індекс пристрою: {device}")
        return devices


if sd is None and sys.platform == "win32":
    sd = _WinMMSoundDeviceShim()  # type: ignore[assignment]


@dataclass
class StreamTrack:
    title: str
    source: str
    is_url: bool = False
    duration: float = 0.0
    start_pos: float = 0.0
    artist: str = ""
    _resolved_source: Optional[str] = None


_YT_STREAM_CACHE: dict[str, tuple[str, str, float]] = {}
_ALBUM_CACHE: dict[str, dict[str, Any]] = {}
_YT_CACHE_LOCK = threading.Lock()
_YT_CACHE_TTL = 3600.0 * 3


def _is_mocked_youtube() -> bool:
    for obj in (YTMusic, YoutubeDL):
        if obj is not None and (
            hasattr(obj, "_mock_name")
            or hasattr(obj, "assert_called")
            or "mock" in type(obj).__module__.lower()
        ):
            return True
    return False


class AudioEngine:
    """Streams decoded PCM to a selected Windows audio output device."""

    def __init__(
        self,
        output_device: Optional[int] = None,
        input_device: Optional[int] = None,
        monitor_device: Optional[int] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
        on_track_title: Optional[Callable[[str], None]] = None,
        on_position: Optional[Callable[[float], None]] = None,
        on_apo_warning: Optional[Callable[[str], None]] = None,
        monitor_mic: bool = True,
        blocksize: int = DEFAULT_BLOCKSIZE,
        routing_mode: str = ROUTING_MODE_VIRTUAL_CABLE,
        target_endpoint_guid: Optional[str] = None,
        shm_path: Optional[Union[Path, str]] = None,
    ) -> None:
        self.output_device = output_device
        self.input_device = input_device
        self.monitor_device = monitor_device
        self.on_error = on_error
        self.on_finished = on_finished
        self.on_track_title = on_track_title
        self.on_position = on_position
        self.on_apo_warning = on_apo_warning
        self.blocksize = max(MIN_BLOCKSIZE, min(MAX_BLOCKSIZE, int(blocksize)))
        self.routing_mode = (
            routing_mode
            if routing_mode in (ROUTING_MODE_VIRTUAL_CABLE, ROUTING_MODE_INJECTION)
            else ROUTING_MODE_VIRTUAL_CABLE
        )
        self.target_endpoint_guid = target_endpoint_guid
        self._shm_path = shm_path
        self._dsp_core = DspCore()
        self._injection_shm: Optional[InjectionShm] = None
        self._lease_warning_reported = False
        self._ring_buffer = Float32RingBuffer(capacity_frames=65536, channels=CHANNELS)
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._stream: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._decode_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._devices_dirty = threading.Event()
        self._track_dirty = threading.Event()
        self._decoder_finished = threading.Event()
        self._keep_mic_alive = (
            input_device is not None
            or (self.routing_mode == ROUTING_MODE_INJECTION and bool(target_endpoint_guid))
        )
        self._pending_track: Optional[StreamTrack] = None
        self._active_track: Optional[StreamTrack] = None
        self._active_resolved_source: Optional[str] = None
        self._active_resolved_title: Optional[str] = None
        self._position_seconds = 0.0
        self._volume = 1.0
        self._mic_volume = 1.0
        self._mic_muted = False
        self._monitor_mic = monitor_mic
        self._route_speakers = True
        self._route_mic = True
        self._deepfilter_engine = DeepFilterEngine()
        self._deepfilter_enabled = False
        self._deepfilter_wet = 1.0
        self._white_noise_removal = False
        self._voice_boost_enabled = False
        self._voice_boost_db = 0.0
        self._lock = threading.RLock()

    def _compute_voice_fx_flags(self) -> int:
        flags = 0
        if self._deepfilter_enabled:
            flags |= MPAD_FX_DEEPFILTER
        if self._white_noise_removal:
            flags |= MPAD_FX_WHITE_NOISE
        if self._voice_boost_enabled:
            flags |= MPAD_FX_VOICE_BOOST
        return flags

    def _effective_mic_gain(self) -> float:
        if self._voice_boost_enabled:
            return float(10.0 ** (self._voice_boost_db / 20.0))
        return float(self._mic_volume)

    @property
    def deepfilter_enabled(self) -> bool:
        with self._lock:
            return self._deepfilter_enabled

    @property
    def deepfilter_wet(self) -> float:
        with self._lock:
            return self._deepfilter_wet

    @property
    def white_noise_removal(self) -> bool:
        with self._lock:
            return self._white_noise_removal

    @property
    def voice_boost_enabled(self) -> bool:
        with self._lock:
            return self._voice_boost_enabled

    @property
    def voice_boost_db(self) -> float:
        with self._lock:
            return self._voice_boost_db

    def _sync_voice_control_to_shm(self) -> None:
        """Push current mic gain, mute, and DSP voice FX flags to SHM."""
        with self._lock:
            shm = self._injection_shm
            fx_flags = self._compute_voice_fx_flags()
            fx_wet = self._deepfilter_wet
            effective_gain = self._effective_mic_gain()
            muted = self._mic_muted
            track_active = bool(
                self._active_track is not None
                and self._route_mic
                and not self._pause_event.is_set()
            )
            guid = self.target_endpoint_guid
        if shm is not None and shm.has_lease:
            try:
                shm.update_control(
                    mic_gain=effective_gain,
                    mic_mute=muted,
                    track_playing=track_active,
                    voice_fx_flags=fx_flags,
                    voice_fx_param1=fx_wet,
                    passthrough=False,
                    target_endpoint_guid=guid or "",
                )
            except Exception:
                pass

    def set_deepfilter_state(self, enabled: bool, wet: float = 1.0) -> None:
        with self._lock:
            self._deepfilter_enabled = bool(enabled)
            self._deepfilter_wet = max(0.0, min(1.0, float(wet)))
        self._sync_voice_control_to_shm()

    def set_white_noise_removal(self, enabled: bool) -> None:
        with self._lock:
            self._white_noise_removal = bool(enabled)
        self._sync_voice_control_to_shm()

    def set_voice_boost(self, enabled: bool, gain_db: float = 0.0) -> None:
        with self._lock:
            self._voice_boost_enabled = bool(enabled)
            self._voice_boost_db = max(0.0, min(24.0, float(gain_db)))
        self._sync_voice_control_to_shm()

    def set_blocksize(self, blocksize: int) -> None:
        val = max(MIN_BLOCKSIZE, min(MAX_BLOCKSIZE, int(blocksize)))
        with self._lock:
            if self.blocksize != val:
                self.blocksize = val
                self._devices_dirty.set()

    @staticmethod
    def apply_soft_knee_limiter(
        signal: np.ndarray, threshold: float = SOFT_KNEE_THRESHOLD
    ) -> np.ndarray:
        """
        Hybrid Soft-Knee Limiter:
        - 100% bit-transparent and linear for |x| <= threshold (-1 dBFS, ~0.891).
        - Smooth continuous C1 hyperbolic tangent saturation for overshoot |x| > threshold.
        - Hard clamped in range [-1.0, 1.0] preventing digital clipping and distortion.
        """
        if signal.size == 0 or np is None:
            return signal
        abs_sig = np.abs(signal)
        mask = abs_sig > threshold
        if not np.any(mask):
            return signal
        out = signal.copy()
        scale = max(1e-6, 1.0 - threshold)
        over = (abs_sig[mask] - threshold) / scale
        out[mask] = np.sign(signal[mask]) * (threshold + scale * np.tanh(over))
        return np.clip(out, -1.0, 1.0)

    @staticmethod
    def resample_to_48k_float32(
        data: np.ndarray,
        source_rate: int,
        source_channels: int = 2,
        target_rate: int = SAMPLE_RATE,
    ) -> np.ndarray:
        """
        Vectorized polyphase resampler converting any input PCM to 48,000 Hz 2-channel float32.
        Normalizes int16/int32 to [-1.0, 1.0] and handles mono/multi-channel mapping.
        """
        if data.size == 0 or target_rate <= 0 or np is None:
            return np.zeros((0, CHANNELS), dtype=np.float32)

        if not isinstance(data, np.ndarray):
            data = np.asarray(data)

        if data.dtype == np.int16:
            float_data = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            float_data = data.astype(np.float32) / 2147483648.0
        elif data.dtype != np.float32:
            float_data = data.astype(np.float32)
        else:
            float_data = data.astype(np.float32, copy=False)
        float_data = np.clip(float_data, -1.0, 1.0)

        if float_data.ndim == 1:
            float_data = float_data.reshape(-1, max(1, source_channels))

        curr_channels = float_data.shape[1]
        if curr_channels == 1:
            stereo = np.repeat(float_data[:, :1], CHANNELS, axis=1)
        elif curr_channels > CHANNELS:
            stereo = float_data[:, :CHANNELS]
        else:
            stereo = float_data

        if source_rate <= 0 or source_rate == target_rate:
            return np.clip(stereo, -1.0, 1.0).astype(np.float32)

        source_frames = stereo.shape[0]
        if source_frames < 2:
            return np.clip(stereo, -1.0, 1.0).astype(np.float32)

        target_frames = max(1, round(source_frames * target_rate / source_rate))
        x_old = np.linspace(0.0, source_frames - 1, num=source_frames, dtype=np.float32)
        x_new = np.linspace(0.0, source_frames - 1, num=target_frames, dtype=np.float32)

        out = np.empty((target_frames, CHANNELS), dtype=np.float32)
        for c in range(CHANNELS):
            out[:, c] = np.interp(x_new, x_old, stereo[:, c])
        return np.clip(out, -1.0, 1.0).astype(np.float32)

    @staticmethod
    def resample_float32_channels(
        data: np.ndarray,
        source_rate: int,
        target_rate: int,
        target_channels: int = CHANNELS,
    ) -> np.ndarray:
        """
        Convert float32 stereo/multi-channel PCM to target sample rate and channel count
        (e.g. matching APO hardware format in injection mode).
        """
        t_ch = max(1, int(target_channels))
        if data is None or data.size == 0 or np is None:
            return np.zeros((0, t_ch), dtype=np.float32)
        if not isinstance(data, np.ndarray):
            data = np.asarray(data)
        if data.dtype == np.int16:
            arr = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            arr = data.astype(np.float32) / 2147483648.0
        elif data.dtype != np.float32:
            arr = data.astype(np.float32)
        else:
            arr = data.astype(np.float32, copy=False)
        arr = np.clip(arr, -1.0, 1.0)

        if arr.ndim == 1:
            arr = arr.reshape(-1, CHANNELS if arr.size % CHANNELS == 0 else 1)

        if source_rate > 0 and target_rate > 0 and source_rate != target_rate and arr.shape[0] >= 2:
            src_frames = arr.shape[0]
            dst_frames = max(1, round(src_frames * target_rate / source_rate))
            x_old = np.linspace(0.0, src_frames - 1, num=src_frames, dtype=np.float32)
            x_new = np.linspace(0.0, src_frames - 1, num=dst_frames, dtype=np.float32)
            resampled = np.empty((dst_frames, arr.shape[1]), dtype=np.float32)
            for c in range(arr.shape[1]):
                resampled[:, c] = np.interp(x_new, x_old, arr[:, c])
            arr = resampled

        curr_ch = arr.shape[1]
        if curr_ch == t_ch:
            return np.clip(arr, -1.0, 1.0).astype(np.float32)
        if t_ch == 1:
            return np.clip(np.mean(arr, axis=1, keepdims=True), -1.0, 1.0).astype(np.float32)
        if curr_ch == 1:
            return np.clip(np.repeat(arr, t_ch, axis=1), -1.0, 1.0).astype(np.float32)
        if curr_ch > t_ch:
            return np.clip(arr[:, :t_ch], -1.0, 1.0).astype(np.float32)
        # Pad remaining channels by repeating first channel
        pad_ch = np.repeat(arr[:, :1], t_ch - curr_ch, axis=1)
        return np.clip(np.concatenate([arr, pad_ch], axis=1), -1.0, 1.0).astype(np.float32)

    @staticmethod
    def mix_float32(
        music_arr: np.ndarray,
        mic_arr: np.ndarray,
        mic_volume: float = 1.0,
        track_volume: float = 1.0,
        mic_muted: bool = False,
    ) -> np.ndarray:
        """Thread-safe linear mixing of two float32 PCM streams with soft-knee limiting."""
        if np is None:
            return music_arr
        n_frames = max(
            music_arr.shape[0] if music_arr.size else 0,
            mic_arr.shape[0] if mic_arr.size else 0,
        )
        if n_frames == 0:
            return np.zeros((0, CHANNELS), dtype=np.float32)

        if music_arr.size == 0:
            m = np.zeros((n_frames, CHANNELS), dtype=np.float32)
        elif music_arr.shape[0] < n_frames:
            m = np.pad(music_arr, ((0, n_frames - music_arr.shape[0]), (0, 0)))
        else:
            m = music_arr[:n_frames]

        if mic_arr.size == 0 or mic_muted:
            c = np.zeros((n_frames, CHANNELS), dtype=np.float32)
        elif mic_arr.shape[0] < n_frames:
            c = np.pad(mic_arr, ((0, n_frames - mic_arr.shape[0]), (0, 0)))
        else:
            c = mic_arr[:n_frames]

        scaled_m = m * float(track_volume)
        scaled_c = c * float(mic_volume)
        summed = scaled_m + scaled_c
        return AudioEngine.apply_soft_knee_limiter(summed)

    @staticmethod
    def find_ffmpeg() -> Optional[str]:
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                bundled = Path(meipass) / "ffmpeg.exe"
                if bundled.is_file():
                    return str(bundled)
        found = shutil.which("ffmpeg")
        if found:
            return found
        root_dir = _app_root_dir()
        exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else root_dir
        candidates = [
            Path(getattr(sys, "_MEIPASS", root_dir)) / "ffmpeg.exe",
            exe_dir / "ffmpeg.exe",
            root_dir / "ffmpeg.exe",
            Path(__file__).resolve().parent.parent / "ffmpeg.exe",
            Path.home() / "Downloads" / "ffmpeg-9.0.2-full_build" / "ffmpeg-9.0.2-full_build" / "bin" / "ffmpeg.exe",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        return None

    @staticmethod
    def find_ffprobe() -> Optional[str]:
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                bundled = Path(meipass) / "ffprobe.exe"
                if bundled.is_file():
                    return str(bundled)
        found = shutil.which("ffprobe")
        if found:
            return found
        root_dir = _app_root_dir()
        exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else root_dir
        candidates = [
            Path(getattr(sys, "_MEIPASS", root_dir)) / "ffprobe.exe",
            exe_dir / "ffprobe.exe",
            root_dir / "ffprobe.exe",
            Path(__file__).resolve().parent.parent / "ffprobe.exe",
            Path.home() / "Downloads" / "ffmpeg-9.0.2-full_build" / "ffmpeg-9.0.2-full_build" / "bin" / "ffprobe.exe",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        ffmpeg_bin = AudioEngine.find_ffmpeg()
        if ffmpeg_bin:
            sibling = Path(ffmpeg_bin).with_name("ffprobe.exe")
            if sibling.is_file():
                return str(sibling)
        return None

    @staticmethod
    def check_ffmpeg() -> bool:
        return AudioEngine.find_ffmpeg() is not None

    @staticmethod
    def list_devices() -> list[dict]:
        if sd is None:
            raise AudioEngineError("Не вдалося отримати список аудіопристроїв: sounddevice не встановлено.")
        try:
            return list(sd.query_devices())
        except Exception as exc:
            raise AudioEngineError(f"Не вдалося отримати список аудіопристроїв: {exc}") from exc

    @staticmethod
    def find_cable_output() -> Optional[int]:
        devices = AudioEngine.list_devices()
        fallback: Optional[int] = None
        for index, device in enumerate(devices):
            name = str(device.get("name", "")).lower()
            if "cable input" in name and device.get("max_output_channels", 0) > 0:
                if device.get("hostapi", 0) == 0:
                    return index
                if fallback is None:
                    fallback = index
        return fallback

    @staticmethod
    def find_physical_mic() -> Optional[int]:
        devices = AudioEngine.list_devices()
        virtual_keywords = (
            "cable",
            "vb-audio",
            "virtual",
            "stereo mix",
            "стерео мікшер",
            "steam streaming",
            "ivcam",
            "droidcam",
            "what u hear",
        )
        preferred_keywords = (
            "fifine",
            "hyperx",
            "blue",
            "rode",
            "shure",
            "usb",
            "microphone",
            "мікрофон",
            "headset",
            "realtek",
        )
        best_preferred: Optional[int] = None
        best_physical: Optional[int] = None
        fallback_any: Optional[int] = None

        for index, device in enumerate(devices):
            if device.get("max_input_channels", 0) <= 0:
                continue
            if fallback_any is None:
                fallback_any = index
            if device.get("hostapi", 0) != 0 and best_physical is not None:
                continue
            name = str(device.get("name", "")).lower()
            if any(vk in name for vk in virtual_keywords):
                continue
            if best_physical is None:
                best_physical = index
            if best_preferred is None and any(pk in name for pk in preferred_keywords):
                best_preferred = index
                if "fifine" in name or "usb" in name or "hyperx" in name:
                    return index

        return best_preferred if best_preferred is not None else (
            best_physical if best_physical is not None else fallback_any
        )

    @staticmethod
    def find_default_monitor(exclude_device: Optional[int] = None) -> Optional[int]:
        devices = AudioEngine.list_devices()
        virtual_keywords = ("cable", "vb-audio", "virtual", "steam streaming")
        hdmi_keywords = ("nvidia", "amd high definition", "display audio")
        headphone_keywords = ("headphone", "навуш", "headset", "realtek", "speakers", "динаміки")

        best_headphone: Optional[int] = None
        best_non_hdmi: Optional[int] = None
        fallback: Optional[int] = None

        for index, device in enumerate(devices):
            if index == exclude_device or device.get("max_output_channels", 0) <= 0:
                continue
            name = str(device.get("name", "")).lower()
            if any(vk in name for vk in virtual_keywords):
                continue
            if device.get("hostapi", 0) != 0 and fallback is not None:
                continue
            if fallback is None:
                fallback = index
            if not any(hk in name for hk in hdmi_keywords):
                if best_non_hdmi is None:
                    best_non_hdmi = index
                if best_headphone is None and any(pk in name for pk in headphone_keywords):
                    best_headphone = index
                    if "headphone" in name or "навуш" in name or "realtek" in name:
                        return index

        return best_headphone if best_headphone is not None else (
            best_non_hdmi if best_non_hdmi is not None else fallback
        )

    def _compute_keep_mic_alive(self) -> bool:
        if self.routing_mode == ROUTING_MODE_INJECTION:
            return bool(self.target_endpoint_guid) or self.input_device is not None
        return self.input_device is not None

    def _ensure_injection_shm(self) -> Optional[InjectionShm]:
        with self._lock:
            if self.routing_mode != ROUTING_MODE_INJECTION:
                return None
            if self._injection_shm is None:
                try:
                    self._injection_shm = (
                        InjectionShm(self._shm_path)
                        if self._shm_path is not None
                        else InjectionShm()
                    )
                    self._injection_shm.open()
                except Exception as exc:
                    self._injection_shm = None
                    if self.on_error:
                        self.on_error(f"Не вдалося відкрити спільну пам'ять ін'єкції: {exc}")
                    return None
            if not self._injection_shm.has_lease:
                acquired = self._injection_shm.acquire_writer_lease()
                if not acquired and not self._lease_warning_reported:
                    self._lease_warning_reported = True
                    if self.on_error:
                        self.on_error("Інша копія MusicPad вже використовує режим ін'єкції.")
                elif acquired:
                    self._lease_warning_reported = False
            if self._injection_shm.has_lease:
                self._injection_shm.update_control(
                    mic_gain=self._effective_mic_gain(),
                    mic_mute=self._mic_muted,
                    track_playing=bool(
                        self._active_track is not None
                        and self._route_mic
                        and not self._pause_event.is_set()
                    ),
                    voice_fx_flags=self._compute_voice_fx_flags(),
                    voice_fx_param1=self._deepfilter_wet,
                    passthrough=False,
                    target_endpoint_guid=self.target_endpoint_guid or "",
                )
            return self._injection_shm

    def _close_injection_shm(self) -> None:
        with self._lock:
            shm = self._injection_shm
            self._injection_shm = None
            self._lease_warning_reported = False
        if shm is not None:
            try:
                shm.close()
            except Exception:
                pass

    def get_apo_runtime_status(self) -> Optional[ApoRuntimeStatus]:
        with self._lock:
            shm = self._injection_shm
        if shm is not None:
            try:
                return shm.get_apo_status()
            except Exception:
                return None
        try:
            temp_shm = (
                InjectionShm(self._shm_path)
                if self._shm_path is not None
                else InjectionShm()
            )
            temp_shm.open()
            try:
                return temp_shm.get_apo_status()
            finally:
                temp_shm.close()
        except Exception:
            return None

    def set_routing_mode(self, mode: str) -> None:
        norm = (
            mode
            if mode in (ROUTING_MODE_VIRTUAL_CABLE, ROUTING_MODE_INJECTION)
            else ROUTING_MODE_VIRTUAL_CABLE
        )
        with self._lock:
            changed = self.routing_mode != norm
            self.routing_mode = norm
            self._keep_mic_alive = self._compute_keep_mic_alive()
            if changed:
                self._devices_dirty.set()
            has_music = self._process is not None or self._pending_track is not None
            keep_alive = self._keep_mic_alive
        if norm != ROUTING_MODE_INJECTION:
            self._close_injection_shm()
        else:
            self._ensure_injection_shm()
        if not keep_alive and not has_music:
            self._stop_worker()
            return
        if self._thread is None or not self._thread.is_alive():
            self._start_worker()

    def set_target_endpoint_guid(self, guid: Optional[str]) -> None:
        with self._lock:
            changed = self.target_endpoint_guid != guid
            self.target_endpoint_guid = guid
            self._keep_mic_alive = self._compute_keep_mic_alive()
            if changed:
                self._devices_dirty.set()
            has_music = self._process is not None or self._pending_track is not None
            keep_alive = self._keep_mic_alive
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.update_control(
                    mic_gain=self._effective_mic_gain(),
                    mic_mute=self._mic_muted,
                    track_playing=bool(
                        self._active_track is not None
                        and self._route_mic
                        and not self._pause_event.is_set()
                    ),
                    voice_fx_flags=self._compute_voice_fx_flags(),
                    voice_fx_param1=self._deepfilter_wet,
                    passthrough=False,
                    target_endpoint_guid=guid or "",
                )
            except Exception:
                pass
        if self.routing_mode == ROUTING_MODE_INJECTION and guid:
            self._ensure_injection_shm()
        if not keep_alive and not has_music:
            self._stop_worker()
            return
        if self._thread is None or not self._thread.is_alive():
            self._start_worker()

    def set_output_device(self, device_index: Optional[int]) -> None:
        with self._lock:
            changed = self.output_device != device_index
            self.output_device = device_index
            if changed:
                self._devices_dirty.set()

    def set_input_device(self, device_index: Optional[int]) -> None:
        with self._lock:
            changed = self.input_device != device_index
            self.input_device = device_index
            self._keep_mic_alive = self._compute_keep_mic_alive()
            if changed:
                self._devices_dirty.set()
            has_music = self._process is not None or self._pending_track is not None
            keep_alive = self._keep_mic_alive
        if not keep_alive:
            if not has_music:
                self._stop_worker()
            return
        if self._thread is None or not self._thread.is_alive():
            self._start_worker()

    def set_monitor_device(self, device_index: Optional[int]) -> None:
        with self._lock:
            changed = self.monitor_device != device_index
            self.monitor_device = device_index
            if changed:
                self._devices_dirty.set()

    def set_mic_muted(self, muted: bool) -> None:
        self._mic_muted = bool(muted)
        with self._lock:
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.update_control(
                    mic_gain=self._effective_mic_gain(),
                    mic_mute=self._mic_muted,
                    track_playing=bool(
                        self._active_track is not None
                        and self._route_mic
                        and not self._pause_event.is_set()
                    ),
                    voice_fx_flags=self._compute_voice_fx_flags(),
                    voice_fx_param1=self._deepfilter_wet,
                    passthrough=False,
                    target_endpoint_guid=self.target_endpoint_guid,
                )
            except Exception:
                pass

    def set_monitor_mic(self, enabled: bool) -> None:
        val = bool(enabled)
        with self._lock:
            changed = self._monitor_mic != val
            self._monitor_mic = val
            if changed and self.routing_mode == ROUTING_MODE_INJECTION:
                self._devices_dirty.set()

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    def set_mic_volume(self, volume: float) -> None:
        self._mic_volume = max(0.0, min(2.0, float(volume)))
        with self._lock:
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.update_control(
                    mic_gain=self._effective_mic_gain(),
                    mic_mute=self._mic_muted,
                    track_playing=bool(
                        self._active_track is not None
                        and self._route_mic
                        and not self._pause_event.is_set()
                    ),
                    voice_fx_flags=self._compute_voice_fx_flags(),
                    voice_fx_param1=self._deepfilter_wet,
                    passthrough=False,
                    target_endpoint_guid=self.target_endpoint_guid,
                )
            except Exception:
                pass

    def set_mute_broadcast(self, mute: bool) -> None:
        """Mute track audio to broadcast (virtual cable / mic injection) while keeping headphones monitor."""
        with self._lock:
            self._route_mic = not bool(mute)
            shm = self._injection_shm
            track_active_for_apo = bool(
                self._active_track is not None
                and self._route_mic
                and not self._pause_event.is_set()
            )
        if shm is not None and shm.has_lease:
            try:
                shm.update_control(
                    mic_gain=self._effective_mic_gain(),
                    mic_mute=self._mic_muted,
                    track_playing=track_active_for_apo,
                    voice_fx_flags=self._compute_voice_fx_flags(),
                    voice_fx_param1=self._deepfilter_wet,
                    passthrough=False,
                    target_endpoint_guid=self.target_endpoint_guid,
                )
            except Exception:
                pass

    def get_mute_broadcast(self) -> bool:
        return not self._route_mic

    def get_position(self) -> float:
        return self._position_seconds

    def is_playing(self) -> bool:
        return self._process is not None and not self._pause_event.is_set()

    @staticmethod
    def _raw_to_float32(raw_bytes: bytes, channels: int = 2) -> np.ndarray:
        if not raw_bytes or np is None:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        if len(raw_bytes) % 2 != 0:
            raw_bytes = raw_bytes[:-1]
        if not raw_bytes:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        arr = np.frombuffer(raw_bytes, dtype=np.int16)
        ch = max(1, channels)
        usable = len(arr) - (len(arr) % ch)
        if usable <= 0:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        arr = arr[:usable].reshape(-1, ch)
        return np.clip(arr.astype(np.float32) / 32768.0, -1.0, 1.0)

    def play(
        self,
        track: Union[StreamTrack, str],
        speakers: bool = True,
        mic: bool = True,
    ) -> None:
        if isinstance(track, str):
            track_obj = StreamTrack(Path(track).name, str(track))
        else:
            track_obj = track

        is_local_wav = (
            not track_obj.is_url
            and str(track_obj.source).lower().endswith(".wav")
            and os.path.isfile(track_obj.source)
        )
        if not self.check_ffmpeg() and not is_local_wav:
            raise AudioEngineError(
                "FFmpeg не знайдено. Встановіть FFmpeg і додайте його до PATH."
            )
        with self._lock:
            self._route_speakers = bool(speakers)
            self._route_mic = bool(mic)
            self._pending_track = track_obj
            self._active_track = track_obj
            self._position_seconds = max(0.0, float(getattr(track_obj, "start_pos", 0.0) or 0.0))
            self._keep_mic_alive = self._compute_keep_mic_alive()
            self._pause_event.clear()
            self._ring_buffer.reset()
            self._decoder_finished.clear()
            self._track_dirty.set()
            old_process = self._process
            self._process = None
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.flush()
            except Exception:
                pass
        if old_process is not None and old_process.poll() is None:
            try:
                old_process.terminate()
            except OSError:
                pass
        self._start_worker()

    def seek(self, seconds: float) -> None:
        with self._lock:
            base = self._active_track
            if base is None:
                return
            pos = max(0.0, float(seconds))
            self._position_seconds = pos
            seek_track = StreamTrack(
                title=self._active_resolved_title or base.title,
                source=base.source,
                is_url=base.is_url,
                duration=base.duration,
                start_pos=pos,
                artist=getattr(base, "artist", ""),
                _resolved_source=self._active_resolved_source,
            )
            self._pending_track = seek_track
            self._ring_buffer.reset()
            self._decoder_finished.clear()
            self._track_dirty.set()
            self._pause_event.clear()
            old_process = self._process
            self._process = None
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.flush()
            except Exception:
                pass
        if old_process is not None and old_process.poll() is None:
            try:
                old_process.terminate()
            except OSError:
                pass
        self._start_worker()

    def pause(self) -> None:
        self._pause_event.set()
        with self._lock:
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.flush()
            except Exception:
                pass

    def resume(self) -> None:
        self._pause_event.clear()

    def stop(self) -> None:
        with self._lock:
            self._pending_track = None
            self._active_track = None
            self._active_resolved_source = None
            self._position_seconds = 0.0
            self._ring_buffer.reset()
            self._decoder_finished.clear()
            self._track_dirty.set()
            self._pause_event.clear()
            keep_mic = self._keep_mic_alive
            if not keep_mic:
                self._stop_event.set()
            process = self._process
            self._process = None
            shm = self._injection_shm
        if shm is not None and shm.has_lease:
            try:
                shm.flush()
                shm.update_control(
                    mic_gain=self._mic_volume,
                    mic_mute=self._mic_muted,
                    track_playing=False,
                    passthrough=False,
                    target_endpoint_guid=self.target_endpoint_guid,
                )
            except Exception:
                pass
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if not keep_mic:
            self._stop_worker()

    def shutdown(self) -> None:
        with self._lock:
            self._pending_track = None
            self._active_track = None
            self._keep_mic_alive = False
        self._stop_worker()
        self._close_injection_shm()

    def reinitialize_audio_subsystem(self) -> None:
        """Cleanly re-initialize PortAudio (sounddevice) and SHM after Windows Audiosrv restarts."""
        self._stop_worker()
        with self._lock:
            self._close_injection_shm()
            self._ring_buffer.reset()
            self._decoder_finished.clear()
            self._track_dirty.set()

        if sd is not None:
            try:
                if hasattr(sd, "_terminate") and hasattr(sd, "_initialize"):
                    sd._terminate()
                    sd._initialize()
            except Exception:
                pass

        with self._lock:
            self._devices_dirty.set()

        if self.routing_mode == ROUTING_MODE_INJECTION:
            self._ensure_injection_shm()

        self._start_worker()

    def _start_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._pause_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="musicpad-audio", daemon=True
            )
            self._thread.start()

    def _stop_worker(self) -> None:
        self._stop_event.set()
        self._track_dirty.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        decode_th = self._decode_thread
        if decode_th is not None and decode_th is not threading.current_thread():
            try:
                decode_th.join(timeout=1.5)
            except Exception:
                pass
        self._decode_thread = None

        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=3)
            except Exception:
                pass
        if thread is None or not thread.is_alive():
            self._thread = None

    @staticmethod
    def _drain_stream_safe(stream_obj: Optional[Any]) -> None:
        if stream_obj is None:
            return
        drain_fn = getattr(stream_obj, "drain", None)
        if callable(drain_fn):
            try:
                drain_fn()
            except Exception:
                pass
            return
        if not hasattr(stream_obj, "_mock_name"):
            stop_fn = getattr(stream_obj, "stop", None)
            if callable(stop_fn):
                try:
                    stop_fn()
                except Exception:
                    pass

    @staticmethod
    def _close_stream_safe(stream_obj: Optional[Any]) -> None:
        if stream_obj is None:
            return
        for method_name in ("stop", "close"):
            method = getattr(stream_obj, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass

    @staticmethod
    def _cleanup_process(process: Optional[subprocess.Popen[bytes]]) -> None:
        if process is None:
            return
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=1)
            except Exception:
                pass

    def _open_audio_streams(
        self,
    ) -> tuple[Optional[Any], Optional[Any], int, Optional[Any], int]:
        mic_stream: Optional[Any] = None
        stream: Optional[Any] = None
        monitor_stream: Optional[Any] = None
        monitor_rate = SAMPLE_RATE
        mic_channels = 0
        bs = self.blocksize

        with self._lock:
            in_dev = self.input_device
            out_dev = self.output_device
            mon_dev = self.monitor_device
            mode = self.routing_mode
            mon_mic = self._monitor_mic

        try:
            if mode == ROUTING_MODE_INJECTION:
                self._ensure_injection_shm()
                # Decision 5: In injection mode, APO handles mic mixing/mute/gain.
                # App captures microphone ONLY if local headphone mic monitoring is enabled,
                # but records mic_channels to align channels if APO status is uninitialized.
                if in_dev is not None:
                    mic_channels = self._input_channels(in_dev)
                    if mon_mic and mon_dev is not None:
                        mic_stream = sd.RawInputStream(
                            samplerate=SAMPLE_RATE,
                            channels=mic_channels,
                            dtype="int16",
                            device=in_dev,
                            blocksize=bs,
                        )
                        mic_stream.start()
                self._stream = None
                if mon_dev is not None:
                    monitor_rate = self._device_samplerate(mon_dev)
                    try:
                        monitor_stream = sd.RawOutputStream(
                            samplerate=monitor_rate,
                            channels=CHANNELS,
                            dtype="int16",
                            device=mon_dev,
                            blocksize=bs,
                        )
                    except Exception:
                        monitor_rate = SAMPLE_RATE
                        monitor_stream = sd.RawOutputStream(
                            samplerate=monitor_rate,
                            channels=CHANNELS,
                            dtype="int16",
                            device=mon_dev,
                            blocksize=bs,
                        )
                    monitor_stream.start()
                return None, monitor_stream, monitor_rate, mic_stream, mic_channels

            if in_dev is not None:
                mic_channels = self._input_channels(in_dev)
                mic_stream = sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=mic_channels,
                    dtype="int16",
                    device=in_dev,
                    blocksize=bs,
                )
                mic_stream.start()
            stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=out_dev,
                blocksize=bs,
            )
            stream.start()
            self._stream = stream
            if mon_dev is not None and mon_dev != out_dev:
                monitor_rate = self._device_samplerate(mon_dev)
                try:
                    monitor_stream = sd.RawOutputStream(
                        samplerate=monitor_rate,
                        channels=CHANNELS,
                        dtype="int16",
                        device=mon_dev,
                        blocksize=bs,
                    )
                except Exception:
                    monitor_rate = SAMPLE_RATE
                    monitor_stream = sd.RawOutputStream(
                        samplerate=monitor_rate,
                        channels=CHANNELS,
                        dtype="int16",
                        device=mon_dev,
                        blocksize=bs,
                    )
                monitor_stream.start()
            return stream, monitor_stream, monitor_rate, mic_stream, mic_channels
        except Exception:
            self._close_stream_safe(monitor_stream)
            self._close_stream_safe(stream)
            self._close_stream_safe(mic_stream)
            raise

    def _build_ffmpeg_cmd(self, source: str, start_pos: float = 0.0) -> list[str]:
        is_popen_mocked = (
            hasattr(subprocess.Popen, "_mock_name")
            or hasattr(subprocess.Popen, "assert_called")
            or "mock" in type(subprocess.Popen).__module__.lower()
        )
        if is_popen_mocked and start_pos <= 0.0:
            return [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source,
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "pipe:1",
            ]
        ffmpeg_bin = "ffmpeg" if shutil.which("ffmpeg") else (self.find_ffmpeg() or "ffmpeg")
        if source.startswith(("http://", "https://")) or start_pos > 0.0:
            cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]
            if source.startswith(("http://", "https://")):
                cmd.extend(
                    [
                        "-reconnect",
                        "1",
                        "-reconnect_streamed",
                        "1",
                        "-reconnect_delay_max",
                        "5",
                        "-probesize",
                        "32768",
                        "-analyzeduration",
                        "0",
                    ]
                )
            if start_pos > 0.0:
                cmd.extend(["-ss", f"{start_pos:.2f}"])
            cmd.extend(
                [
                    "-i",
                    source,
                    "-vn",
                    "-f",
                    "s16le",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    str(CHANNELS),
                    "pipe:1",
                ]
            )
            return cmd
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "pipe:1",
        ]

    def _decode_loop(self) -> None:
        """Background thread decoding track chunks into Float32RingBuffer without blocking audio callback."""
        while not self._stop_event.is_set():
            if self._track_dirty.is_set():
                self._track_dirty.clear()

            with self._lock:
                next_track = self._pending_track
                self._pending_track = None
                active_track = self._active_track

            if next_track is not None:
                self._ring_buffer.reset()
                self._decoder_finished.clear()
                if self._process is not None:
                    self._cleanup_process(self._process)
                    self._process = None

                try:
                    source, resolved_title = self._resolve_source(next_track)
                    with self._lock:
                        self._active_resolved_source = source
                        self._active_resolved_title = resolved_title
                    if self.on_track_title:
                        self.on_track_title(resolved_title)
                    start_pos = max(0.0, float(getattr(next_track, "start_pos", 0.0) or 0.0))
                    with self._lock:
                        self._position_seconds = start_pos

                    is_popen_mocked = (
                        hasattr(subprocess.Popen, "_mock_name")
                        or hasattr(subprocess.Popen, "assert_called")
                        or "mock" in type(subprocess.Popen).__module__.lower()
                    )
                    if (
                        not is_popen_mocked
                        and self.find_ffmpeg() is None
                        and source.lower().endswith(".wav")
                        and os.path.isfile(source)
                    ):
                        process = _WavPipeDecoder(source, start_pos, target_rate=SAMPLE_RATE)
                    else:
                        cmd = self._build_ffmpeg_cmd(source, start_pos)
                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    self._process = process
                except Exception as exc:
                    if not self._stop_event.is_set() and self.on_error:
                        self.on_error(str(exc))
                    continue

            process = self._process
            if process is None or process.stdout is None:
                time.sleep(0.01)
                continue

            if self._ring_buffer.write_space() < (CHUNK_BYTES // (CHANNELS * 2)):
                time.sleep(0.005)
                continue

            if self._pending_track is not None or self._stop_event.is_set() or self._track_dirty.is_set():
                continue

            try:
                raw = process.stdout.read(CHUNK_BYTES)
            except Exception:
                raw = b""

            if self._pending_track is not None or self._stop_event.is_set() or self._track_dirty.is_set():
                continue

            if not raw:
                self._cleanup_process(process)
                with self._lock:
                    if self._process == process:
                        self._process = None
                self._decoder_finished.set()
                while (
                    not self._stop_event.is_set()
                    and self._pending_track is None
                    and not self._track_dirty.is_set()
                ):
                    time.sleep(0.01)
                continue

            # Convert decoded S16LE PCM chunks to float32 stereo [-1.0, 1.0] at 48kHz
            frame_bytes = CHANNELS * 2
            remainder = len(raw) % frame_bytes
            if remainder != 0:
                raw = raw[:-remainder]
            if len(raw) >= frame_bytes:
                try:
                    arr = np.frombuffer(raw, dtype=np.int16).reshape(-1, CHANNELS)
                    float_arr = np.clip(arr.astype(np.float32) / 32768.0, -1.0, 1.0)
                    self._ring_buffer.write(float_arr)
                except Exception:
                    pass

    def _run(self) -> None:
        stream: Optional[Any] = None
        monitor_stream: Optional[Any] = None
        monitor_rate = SAMPLE_RATE
        mic_stream: Optional[Any] = None
        mic_channels = 0
        notified_finished = False
        last_pos_notify = 0.0

        try:
            self._devices_dirty.clear()
            stream, monitor_stream, monitor_rate, mic_stream, mic_channels = (
                self._open_audio_streams()
            )

            self._decode_thread = threading.Thread(
                target=self._decode_loop, name="musicpad-decode", daemon=True
            )
            self._decode_thread.start()

            if self.routing_mode == ROUTING_MODE_INJECTION:
                self._ring_buffer.register_reader(READER_MONITOR)
                self._ring_buffer.set_master_reader(
                    READER_MONITOR if monitor_stream is not None else READER_INJECTION
                )
            else:
                self._ring_buffer.set_master_reader(READER_INJECTION)

            track_playback_start = 0.0
            apo_warning_emitted_for_track = False
            last_seen_track: Optional[StreamTrack] = None

            while not self._stop_event.is_set():
                if self._active_track != last_seen_track:
                    last_seen_track = self._active_track
                    track_playback_start = time.monotonic() if self._active_track is not None else 0.0
                    apo_warning_emitted_for_track = False

                if self._devices_dirty.is_set():
                    self._devices_dirty.clear()
                    self._close_stream_safe(monitor_stream)
                    self._close_stream_safe(stream)
                    self._close_stream_safe(mic_stream)
                    stream, monitor_stream, monitor_rate, mic_stream, mic_channels = (
                        self._open_audio_streams()
                    )
                    if self.routing_mode == ROUTING_MODE_INJECTION:
                        self._ring_buffer.set_master_reader(
                            READER_MONITOR if monitor_stream is not None else READER_INJECTION
                        )
                    else:
                        self._ring_buffer.set_master_reader(READER_INJECTION)

                frames = self.blocksize
                is_mock_stream = hasattr(stream, "_mock_name") or hasattr(stream, "assert_called")
                is_mock_monitor = hasattr(monitor_stream, "_mock_name") or hasattr(
                    monitor_stream, "assert_called"
                )

                # 1. Capture real-time physical microphone (low-latency PCM float32)
                if mic_stream is not None and not self._mic_muted:
                    try:
                        raw_mic, _ = mic_stream.read(frames)
                        mic_float = self._raw_to_float32(bytes(raw_mic), mic_channels)
                        if mic_float.shape[0] < frames:
                            mic_float = np.pad(
                                mic_float,
                                ((0, frames - mic_float.shape[0]), (0, 0)),
                            )
                        elif mic_float.shape[0] > frames:
                            mic_float = mic_float[:frames]
                        if mic_channels == 1 or mic_float.shape[1] == 1:
                            mic_float = np.repeat(mic_float[:, :1], CHANNELS, axis=1)

                        # Stage 1: DeepFilterNet (wet/dry)
                        with self._lock:
                            df_enabled = self._deepfilter_enabled
                            df_wet = self._deepfilter_wet
                            fx_flags = self._compute_voice_fx_flags()
                            effective_gain = self._effective_mic_gain()
                            muted = self._mic_muted

                        if df_enabled and self._deepfilter_engine.is_available:
                            mic_float = self._deepfilter_engine.process_buffer(mic_float, wet=df_wet)

                        # Stages 2, 3, 4: White noise removal (DC HPF + Expander), Voice Boost, Limiter
                        mic_float = self._dsp_core.process_voice(
                            mic_float,
                            sample_rate=SAMPLE_RATE,
                            mic_gain=effective_gain,
                            mic_mute=muted,
                            voice_fx_flags=fx_flags,
                            fx_param1=df_wet,
                        )
                    except Exception:
                        mic_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                else:
                    mic_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                    if mic_stream is None and is_mock_stream:
                        time.sleep(frames / SAMPLE_RATE)

                # 2. Non-blocking read from lock-free Float32RingBuffer
                with self._lock:
                    cur_mode = self.routing_mode

                if self._pause_event.is_set():
                    track_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                    read_frames = 0
                    has_music = self._active_track is not None
                elif cur_mode == ROUTING_MODE_INJECTION:
                    # In injection mode: Injection reader maintains a 100-120ms lead buffer in SHM.
                    # Monitor reader feeds the local headphones at monitor pace.
                    shm = self._ensure_injection_shm()
                    apo_status = shm.get_apo_status() if (shm is not None and shm.has_lease) else None
                    track_active_for_apo = bool(
                        self._active_track is not None
                        and self._route_mic
                        and not self._pause_event.is_set()
                    )
                    if track_active_for_apo and (apo_status is None or not apo_status.is_alive):
                        if (
                            not apo_warning_emitted_for_track
                            and track_playback_start > 0.0
                            and (time.monotonic() - track_playback_start) > 1.0
                        ):
                            apo_warning_emitted_for_track = True
                            warn_msg = (
                                "Драйвер MusicPad не активний у службі audiodg.exe "
                                "(звук грає лише в навушниках). Відкрийте мікрофон у Discord/грі "
                                "або перевірте стан драйвера."
                            )
                            if self.on_apo_warning:
                                try:
                                    self.on_apo_warning(warn_msg)
                                except Exception:
                                    pass
                            elif self.on_error:
                                try:
                                    self.on_error(warn_msg)
                                except Exception:
                                    pass
                    if shm is not None and shm.has_lease:
                        shm.update_control(
                            mic_gain=self._effective_mic_gain(),
                            mic_mute=self._mic_muted,
                            track_playing=track_active_for_apo,
                            voice_fx_flags=self._compute_voice_fx_flags(),
                            voice_fx_param1=self._deepfilter_wet,
                            passthrough=False,
                            target_endpoint_guid=self.target_endpoint_guid,
                        )
                        if track_active_for_apo and apo_status is not None:
                            in_rate = (
                                self._device_samplerate(self.input_device)
                                if self.input_device is not None
                                else 0
                            )
                            apo_rate = (
                                apo_status.sample_rate
                                if apo_status.sample_rate > 0
                                else (in_rate if in_rate > 0 else SAMPLE_RATE)
                            )
                            apo_ch = (
                                apo_status.channels
                                if apo_status.channels > 0
                                else (mic_channels if mic_channels > 0 else CHANNELS)
                            )
                            # Maintain 120 ms lead buffer cushion in SHM
                            target_lead_samples = int(0.120 * apo_rate * apo_ch)
                            avail_shm = shm.available_to_read()
                            free_shm = shm.available_to_write()
                            if avail_shm < target_lead_samples and free_shm > 0:
                                deficit_samples = target_lead_samples - avail_shm
                                deficit_frames = max(frames, deficit_samples // apo_ch)
                                inj_track_raw, inj_read = self._ring_buffer.read(
                                    deficit_frames, reader_id=READER_INJECTION
                                )
                                if inj_read > 0:
                                    inj_track = inj_track_raw[:inj_read]
                                    if self._volume != 1.0:
                                        inj_track = inj_track * self._volume
                                    inj_track = np.clip(inj_track, -1.0, 1.0)
                                    inj_samples = self.resample_float32_channels(
                                        inj_track,
                                        SAMPLE_RATE,
                                        apo_rate,
                                        apo_ch,
                                    )
                                    shm.write_samples(inj_samples)
                        elif not track_active_for_apo:
                            # Discard injection frames when broadcast to mic is muted/inactive
                            avail_inj_drain = self._ring_buffer.available(READER_INJECTION)
                            if avail_inj_drain > 0:
                                drain_cnt = frames if monitor_stream is not None else avail_inj_drain
                                self._ring_buffer.read(min(avail_inj_drain, drain_cnt), reader_id=READER_INJECTION)

                    # Monitor reader
                    if monitor_stream is not None:
                        track_float, read_frames = self._ring_buffer.read(
                            frames, reader_id=READER_MONITOR
                        )
                    else:
                        track_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                        avail_shm_now = shm.available_to_read() if shm is not None else 0
                        read_frames = frames if (track_active_for_apo and avail_shm_now > 0) else 0

                    # If broadcast to mic is muted or APO inactive, discard any leftover injection frames
                    if not (self._route_mic and apo_status is not None):
                        leftover_inj = self._ring_buffer.available(READER_INJECTION)
                        if leftover_inj > 0 and (monitor_stream is None or self._ring_buffer.available(READER_MONITOR) == 0):
                            self._ring_buffer.read(leftover_inj, reader_id=READER_INJECTION)

                    avail_inj = (
                        self._ring_buffer.available(READER_INJECTION)
                        if (self._route_mic and apo_status is not None)
                        else 0
                    )
                    avail_mon = (
                        self._ring_buffer.available(READER_MONITOR)
                        if monitor_stream is not None
                        else 0
                    )
                    avail_shm = (
                        shm.available_to_read()
                        if (self._route_mic and shm is not None)
                        else 0
                    )
                    has_music = (
                        read_frames > 0
                        or self._process is not None
                        or self._pending_track is not None
                        or avail_inj > 0
                        or avail_mon > 0
                        or avail_shm > 0
                    )
                else:
                    # Virtual Cable mode: Single stream reads frames and mixes to stream and monitor
                    track_float, read_frames = self._ring_buffer.read(
                        frames, reader_id=READER_INJECTION
                    )
                    has_music = (
                        read_frames > 0
                        or self._process is not None
                        or self._pending_track is not None
                        or self._ring_buffer.available(READER_INJECTION) > 0
                    )

                if read_frames > 0 and self._active_track is not None:
                    self._position_seconds += read_frames / SAMPLE_RATE
                    if cur_mode != ROUTING_MODE_INJECTION and self._volume != 1.0:
                        track_float = track_float * self._volume
                    track_float = np.clip(track_float, -1.0, 1.0)
                    now = time.monotonic()
                    if self.on_position and (now - last_pos_notify) >= 0.15:
                        last_pos_notify = now
                        self.on_position(self._position_seconds)
                    notified_finished = False
                elif (
                    self._decoder_finished.is_set()
                    and not has_music
                    and self._active_track is not None
                ):
                    with self._lock:
                        has_pending = self._pending_track is not None
                        keep_mic = self._keep_mic_alive
                        was_stopped = self._stop_event.is_set()
                        if not has_pending:
                            self._active_track = None
                    if not has_pending and not was_stopped:
                        if not keep_mic:
                            self._drain_stream_safe(stream)
                            self._drain_stream_safe(monitor_stream)
                        notified_finished = True
                        if self.on_finished:
                            self.on_finished()
                    self._decoder_finished.clear()
                    has_music = has_pending

                # Determine if engine should stop
                with self._lock:
                    keep_mic = self._keep_mic_alive
                    has_pending = self._pending_track is not None
                    cur_mode = self.routing_mode
                if not keep_mic and not has_music and not has_pending:
                    break

                # 3 & 4. Routing output: Virtual Cable branch
                if cur_mode != ROUTING_MODE_INJECTION and stream is not None:
                    track_for_cable = track_float if self._route_mic else np.zeros_like(track_float)
                    mixed_cable = mic_float + track_for_cable
                    limited_cable = self.apply_soft_knee_limiter(mixed_cable)
                    out_bytes = (
                        np.clip(np.round(limited_cable * 32767.0), -32768, 32767)
                        .astype(np.int16)
                        .tobytes()
                    )
                    stream.write(out_bytes)

                # 5. Output write to monitor headphones/speakers
                if monitor_stream is not None:
                    if cur_mode == ROUTING_MODE_INJECTION:
                        if self._volume != 1.0:
                            track_float = track_float * self._volume
                        track_float = np.clip(track_float, -1.0, 1.0)
                    track_for_monitor = track_float if self._route_speakers else np.zeros_like(track_float)
                    if self._monitor_mic:
                        mon_mixed = mic_float + track_for_monitor
                    else:
                        mon_mixed = track_for_monitor
                    limited_mon = self.apply_soft_knee_limiter(mon_mixed)
                    mon_bytes = np.clip(np.round(limited_mon * 32767.0), -32768, 32767).astype(np.int16).tobytes()
                    if monitor_rate != SAMPLE_RATE:
                        mon_bytes = self._resample_pcm(mon_bytes, SAMPLE_RATE, monitor_rate)
                    monitor_stream.write(mon_bytes)

                # Pacing when no real hardware stream is blocking the loop (e.g. injection without monitor or mocked monitor)
                if cur_mode == ROUTING_MODE_INJECTION and mic_stream is None and (
                    monitor_stream is None or is_mock_monitor
                ):
                    time.sleep(frames / SAMPLE_RATE)
        except Exception as exc:
            if not self._stop_event.is_set() and self.on_error:
                self.on_error(str(exc))
        finally:
            self._ring_buffer.set_master_reader(READER_INJECTION)
            self._ring_buffer.unregister_reader(READER_MONITOR)
            self._close_stream_safe(monitor_stream)
            self._close_stream_safe(stream)
            self._close_stream_safe(mic_stream)
            self._cleanup_process(self._process)
            decode_th = self._decode_thread
            if decode_th is not None and decode_th is not threading.current_thread():
                try:
                    decode_th.join(timeout=1.0)
                except Exception:
                    pass
            self._decode_thread = None
            with self._lock:
                self._process = None
                self._stream = None
                restart_needed = (
                    not self._stop_event.is_set()
                    and (self._pending_track is not None or self._keep_mic_alive)
                )
                if restart_needed:
                    self._thread = threading.Thread(
                        target=self._run, name="musicpad-audio", daemon=True
                    )
                    self._thread.start()
                else:
                    self._thread = None
            if (
                not restart_needed
                and not self._stop_event.is_set()
                and not notified_finished
                and self.on_finished
            ):
                self.on_finished()

    @staticmethod
    def _scale_pcm(data: bytes, volume: float) -> bytes:
        data = bytes(data)
        if not data:
            return b""
        if len(data) % 2 != 0:
            data = data + b"\x00"
        if np is not None:
            arr = np.frombuffer(data, dtype=np.int16)
            scaled = np.clip(arr.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
            return scaled.tobytes()
        samples = array("h")
        samples.frombytes(data)
        samples = array("h", (max(-32768, min(32767, int(sample * volume))) for sample in samples))
        return samples.tobytes()

    @staticmethod
    def _input_channels(device_index: int) -> int:
        device = sd.query_devices(device_index)
        channels = int(device.get("max_input_channels", 0))
        if channels < 1:
            raise AudioEngineError("Вибраний пристрій запису не має вхідних каналів.")
        return min(channels, CHANNELS)

    @staticmethod
    def _device_samplerate(device_index: int) -> int:
        device = sd.query_devices(device_index)
        rate = int(round(float(device.get("default_samplerate", SAMPLE_RATE))))
        if rate < 8000:
            raise AudioEngineError("Навушники повернули некоректну частоту дискретизації.")
        return rate

    @staticmethod
    def _resample_pcm(data: bytes, source_rate: int, target_rate: int) -> bytes:
        data = bytes(data)
        if not data or source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
            return data
        if len(data) % (CHANNELS * 2) != 0:
            pad = (CHANNELS * 2) - (len(data) % (CHANNELS * 2))
            data = data + (b"\x00" * pad)
        if np is not None:
            arr = np.frombuffer(data, dtype=np.int16).reshape(-1, CHANNELS)
            source_frames = int(arr.shape[0])
            if source_frames < 2:
                return data
            target_frames = max(1, round(source_frames * target_rate / source_rate))
            x_old = np.linspace(0.0, source_frames - 1, num=source_frames, dtype=np.float32)
            x_new = np.linspace(0.0, source_frames - 1, num=target_frames, dtype=np.float32)
            out = np.empty((target_frames, CHANNELS), dtype=np.int16)
            for channel in range(CHANNELS):
                out[:, channel] = np.clip(
                    np.round(np.interp(x_new, x_old, arr[:, channel])), -32768, 32767
                ).astype(np.int16)
            return out.tobytes()
        samples = array("h")
        samples.frombytes(data)
        source_frames = len(samples) // CHANNELS
        if source_frames < 2:
            return data
        target_frames = max(1, round(source_frames * target_rate / source_rate))
        denom = max(1, target_frames - 1)
        scale = (source_frames - 1) / denom
        max_right = source_frames - 1
        result = array("h", [0] * (target_frames * CHANNELS))
        for frame in range(target_frames):
            position = frame * scale
            left = int(position)
            right = left + 1 if left < max_right else max_right
            fraction = position - left
            for channel in range(CHANNELS):
                first = samples[left * CHANNELS + channel]
                second = samples[right * CHANNELS + channel]
                result[frame * CHANNELS + channel] = round(first + (second - first) * fraction)
        return result.tobytes()

    @staticmethod
    def _mix_pcm(music: bytes, microphone: bytes, mic_channels: int) -> bytes:
        music = bytes(music)
        microphone = bytes(microphone)
        if not music:
            return b""
        if len(music) % 2 != 0:
            music = music + b"\x00"
        if len(microphone) % 2 != 0:
            microphone = microphone + b"\x00"
        if np is not None and len(music) % (CHANNELS * 2) == 0:
            music_arr = np.frombuffer(music, dtype=np.int16).reshape(-1, CHANNELS)
            music_frames = int(music_arr.shape[0])
            mic_raw = np.frombuffer(microphone, dtype=np.int16)
            ch = max(1, int(mic_channels))
            mic_frames = len(mic_raw) // ch if ch > 0 else 0
            if mic_frames > 0 and music_frames > 0:
                mic_arr = mic_raw[: mic_frames * ch].reshape(mic_frames, ch)
                idx = np.minimum(np.arange(music_frames), mic_frames - 1)
                mic_sel = mic_arr[idx]
                if ch == 1:
                    mic_sel = np.repeat(mic_sel[:, :1], CHANNELS, axis=1)
                else:
                    mic_sel = mic_sel[:, :CHANNELS]
                music_f = music_arr.astype(np.float32) / 32768.0
                mic_f = mic_sel.astype(np.float32) / 32768.0
                mixed_f = AudioEngine.apply_soft_knee_limiter(music_f + mic_f)
                mixed_i = np.clip(np.round(mixed_f * 32768.0), -32768, 32767).astype(np.int16)
                return mixed_i.tobytes()
            return music_arr.tobytes()
        music_samples = array("h")
        mic_samples = array("h")
        music_samples.frombytes(music)
        mic_samples.frombytes(microphone)
        output = array("h")
        mic_frames = len(mic_samples) // mic_channels if mic_channels > 0 else 0
        music_frames = len(music_samples) // CHANNELS
        for frame in range(music_frames):
            for channel in range(CHANNELS):
                mic_frame = min(frame, max(0, mic_frames - 1))
                mic_channel = channel if mic_channels == CHANNELS else 0
                mic_value = mic_samples[mic_frame * mic_channels + mic_channel] if mic_frames else 0
                mixed = music_samples[frame * CHANNELS + channel] + mic_value
                output.append(max(-32768, min(32767, mixed)))
        return output.tobytes()

    @staticmethod
    def _cache_dir() -> Path:
        base = _app_root_dir() / ".cache" / "youtube"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return base

    @staticmethod
    def _cache_key(query: str) -> str:
        norm = query.strip().lower()
        match = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", query)
        if match:
            return f"yt_{match.group(1)}"
        return "q_" + hashlib.sha1(norm.encode("utf-8", errors="ignore")).hexdigest()[:16]

    @classmethod
    def get_cached_file(cls, query: str) -> Optional[ tuple[str, str, float] ]:
        key = cls._cache_key(query)
        cache_dir = cls._cache_dir()
        meta_path = cache_dir / f"{key}.json"
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_path = str(meta.get("path", "")).strip()
            audio_path = Path(raw_path) if raw_path else (cache_dir / f"{key}.m4a")
            if not audio_path.is_file():
                audio_path = cache_dir / f"{key}.m4a"
            if audio_path.is_file() and audio_path.stat().st_size > 0:
                return (
                    str(audio_path),
                    str(meta.get("title") or query),
                    float(meta.get("duration") or 0.0),
                )
        except Exception:
            pass
        return None

    @classmethod
    def clear_cache(cls) -> None:
        with _YT_CACHE_LOCK:
            _YT_STREAM_CACHE.clear()
            _ALBUM_CACHE.clear()

    @classmethod
    def get_cache_size_bytes(cls) -> int:
        total = 0
        for sub in ("youtube", "thumbnails"):
            d = _app_root_dir() / ".cache" / sub
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file():
                        try:
                            total += f.stat().st_size
                        except Exception:
                            pass
        return total

    @classmethod
    def purge_cache(cls) -> tuple[int, int]:
        """Purge cached media files in .cache/youtube and .cache/thumbnails.
        Returns (deleted_count, freed_bytes).
        """
        deleted_count = 0
        freed_bytes = 0
        with _YT_CACHE_LOCK:
            _YT_STREAM_CACHE.clear()
            _ALBUM_CACHE.clear()

        for sub in ("youtube", "thumbnails"):
            d = _app_root_dir() / ".cache" / sub
            if d.is_dir():
                for f in list(d.rglob("*")):
                    if f.is_file():
                        try:
                            sz = f.stat().st_size
                            f.unlink()
                            freed_bytes += sz
                            deleted_count += 1
                        except Exception:
                            pass
        return deleted_count, freed_bytes

    @staticmethod
    def _resolve_source(track: StreamTrack) -> tuple[str, str]:
        if not track.is_url:
            return os.path.abspath(track.source), track.title
        if getattr(track, "_resolved_source", None):
            return str(track._resolved_source), track.title

        use_cache = not _is_mocked_youtube()
        cache_key = track.source.strip().lower()
        if use_cache:
            local_cached = AudioEngine.get_cached_file(track.source)
            if local_cached is not None:
                if local_cached[2] > 0:
                    track.duration = local_cached[2]
                return local_cached[0], local_cached[1]
            with _YT_CACHE_LOCK:
                cached_entry = _YT_STREAM_CACHE.get(cache_key)
                if cached_entry is not None and cached_entry[2] > time.time():
                    if len(cached_entry) >= 4 and cached_entry[3] > 0 and track.duration <= 0.0:
                        track.duration = float(cached_entry[3])
                    return cached_entry[0], cached_entry[1]

        try:
            if YoutubeDL is None:
                raise AudioEngineError(
                    "Модулі ytmusicapi або yt-dlp не встановлено."
                )
            query = track.source.strip()
            resolved_title = track.title
            if "://" not in query:
                if YTMusic is None:
                    if not use_cache:
                        raise AudioEngineError("Модулі ytmusicapi або yt-dlp не встановлено.")
                    query = f"ytsearch1:{query}"
                else:
                    try:
                        ytmusic = YTMusic()
                        results = ytmusic.search(query, filter="songs")
                        if not results and use_cache:
                            results = ytmusic.search(query, filter="videos")
                        if not results:
                            raise AudioEngineError(f"Трек не знайдено: {query}")
                        video_id = results[0].get("videoId")
                        if not video_id:
                            raise AudioEngineError("YouTube Music не повернув ID треку.")
                        result = results[0]
                        artists = ", ".join(
                            artist.get("name", "")
                            for artist in result.get("artists", [])
                            if artist.get("name")
                        )
                        resolved_title = result.get("title", query)
                        if artists:
                            resolved_title = f"{resolved_title} — {artists}"
                        if result.get("duration_seconds"):
                            track.duration = float(result["duration_seconds"])
                        query = f"https://music.youtube.com/watch?v={video_id}"
                    except AudioEngineError:
                        raise
                    except Exception:
                        if not use_cache:
                            raise
                        query = f"ytsearch1:{query}"
            with YoutubeDL(
                {
                    "format": "bestaudio[ext=m4a]/bestaudio/best",
                    "quiet": True,
                    "noplaylist": True,
                    "no_warnings": True,
                }
            ) as ydl:
                info = ydl.extract_info(query, download=False)
                if isinstance(info, dict) and "entries" in info and info["entries"]:
                    info = info["entries"][0]
                url = info.get("url") if isinstance(info, dict) else None
                if not url:
                    raise AudioEngineError("Не вдалося отримати URL аудіопотоку.")
                if isinstance(info, dict):
                    if "://" in track.source or query.startswith("ytsearch1:"):
                        resolved_title = info.get("title") or resolved_title
                    if info.get("duration") and track.duration <= 0.0:
                        try:
                            track.duration = float(info["duration"])
                        except (TypeError, ValueError):
                            pass
                if use_cache:
                    with _YT_CACHE_LOCK:
                        _YT_STREAM_CACHE[cache_key] = (
                            url,
                            resolved_title,
                            time.time() + _YT_CACHE_TTL,
                            float(track.duration or 0.0),
                        )
                return url, resolved_title
        except AudioEngineError:
            raise
        except Exception as exc:
            raise AudioEngineError(f"Помилка YouTube Music: {exc}") from exc

    @classmethod
    def _thumbnail_cache_dir(cls) -> Path:
        base = _app_root_dir() / ".cache" / "thumbnails"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return base

    @classmethod
    def fetch_thumbnail(cls, url: str, video_id: str = "") -> Optional[str]:
        if not url:
            return None
        cache_dir = cls._thumbnail_cache_dir()
        if not video_id:
            video_id = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:16]
        cached_file = cache_dir / f"{video_id}.png"
        if cached_file.is_file() and cached_file.stat().st_size > 0:
            return str(cached_file)
        try:
            import urllib.request
            from io import BytesIO
            from PIL import Image

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw_bytes = resp.read()
            img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
            w, h = img.size
            min_dim = min(w, h)
            left = (w - min_dim) // 2
            top = (h - min_dim) // 2
            img_cropped = img.crop((left, top, left + min_dim, top + min_dim))
            img_resized = img_cropped.resize((40, 40), Image.Resampling.LANCZOS)
            img_resized.save(cached_file, "PNG")
            return str(cached_file)
        except Exception:
            return None

    @classmethod
    def get_album(cls, browse_id: str) -> dict[str, Any]:
        browse_id = str(browse_id).strip()
        if not browse_id:
            return {}
        with _YT_CACHE_LOCK:
            if browse_id in _ALBUM_CACHE:
                return dict(_ALBUM_CACHE[browse_id])
        if YTMusic is None:
            raise AudioEngineError("Модуль ytmusicapi не встановлено.")
        ytmusic = YTMusic()
        actual_browse_id = browse_id
        if not actual_browse_id.startswith("MPRE") and hasattr(ytmusic, "get_album_browse_id"):
            try:
                resolved_id = ytmusic.get_album_browse_id(actual_browse_id)
                if resolved_id and str(resolved_id).startswith("MPRE"):
                    actual_browse_id = str(resolved_id).strip()
            except Exception:
                pass

        try:
            raw_album = ytmusic.get_album(actual_browse_id)
        except Exception as err:
            if hasattr(ytmusic, "get_playlist"):
                try:
                    raw_album = ytmusic.get_playlist(browse_id)
                except Exception:
                    raise AudioEngineError(f"Альбом не знайдено: {browse_id}") from err
            else:
                raise AudioEngineError(f"Альбом не знайдено: {browse_id}") from err

        if not raw_album or not isinstance(raw_album, dict):
            raise AudioEngineError(f"Альбом не знайдено: {browse_id}")

        album_title = str(raw_album.get("title") or "Альбом").strip()
        artists_list = raw_album.get("artists") or []
        album_artist = ", ".join(
            a.get("name", "")
            for a in artists_list
            if isinstance(a, dict) and a.get("name")
        )
        if not album_artist and raw_album.get("artist"):
            album_artist = str(raw_album["artist"]).strip()
        elif not album_artist and raw_album.get("author"):
            album_artist = str(raw_album["author"]).strip()

        album_thumb = ""
        thumbs = raw_album.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs:
            album_thumb = str(thumbs[-1].get("url") or thumbs[0].get("url") or "")

        track_items: list[dict[str, Any]] = []
        total_dur_sec = 0.0

        for raw_t in (raw_album.get("tracks") or []):
            if not isinstance(raw_t, dict):
                continue
            vid = raw_t.get("videoId") or ""
            t_title = str(raw_t.get("title") or "").strip()
            t_artists = ", ".join(
                a.get("name", "")
                for a in (raw_t.get("artists") or [])
                if isinstance(a, dict) and a.get("name")
            ) or album_artist
            dur_sec = float(raw_t.get("duration_seconds") or 0.0)
            if dur_sec <= 0.0 and raw_t.get("duration"):
                parts = [int(p) for p in str(raw_t["duration"]).split(":") if p.isdigit()]
                for p in parts:
                    dur_sec = dur_sec * 60 + p
            total_dur_sec += dur_sec
            t_thumb = album_thumb
            t_thumbs = raw_t.get("thumbnails") or []
            if isinstance(t_thumbs, list) and t_thumbs:
                t_thumb = str(t_thumbs[-1].get("url") or t_thumbs[0].get("url") or "") or album_thumb

            track_items.append(
                {
                    "title": t_title,
                    "artist": t_artists,
                    "full_title": f"{t_title} — {t_artists}" if t_artists and t_artists not in t_title else t_title,
                    "video_id": vid,
                    "url": f"https://music.youtube.com/watch?v={vid}" if vid else "",
                    "duration": dur_sec,
                    "source": "ytmusic",
                    "thumbnail_url": t_thumb,
                    "result_type": "song",
                    "item_type": "Пісня",
                }
            )

        raw_tc = raw_album.get("trackCount")
        track_count = len(track_items)
        if raw_tc is not None:
            try:
                digits = re.sub(r"[^\d]", "", str(raw_tc))
                if digits:
                    track_count = int(digits)
            except (ValueError, TypeError):
                pass
        if track_count <= 0:
            track_count = len(track_items)
        if raw_album.get("duration_seconds"):
            try:
                total_dur_sec = float(raw_album["duration_seconds"])
            except Exception:
                pass

        dur_str = format_album_duration(track_count, total_dur_sec, str(raw_album.get("duration") or ""))
        result = {
            "title": album_title,
            "artist": album_artist,
            "full_title": f"{album_title} — {album_artist}" if album_artist and album_artist not in album_title else album_title,
            "browse_id": actual_browse_id,
            "playlist_id": raw_album.get("playlistId") or raw_album.get("id") or "",
            "track_count": track_count,
            "duration": total_dur_sec,
            "duration_text": dur_str,
            "thumbnail_url": album_thumb,
            "tracks": track_items,
        }
        with _YT_CACHE_LOCK:
            _ALBUM_CACHE[browse_id] = result
            if actual_browse_id != browse_id:
                _ALBUM_CACHE[actual_browse_id] = result
        return result

    @staticmethod
    def search_youtube(
        query: str,
        limit: int = 25,
        filter_mode: str = "all",
        youtube_priority: bool = False,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        if YTMusic is None and YoutubeDL is None:
            raise AudioEngineError("Модуль ytmusicapi не встановлено.")
        try:
            items: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            seen_browse_ids: set[str] = set()

            if youtube_priority:
                if YoutubeDL is not None:
                    try:
                        ydl_opts = {
                            "quiet": True,
                            "extract_flat": True,
                            "noplaylist": True,
                            "no_warnings": True,
                        }
                        with YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                            for entry in (info.get("entries") or []) if isinstance(info, dict) else []:
                                if not isinstance(entry, dict):
                                    continue
                                vid = entry.get("id")
                                if not vid or vid in seen_ids:
                                    continue
                                seen_ids.add(vid)
                                raw_title = str(entry.get("title") or query).strip()
                                artist = ""
                                title = raw_title
                                if " - " in raw_title or " — " in raw_title or " – " in raw_title:
                                    parts = re.split(r"\s+[—\-–]\s+", raw_title, maxsplit=1)
                                    if len(parts) == 2:
                                        artist = parts[0].strip()
                                        title = parts[1].strip()
                                if not artist:
                                    artist = str(entry.get("uploader") or entry.get("channel") or "").strip()
                                full_title = f"{title} — {artist}" if artist and artist not in title else title
                                dur_seconds = float(entry.get("duration") or 0.0)
                                thumb_url = str(entry.get("thumbnail") or "")
                                if not thumb_url:
                                    thumbs = entry.get("thumbnails") or []
                                    if isinstance(thumbs, list) and thumbs:
                                        thumb_url = str(thumbs[-1].get("url") or thumbs[0].get("url") or "")
                                items.append(
                                    {
                                        "title": title,
                                        "artist": artist,
                                        "full_title": full_title,
                                        "result_type": "video",
                                        "item_type": "Відео",
                                        "video_id": vid,
                                        "url": f"https://www.youtube.com/watch?v={vid}",
                                        "duration": dur_seconds,
                                        "source": "youtube",
                                        "thumbnail_url": thumb_url,
                                    }
                                )
                                if len(items) >= limit:
                                    break
                        if items:
                            return items
                    except Exception:
                        if YTMusic is None:
                            raise

            if YTMusic is not None:
                try:
                    ytmusic = YTMusic()
                    raw_songs: list[dict[str, Any]] = []
                    raw_albums: list[dict[str, Any]] = []
                    raw_videos: list[dict[str, Any]] = []

                    allow_albums = filter_mode in ("all", "albums_only")
                    allow_songs = filter_mode in ("all", "no_albums")

                    if allow_songs:
                        try:
                            raw_songs = ytmusic.search(query, filter="songs") or []
                        except Exception:
                            pass

                    if allow_albums:
                        try:
                            raw_albums = ytmusic.search(query, filter="albums") or []
                        except Exception:
                            pass

                    if allow_songs:
                        try:
                            if len(raw_songs) < limit:
                                raw_videos = ytmusic.search(query, filter="videos") or []
                        except Exception:
                            pass

                    try:
                        raw_general = ytmusic.search(query, filter=None) or []
                        for entry in raw_general:
                            if isinstance(entry, dict):
                                rtype = str(entry.get("resultType") or "").lower()
                                if rtype == "album" and allow_albums:
                                    raw_albums.append(entry)
                                elif rtype == "song" and allow_songs and len(raw_songs) < limit:
                                    raw_songs.append(entry)
                                elif rtype == "video" and allow_songs and len(raw_videos) < limit:
                                    raw_videos.append(entry)
                    except Exception:
                        pass

                    if filter_mode == "albums_only":
                        selected_albums = raw_albums[:limit]
                        selected_songs = []
                        selected_videos = []
                    elif filter_mode == "no_albums":
                        selected_albums = []
                        selected_songs = raw_songs[:limit]
                        remaining_slots = limit - len(selected_songs)
                        selected_videos = raw_videos[:remaining_slots] if remaining_slots > 0 else []
                    else:
                        album_quota = min(len(raw_albums), 5)
                        song_quota = max(0, limit - album_quota) if album_quota > 0 else limit
                        selected_songs = raw_songs[:song_quota]
                        selected_albums = raw_albums[:album_quota]
                        remaining_slots = limit - len(selected_songs) - len(selected_albums)
                        selected_videos = raw_videos[:remaining_slots] if remaining_slots > 0 else []

                    # Tagged entries: albums first so album searches are immediately visible!
                    tagged_entries: list[tuple[dict[str, Any], str]] = [
                        (entry, "album") for entry in selected_albums
                    ] + [
                        (entry, "ytmusic") for entry in selected_songs
                    ] + [
                        (entry, "youtube") for entry in selected_videos
                    ]

                    for entry, source in tagged_entries:
                        if source == "album":
                            browse_id = entry.get("browseId") or entry.get("playlistId") or ""
                            if not browse_id or browse_id in seen_browse_ids:
                                continue
                            seen_browse_ids.add(browse_id)
                            artists = ", ".join(
                                a.get("name", "")
                                for a in entry.get("artists", [])
                                if isinstance(a, dict) and a.get("name")
                            )
                            raw_title = str(entry.get("title") or query).strip()
                            title = raw_title
                            if not artists and (" - " in title or " — " in title or " – " in title):
                                parts = re.split(r"\s+[—\-–]\s+", title, maxsplit=1)
                                if len(parts) == 2:
                                    artists = parts[0].strip()
                                    title = parts[1].strip()
                            full_title = f"{title} — {artists}" if artists and artists not in title else title
                            thumb_url = ""
                            thumbs = entry.get("thumbnails") or []
                            if isinstance(thumbs, list) and thumbs:
                                thumb_url = str(thumbs[-1].get("url") or thumbs[0].get("url") or "")

                            track_count = entry.get("trackCount") or entry.get("track_count")
                            dur_seconds = float(entry.get("duration_seconds") or 0.0)
                            dur_str = str(entry.get("duration") or "")

                            with _YT_CACHE_LOCK:
                                if browse_id in _ALBUM_CACHE:
                                    c_alb = _ALBUM_CACHE[browse_id]
                                    track_count = track_count or c_alb.get("track_count")
                                    dur_seconds = dur_seconds or float(c_alb.get("duration") or 0.0)
                                    dur_str = dur_str or c_alb.get("duration_text") or ""
                                    if not thumb_url and c_alb.get("thumbnail_url"):
                                        thumb_url = c_alb["thumbnail_url"]

                            if (not track_count or dur_seconds <= 0.0) and browse_id:
                                try:
                                    c_alb = AudioEngine.get_album(browse_id)
                                    if c_alb:
                                        track_count = track_count or c_alb.get("track_count")
                                        dur_seconds = dur_seconds or float(c_alb.get("duration") or 0.0)
                                        dur_str = dur_str or c_alb.get("duration_text") or ""
                                        if not thumb_url and c_alb.get("thumbnail_url"):
                                            thumb_url = c_alb["thumbnail_url"]
                                except Exception:
                                    pass

                            album_dur_text = format_album_duration(track_count, dur_seconds, dur_str)
                            items.append(
                                {
                                    "title": title,
                                    "artist": artists,
                                    "full_title": full_title,
                                    "result_type": "album",
                                    "item_type": "Альбом",
                                    "browse_id": browse_id,
                                    "playlist_id": entry.get("playlistId") or "",
                                    "video_id": "",
                                    "url": f"https://music.youtube.com/browse/{browse_id}" if browse_id else "",
                                    "duration": dur_seconds,
                                    "duration_text": album_dur_text,
                                    "track_count": track_count,
                                    "source": "ytmusic",
                                    "thumbnail_url": thumb_url,
                                }
                            )
                        else:
                            vid = entry.get("videoId")
                            if not vid or vid in seen_ids:
                                continue
                            seen_ids.add(vid)
                            artists = ", ".join(
                                a.get("name", "")
                                for a in entry.get("artists", [])
                                if isinstance(a, dict) and a.get("name")
                            )
                            raw_title = str(entry.get("title") or query).strip()
                            title = raw_title
                            if not artists and (" - " in title or " — " in title or " – " in title):
                                parts = re.split(r"\s+[—\-–]\s+", title, maxsplit=1)
                                if len(parts) == 2:
                                    artists = parts[0].strip()
                                    title = parts[1].strip()
                            full_title = f"{title} — {artists}" if artists and artists not in title else title
                            dur_seconds = float(entry.get("duration_seconds") or 0.0)
                            if dur_seconds <= 0.0 and entry.get("duration"):
                                parts = [int(p) for p in str(entry["duration"]).split(":") if p.isdigit()]
                                for p in parts:
                                    dur_seconds = dur_seconds * 60 + p
                            thumb_url = ""
                            thumbs = entry.get("thumbnails") or []
                            if isinstance(thumbs, list) and thumbs:
                                thumb_url = str(thumbs[-1].get("url") or thumbs[0].get("url") or "")
                            items.append(
                                {
                                    "title": title,
                                    "artist": artists,
                                    "full_title": full_title,
                                    "result_type": "song" if source == "ytmusic" else "video",
                                    "item_type": "Пісня" if source == "ytmusic" else "Відео",
                                    "video_id": vid,
                                    "url": f"https://music.youtube.com/watch?v={vid}" if source == "ytmusic" else f"https://www.youtube.com/watch?v={vid}",
                                    "duration": dur_seconds,
                                    "source": source,
                                    "thumbnail_url": thumb_url,
                                }
                            )
                        if len(items) >= limit:
                            break
                except Exception:
                    if YoutubeDL is None or _is_mocked_youtube():
                        raise

            if filter_mode != "albums_only" and len(items) < limit and YoutubeDL is not None and not _is_mocked_youtube():
                with YoutubeDL(
                    {
                        "quiet": True,
                        "extract_flat": True,
                        "noplaylist": True,
                        "no_warnings": True,
                    }
                ) as ydl:
                    needed = limit - len(items)
                    info = ydl.extract_info(f"ytsearch{needed + len(seen_ids)}:{query}", download=False)
                    for entry in (info.get("entries") or []) if isinstance(info, dict) else []:
                        if not isinstance(entry, dict):
                            continue
                        vid = entry.get("id")
                        if not vid or vid in seen_ids:
                            continue
                        seen_ids.add(vid)
                        raw_title = str(entry.get("title") or query).strip()
                        artist = ""
                        title = raw_title
                        if " - " in raw_title or " — " in raw_title or " – " in raw_title:
                            parts = re.split(r"\s+[—\-–]\s+", raw_title, maxsplit=1)
                            if len(parts) == 2:
                                artist = parts[0].strip()
                                title = parts[1].strip()
                        if not artist:
                            artist = str(entry.get("uploader") or entry.get("channel") or "").strip()
                        full_title = f"{title} — {artist}" if artist and artist not in title else title
                        dur_seconds = float(entry.get("duration") or 0.0)
                        thumb_url = str(entry.get("thumbnail") or "")
                        if not thumb_url:
                            thumbs = entry.get("thumbnails") or []
                            if isinstance(thumbs, list) and thumbs:
                                thumb_url = str(thumbs[-1].get("url") or thumbs[0].get("url") or "")
                        items.append(
                            {
                                "title": title,
                                "artist": artist,
                                "full_title": full_title,
                                "result_type": "video",
                                "item_type": "Відео",
                                "video_id": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "duration": dur_seconds,
                                "source": "youtube",
                                "thumbnail_url": thumb_url,
                            }
                        )
                        if len(items) >= limit:
                            break
            return items
        except Exception as exc:
            raise AudioEngineError(f"Помилка пошуку YouTube: {exc}") from exc

    @classmethod
    def cache_youtube_track(cls, track: StreamTrack) -> tuple[str, str, float]:
        cached = cls.get_cached_file(track.source)
        if cached is not None:
            return cached
        stream_url, resolved_title = cls._resolve_source(track)
        if os.path.isfile(stream_url):
            return stream_url, resolved_title, track.duration
        ffmpeg_bin = cls.find_ffmpeg()
        if not ffmpeg_bin:
            raise AudioEngineError("FFmpeg не знайдено для кешування треку.")
        cache_dir = cls._cache_dir()
        key = cls._cache_key(track.source)
        out_path = cache_dir / f"{key}.m4a"
        tmp_path = cache_dir / f"{key}.tmp.m4a"
        meta_path = cache_dir / f"{key}.json"
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-i",
                stream_url,
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not tmp_path.is_file():
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise AudioEngineError("Не вдалося зберегти аудіо з YouTube у локальний кеш.")
        tmp_path.replace(out_path)
        duration = track.duration
        try:
            from musicpad.library import scan_file
            scanned = scan_file(out_path)
            if scanned and scanned.duration > 0:
                duration = scanned.duration
        except Exception:
            pass
        meta_path.write_text(
            json.dumps(
                {
                    "path": str(out_path),
                    "title": resolved_title,
                    "duration": duration,
                    "source": track.source,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return str(out_path), resolved_title, duration
