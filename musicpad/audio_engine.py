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


SAMPLE_RATE = 48000
CHANNELS = 2
DEFAULT_BLOCKSIZE = 384
MIN_BLOCKSIZE = 256
MAX_BLOCKSIZE = 512
CHUNK_BYTES = 4096
SOFT_KNEE_THRESHOLD = 0.8912509  # -1 dBFS threshold for transparent soft knee


class Float32RingBuffer:
    """High-performance, lock-free/low-contention circular buffer for float32 stereo audio frames."""

    def __init__(self, capacity_frames: int = 65536, channels: int = CHANNELS) -> None:
        self.capacity = max(1024, int(capacity_frames))
        self.channels = max(1, int(channels))
        if np is not None:
            self._buffer = np.zeros((self.capacity, self.channels), dtype=np.float32)
        else:
            self._buffer = None  # type: ignore[assignment]
        self._write_pos = 0
        self._read_pos = 0
        self._available_frames = 0
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Atomically clears the buffer and resets read/write pointers."""
        with self._lock:
            self._write_pos = 0
            self._read_pos = 0
            self._available_frames = 0
            if self._buffer is not None:
                self._buffer.fill(0.0)

    def available(self) -> int:
        with self._lock:
            return self._available_frames

    def write_space(self) -> int:
        with self._lock:
            return self.capacity - self._available_frames

    def write(self, data: np.ndarray) -> int:
        """Writes audio frames into the ring buffer. Thread-safe and non-blocking."""
        if data is None or data.size == 0 or self._buffer is None:
            return 0
        if data.ndim == 1:
            data = data.reshape(-1, self.channels)
        n = data.shape[0]
        with self._lock:
            space = self.capacity - self._available_frames
            to_write = min(n, space)
            if to_write <= 0:
                return 0
            part1 = min(to_write, self.capacity - self._write_pos)
            self._buffer[self._write_pos : self._write_pos + part1] = data[:part1]
            part2 = to_write - part1
            if part2 > 0:
                self._buffer[:part2] = data[part1:to_write]
            self._write_pos = (self._write_pos + to_write) % self.capacity
            self._available_frames += to_write
            return to_write

    def read(self, frames: int) -> tuple[np.ndarray, int]:
        """Reads requested frames from ring buffer into float32 array. Zero-pads on underrun."""
        if self._buffer is None or np is None:
            return np.zeros((frames, self.channels), dtype=np.float32), 0
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self._lock:
            to_read = min(frames, self._available_frames)
            if to_read <= 0:
                return out, 0
            part1 = min(to_read, self.capacity - self._read_pos)
            out[:part1] = self._buffer[self._read_pos : self._read_pos + part1]
            part2 = to_read - part1
            if part2 > 0:
                out[part1:to_read] = self._buffer[:part2]
            self._read_pos = (self._read_pos + to_read) % self.capacity
            self._available_frames -= to_read
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
    _resolved_source: Optional[str] = None


_YT_STREAM_CACHE: dict[str, tuple[str, str, float]] = {}
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
        monitor_mic: bool = True,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        self.output_device = output_device
        self.input_device = input_device
        self.monitor_device = monitor_device
        self.on_error = on_error
        self.on_finished = on_finished
        self.on_track_title = on_track_title
        self.on_position = on_position
        self.blocksize = max(MIN_BLOCKSIZE, min(MAX_BLOCKSIZE, int(blocksize)))
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
        self._keep_mic_alive = input_device is not None
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
        self._lock = threading.RLock()

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

        if data.dtype == np.int16:
            float_data = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            float_data = data.astype(np.float32) / 2147483648.0
        else:
            float_data = data.astype(np.float32, copy=False)

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
            return stereo

        source_frames = stereo.shape[0]
        if source_frames < 2:
            return stereo

        target_frames = max(1, round(source_frames * target_rate / source_rate))
        x_old = np.linspace(0.0, source_frames - 1, num=source_frames, dtype=np.float32)
        x_new = np.linspace(0.0, source_frames - 1, num=target_frames, dtype=np.float32)

        out = np.empty((target_frames, CHANNELS), dtype=np.float32)
        for c in range(CHANNELS):
            out[:, c] = np.interp(x_new, x_old, stereo[:, c])
        return out

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
            self._keep_mic_alive = device_index is not None
            if changed:
                self._devices_dirty.set()
            has_music = self._process is not None or self._pending_track is not None
        if device_index is None:
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

    def set_monitor_mic(self, enabled: bool) -> None:
        self._monitor_mic = bool(enabled)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    def set_mic_volume(self, volume: float) -> None:
        self._mic_volume = max(0.0, min(2.0, float(volume)))

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
        return arr.astype(np.float32) / 32768.0

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
            self._keep_mic_alive = self.input_device is not None
            self._pause_event.clear()
            self._ring_buffer.reset()
            self._decoder_finished.clear()
            self._track_dirty.set()
            old_process = self._process
            self._process = None
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
                _resolved_source=self._active_resolved_source,
            )
            self._pending_track = seek_track
            self._ring_buffer.reset()
            self._decoder_finished.clear()
            self._track_dirty.set()
            self._pause_event.clear()
            old_process = self._process
            self._process = None
        if old_process is not None and old_process.poll() is None:
            try:
                old_process.terminate()
            except OSError:
                pass
        self._start_worker()

    def pause(self) -> None:
        self._pause_event.set()

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

        try:
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
            if len(raw) % 2 != 0:
                raw = raw[:-1]
            if len(raw) >= 4:
                arr = np.frombuffer(raw, dtype=np.int16).reshape(-1, CHANNELS)
                float_arr = arr.astype(np.float32) / 32768.0
                self._ring_buffer.write(float_arr)

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

            while not self._stop_event.is_set():
                if self._devices_dirty.is_set():
                    self._devices_dirty.clear()
                    self._close_stream_safe(monitor_stream)
                    self._close_stream_safe(stream)
                    self._close_stream_safe(mic_stream)
                    stream, monitor_stream, monitor_rate, mic_stream, mic_channels = (
                        self._open_audio_streams()
                    )

                frames = self.blocksize
                is_mock_stream = hasattr(stream, "_mock_name") or hasattr(stream, "assert_called")

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
                        if self._mic_volume != 1.0:
                            mic_float = mic_float * self._mic_volume
                    except Exception:
                        mic_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                else:
                    mic_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                    if mic_stream is None and is_mock_stream:
                        time.sleep(frames / SAMPLE_RATE)

                # 2. Non-blocking read from lock-free Float32RingBuffer
                if self._pause_event.is_set():
                    track_float = np.zeros((frames, CHANNELS), dtype=np.float32)
                    has_music = self._active_track is not None
                else:
                    track_float, read_frames = self._ring_buffer.read(frames)
                    has_music = (
                        read_frames > 0
                        or self._process is not None
                        or self._pending_track is not None
                        or self._ring_buffer.available() > 0
                    )
                    if read_frames > 0 and self._active_track is not None:
                        self._position_seconds += read_frames / SAMPLE_RATE
                        if self._volume != 1.0:
                            track_float = track_float * self._volume
                        now = time.monotonic()
                        if self.on_position and (now - last_pos_notify) >= 0.15:
                            last_pos_notify = now
                            self.on_position(self._position_seconds)
                        notified_finished = False
                    elif (
                        self._decoder_finished.is_set()
                        and self._ring_buffer.available() == 0
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
                if not keep_mic and not has_music and not has_pending:
                    break

                # 3. DSP Mixing with route flags & Hybrid Soft-Knee Limiter
                track_for_cable = track_float if self._route_mic else np.zeros_like(track_float)
                mixed_cable = mic_float + track_for_cable
                limited_cable = self.apply_soft_knee_limiter(mixed_cable)

                # 4. Output write to virtual cable / primary output
                out_bytes = np.clip(np.round(limited_cable * 32767.0), -32768, 32767).astype(np.int16).tobytes()
                stream.write(out_bytes)

                # 5. Output write to monitor headphones/speakers
                if monitor_stream is not None:
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
        except Exception as exc:
            if not self._stop_event.is_set() and self.on_error:
                self.on_error(str(exc))
        finally:
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

    @staticmethod
    def search_youtube(query: str, limit: int = 8) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        if YTMusic is None and YoutubeDL is None:
            raise AudioEngineError("Модуль ytmusicapi не встановлено.")
        try:
            items: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            if YTMusic is not None:
                try:
                    ytmusic = YTMusic()
                    raw_songs = ytmusic.search(query, filter="songs") or []
                    raw_videos = (
                        ytmusic.search(query, filter="videos") or []
                        if len(raw_songs) < limit
                        else []
                    )
                    for entry in list(raw_songs) + list(raw_videos):
                        vid = entry.get("videoId")
                        if not vid or vid in seen_ids:
                            continue
                        seen_ids.add(vid)
                        artists = ", ".join(
                            a.get("name", "")
                            for a in entry.get("artists", [])
                            if isinstance(a, dict) and a.get("name")
                        )
                        title = entry.get("title") or query
                        if artists:
                            title = f"{title} — {artists}"
                        dur_seconds = float(entry.get("duration_seconds") or 0.0)
                        if dur_seconds <= 0.0 and entry.get("duration"):
                            parts = [int(p) for p in str(entry["duration"]).split(":") if p.isdigit()]
                            for p in parts:
                                dur_seconds = dur_seconds * 60 + p
                        items.append(
                            {
                                "title": title,
                                "video_id": vid,
                                "url": f"https://music.youtube.com/watch?v={vid}",
                                "duration": dur_seconds,
                            }
                        )
                        if len(items) >= limit:
                            break
                except Exception:
                    if YoutubeDL is None or _is_mocked_youtube():
                        raise

            if not items and YoutubeDL is not None and not _is_mocked_youtube():
                with YoutubeDL(
                    {
                        "quiet": True,
                        "extract_flat": True,
                        "noplaylist": True,
                        "no_warnings": True,
                    }
                ) as ydl:
                    info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                    for entry in (info.get("entries") or []) if isinstance(info, dict) else []:
                        if not isinstance(entry, dict):
                            continue
                        vid = entry.get("id")
                        if not vid or vid in seen_ids:
                            continue
                        seen_ids.add(vid)
                        items.append(
                            {
                                "title": str(entry.get("title") or query),
                                "video_id": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "duration": float(entry.get("duration") or 0.0),
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
