"""Shared DSP Core wrapper for MusicPad (ctypes binding to musicpad_dsp.dll + NumPy fallback).

Used by Virtual Cable routing mode so voice processing (gain, mute, voice FX) and mixing
are identical to the native C++ APO in Microphone Injection mode.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys
from typing import Optional
import numpy as np


MPAD_FX_NONE = 0
MPAD_FX_DEEPFILTER = 1 << 0
MPAD_FX_WHITE_NOISE = 1 << 1
MPAD_FX_VOICE_BOOST = 1 << 2

# Legacy aliases
MPAD_FX_HPF = MPAD_FX_WHITE_NOISE
MPAD_FX_NR = MPAD_FX_DEEPFILTER
MPAD_FX_AGC2 = MPAD_FX_VOICE_BOOST


def _find_dsp_dll() -> Optional[Path]:
    """Locate musicpad_dsp.dll or musicpad_apo.dll in bundle or workspace."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates.extend([base / "musicpad_dsp.dll", base / "native" / "bin" / "musicpad_dsp.dll"])

    repo_root = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            repo_root / "native" / "bin" / "musicpad_dsp.dll",
            repo_root / "native" / "bin" / "musicpad_apo.dll",
            Path(r"C:\Program Files\MusicPad\musicpad_dsp.dll"),
            Path(r"C:\Program Files\MusicPad\musicpad_apo.dll"),
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


class DspCore:
    """Shared voice processing and track mixing core."""

    def __init__(self, dll_path: Optional[Path | str] = None) -> None:
        self._dll = None
        resolved = Path(dll_path) if dll_path is not None else _find_dsp_dll()
        if resolved is not None and resolved.is_file():
            try:
                dll = ctypes.CDLL(str(resolved))
                dll.mpad_dsp_process_voice.argtypes = [
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_float,
                    ctypes.c_float,
                    ctypes.c_int,
                    ctypes.c_uint32,
                    ctypes.c_float,
                    ctypes.c_float,
                ]
                dll.mpad_dsp_process_voice.restype = None

                dll.mpad_dsp_mix_inplace.argtypes = [
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.c_uint32,
                ]
                dll.mpad_dsp_mix_inplace.restype = None

                dll.mpad_dsp_process_shm_block.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_float,
                    ctypes.c_int64,
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                dll.mpad_dsp_process_shm_block.restype = ctypes.c_int
                self._dll = dll
            except OSError:
                self._dll = None

    @property
    def is_native_loaded(self) -> bool:
        return self._dll is not None

    def process_voice(
        self,
        voice: np.ndarray,
        sample_rate: float = 48000.0,
        *,
        mic_gain: float = 1.0,
        mic_mute: bool = False,
        voice_fx_flags: int = 0,
        fx_param1: float = 0.0,
        fx_param2: float = 0.0,
    ) -> np.ndarray:
        """Process a float32 voice buffer (frames, channels) or 1D array in-place and return it."""
        arr = np.ascontiguousarray(voice, dtype=np.float32)
        if arr.ndim == 1:
            frames = int(arr.shape[0])
            channels = 1
        else:
            frames = int(arr.shape[0])
            channels = int(arr.shape[1])

        if frames == 0 or channels == 0:
            return arr

        if self._dll is not None:
            ptr = arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self._dll.mpad_dsp_process_voice(
                ptr,
                ctypes.c_uint32(frames),
                ctypes.c_uint32(channels),
                ctypes.c_float(sample_rate),
                ctypes.c_float(mic_gain),
                ctypes.c_int(1 if mic_mute else 0),
                ctypes.c_uint32(voice_fx_flags),
                ctypes.c_float(fx_param1),
                ctypes.c_float(fx_param2),
            )
            return arr

        if mic_mute:
            arr.fill(0.0)
            return arr

        boost_enabled = bool(voice_fx_flags & MPAD_FX_VOICE_BOOST)
        gain = float(max(0.0, min(16.0 if boost_enabled else 4.0, mic_gain)))
        if gain != 1.0:
            arr *= gain

        if boost_enabled:
            # Soft-knee limiter at -1.0 dBFS (0.89125)
            thresh = 0.8912509
            excess = np.maximum(0.0, np.abs(arr) - thresh)
            headroom = 1.0 - thresh
            compressed = thresh + headroom * np.tanh(excess / headroom)
            arr = np.where(np.abs(arr) > thresh, np.sign(arr) * np.minimum(0.999, compressed), arr)
        return arr

    def mix_voice_and_track(
        self,
        voice: Optional[np.ndarray],
        track: Optional[np.ndarray],
        frames: int,
        channels: int = 2,
        *,
        sample_rate: float = 48000.0,
        mic_gain: float = 1.0,
        mic_mute: bool = False,
        voice_fx_flags: int = 0,
    ) -> np.ndarray:
        """Process voice through the DSP chain and mix with track samples, clamped to [-1.0, 1.0]."""
        if voice is not None and not mic_mute:
            out = np.array(voice, dtype=np.float32, copy=True).reshape(frames, channels)
            out = self.process_voice(
                out,
                sample_rate=sample_rate,
                mic_gain=mic_gain,
                mic_mute=False,
                voice_fx_flags=voice_fx_flags,
            )
        else:
            out = np.zeros((frames, channels), dtype=np.float32)

        total_samples = frames * channels
        if self._dll is not None:
            out_ptr = out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            if track is not None:
                track_contig = np.ascontiguousarray(track, dtype=np.float32).reshape(frames, channels)
                trk_ptr = track_contig.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            else:
                trk_ptr = ctypes.cast(None, ctypes.POINTER(ctypes.c_float))
            self._dll.mpad_dsp_mix_inplace(out_ptr, trk_ptr, ctypes.c_uint32(total_samples))
            return out

        if track is not None:
            out += np.asarray(track, dtype=np.float32).reshape(frames, channels)
        np.clip(out, -1.0, 1.0, out=out)
        return out

    def simulate_apo_shm_block(
        self,
        shm_mmap,
        mic_in: np.ndarray,
        sample_rate: float = 48000.0,
        *,
        now_ns: int,
        last_flush_seq: int = 0,
    ) -> tuple[np.ndarray, int, int]:
        """Invoke the native C mpad_dsp_process_shm_block against an mmap buffer for testing."""
        in_arr = np.ascontiguousarray(mic_in, dtype=np.float32)
        if in_arr.ndim == 1:
            frames, channels = int(in_arr.shape[0]), 1
        else:
            frames, channels = int(in_arr.shape[0]), int(in_arr.shape[1])
        out_arr = np.zeros_like(in_arr, dtype=np.float32)

        if self._dll is None:
            raise RuntimeError("Native musicpad_dsp.dll is required for simulate_apo_shm_block")

        shm_buf = (ctypes.c_char * len(shm_mmap)).from_buffer(shm_mmap)
        in_ptr = in_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        out_ptr = out_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        flush_val = ctypes.c_uint32(last_flush_seq)

        try:
            active = self._dll.mpad_dsp_process_shm_block(
                ctypes.addressof(shm_buf),
                in_ptr,
                out_ptr,
                ctypes.c_uint32(frames),
                ctypes.c_uint32(channels),
                ctypes.c_float(sample_rate),
                ctypes.c_int64(now_ns),
                ctypes.byref(flush_val),
            )
        finally:
            del shm_buf
        return out_arr, int(active), int(flush_val.value)
