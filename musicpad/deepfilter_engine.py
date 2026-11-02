"""DeepFilterNet neural noise reduction engine for MusicPad.

Provides real-time noise suppression using deepfilter.dll and DeepFilterNet3 ONNX model.
Zero PyTorch dependency: runs lightweight native inference in ~0.58 ms per 10 ms frame.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import threading
from typing import Optional
import numpy as np


def _find_deepfilter_assets() -> tuple[Optional[Path], Optional[Path]]:
    """Locate deepfilter.dll and DeepFilterNet3_onnx.tar.gz in package or bundle."""
    # 1. Try deepfilternet_slim package
    try:
        import deepfilternet_slim as dfs
        lib_path = Path(dfs.library_path())
        model_path = Path(dfs.model_path())
        if lib_path.is_file() and model_path.is_file():
            return lib_path, model_path
    except Exception:
        pass

    # 2. Check PyInstaller _MEIPASS bundle
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates = [
            (base / "deepfilter.dll", base / "DeepFilterNet3_onnx.tar.gz"),
            (base / "deepfilter" / "deepfilter.dll", base / "deepfilter" / "DeepFilterNet3_onnx.tar.gz"),
            (base / "_native" / "deepfilter.dll", base / "_models" / "DeepFilterNet3_onnx.tar.gz"),
        ]
        for lib_cand, model_cand in candidates:
            if lib_cand.is_file() and model_cand.is_file():
                return lib_cand, model_cand

    # 3. Check repo root / site-packages
    root = Path(__file__).resolve().parent.parent
    local_candidates = [
        (
            root / "native" / "bin" / "deepfilter.dll",
            root / "native" / "bin" / "DeepFilterNet3_onnx.tar.gz",
        ),
        (
            Path(r"C:\Program Files\MusicPad\deepfilter.dll"),
            Path(r"C:\Program Files\MusicPad\DeepFilterNet3_onnx.tar.gz"),
        ),
        (
            Path(r"C:\ProgramData\MusicPad\deepfilter.dll"),
            Path(r"C:\ProgramData\MusicPad\DeepFilterNet3_onnx.tar.gz"),
        ),
    ]
    for lib_cand, model_cand in local_candidates:
        if lib_cand.is_file() and model_cand.is_file():
            return lib_cand, model_cand

    return None, None


class DeepFilterEngine:
    """Thread-safe real-time neural noise suppressor using DeepFilterNet3."""

    FRAME_SIZE = 480  # 10 ms at 48000 Hz

    def __init__(
        self,
        lib_path: Optional[Path | str] = None,
        model_path: Optional[Path | str] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._dll: Optional[ctypes.CDLL] = None
        self._state: Optional[int] = None
        self._fifo_in = np.zeros(0, dtype=np.float32)
        self._fifo_out = np.zeros(0, dtype=np.float32)

        resolved_lib, resolved_model = _find_deepfilter_assets()
        final_lib = Path(lib_path) if lib_path else resolved_lib
        final_model = Path(model_path) if model_path else resolved_model

        if final_lib and final_lib.is_file() and final_model and final_model.is_file():
            try:
                dll = ctypes.CDLL(str(final_lib))
                dll.df_create.argtypes = [ctypes.c_char_p, ctypes.c_float, ctypes.c_char_p]
                dll.df_create.restype = ctypes.c_void_p

                dll.df_process_frame.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.POINTER(ctypes.c_float),
                ]
                dll.df_process_frame.restype = ctypes.c_float

                dll.df_free.argtypes = [ctypes.c_void_p]
                dll.df_free.restype = None

                model_str = str(final_model).encode("utf-8")
                st = dll.df_create(model_str, ctypes.c_float(100.0), b"error")
                if st:
                    self._dll = dll
                    self._state = st
            except Exception:
                self._dll = None
                self._state = None

    @property
    def is_available(self) -> bool:
        """Return True if the DeepFilterNet native model is loaded and ready."""
        return self._dll is not None and self._state is not None

    def reset(self) -> None:
        """Clear internal FIFO buffers."""
        with self._lock:
            self._fifo_in = np.zeros(0, dtype=np.float32)
            self._fifo_out = np.zeros(0, dtype=np.float32)

    def close(self) -> None:
        """Free native resources."""
        with self._lock:
            if self._dll is not None and self._state is not None:
                try:
                    self._dll.df_free(ctypes.c_void_p(self._state))
                except Exception:
                    pass
                self._state = None
                self._dll = None

    def __del__(self) -> None:
        self.close()

    def process_buffer(
        self,
        audio: np.ndarray,
        wet: float = 1.0,
    ) -> np.ndarray:
        """Process microphone audio buffer with wet/dry balance.

        Args:
            audio: float32 NumPy array of shape (frames, channels) or (frames,).
            wet: Wet/dry balance in [0.0, 1.0] (0.0 = bypass, 1.0 = fully cleaned).

        Returns:
            Processed float32 audio matching input shape.
        """
        if not self.is_available or wet <= 0.0 or audio.size == 0:
            return audio

        clamped_wet = float(max(0.0, min(1.0, wet)))

        orig_shape = audio.shape
        dry = np.ascontiguousarray(audio, dtype=np.float32)

        # Extract mono signal for neural processing
        if dry.ndim == 2:
            n_frames = dry.shape[0]
            n_ch = dry.shape[1]
            mono = dry[:, 0] if n_ch == 1 else np.mean(dry, axis=1, dtype=np.float32)
        else:
            n_frames = dry.shape[0]
            n_ch = 1
            mono = dry

        with self._lock:
            if self._state is None or self._dll is None:
                return audio

            # Append mono frames to input FIFO
            self._fifo_in = np.concatenate([self._fifo_in, mono])

            # Process all complete 480-sample frames
            while len(self._fifo_in) >= self.FRAME_SIZE:
                in_chunk = np.ascontiguousarray(self._fifo_in[: self.FRAME_SIZE], dtype=np.float32)
                self._fifo_in = self._fifo_in[self.FRAME_SIZE :]
                out_chunk = np.zeros(self.FRAME_SIZE, dtype=np.float32)

                in_ptr = in_chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
                out_ptr = out_chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

                self._dll.df_process_frame(ctypes.c_void_p(self._state), in_ptr, out_ptr)
                self._fifo_out = np.concatenate([self._fifo_out, out_chunk])

            # Deliver requested number of frames
            if len(self._fifo_out) >= n_frames:
                clean_mono = self._fifo_out[:n_frames]
                self._fifo_out = self._fifo_out[n_frames:]
            else:
                # Still filling initial lookahead/FIFO: pass dry mono
                clean_mono = mono

        # Blend wet / dry
        if clamped_wet < 1.0:
            processed_mono = (1.0 - clamped_wet) * mono + clamped_wet * clean_mono
        else:
            processed_mono = clean_mono

        # Reshape to match input
        if dry.ndim == 2:
            if n_ch == 1:
                return processed_mono.reshape(-1, 1)
            # Duplicate mono across channels (or blend per channel)
            if clamped_wet < 1.0:
                wet_clean_stereo = np.repeat(clean_mono[:, np.newaxis], n_ch, axis=1)
                return (1.0 - clamped_wet) * dry + clamped_wet * wet_clean_stereo
            return np.repeat(clean_mono[:, np.newaxis], n_ch, axis=1)

        return processed_mono
