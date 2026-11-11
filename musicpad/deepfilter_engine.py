"""DeepFilterNet neural noise reduction engine for MusicPad.

Provides real-time noise suppression using deepfilter.dll and DeepFilterNet3 ONNX model.
Zero PyTorch dependency: runs lightweight native inference in ~0.58 ms per 10 ms frame.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import threading
from typing import Callable, Optional
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
        auto_load: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._dll: Optional[ctypes.CDLL] = None
        self._state: Optional[int] = None
        self._fifo_in = np.zeros(0, dtype=np.float32)
        self._fifo_out = np.zeros(0, dtype=np.float32)
        self._lib_path = lib_path
        self._model_path = model_path
        self._is_loading = False
        self._load_error: Optional[str] = None
        self._load_thread: Optional[threading.Thread] = None
        self._callbacks: list[Callable[[bool, Optional[str]], None]] = []

        if auto_load:
            self.load_model()

    @property
    def is_available(self) -> bool:
        """Return True if the DeepFilterNet native model is loaded and ready."""
        with self._lock:
            return self._dll is not None and self._state is not None

    @property
    def is_loading(self) -> bool:
        """Return True if background model loading is currently in progress."""
        with self._lock:
            return self._is_loading

    @property
    def load_error(self) -> Optional[str]:
        """Return last load error description, or None if successful/idle."""
        with self._lock:
            return self._load_error

    def load_model(self) -> bool:
        """Load deepfilter.dll and DeepFilterNet3 ONNX model into memory.

        Thread-safe and idempotent. Returns True on success, False on failure.
        """
        with self._lock:
            if self._dll is not None and self._state is not None:
                return True
            self._load_error = None

        resolved_lib, resolved_model = _find_deepfilter_assets()
        final_lib = Path(self._lib_path) if self._lib_path else resolved_lib
        final_model = Path(self._model_path) if self._model_path else resolved_model

        if not (final_lib and final_lib.is_file() and final_model and final_model.is_file()):
            with self._lock:
                self._load_error = "DeepFilterNet library or model asset not found on disk"
            return False

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
            if not st:
                with self._lock:
                    self._load_error = "Failed to initialize DeepFilterNet native state (df_create returned NULL)"
                return False

            with self._lock:
                self._dll = dll
                self._state = st
                self._load_error = None
            return True
        except Exception as e:
            with self._lock:
                self._dll = None
                self._state = None
                self._load_error = str(e)
            return False

    def load_model_async(
        self,
        on_complete: Optional[Callable[[bool, Optional[str]], None]] = None,
    ) -> threading.Thread:
        """Load DeepFilterNet model in a background daemon thread.

        If already loaded, on_complete is invoked immediately.
        If already loading, on_complete is queued and will be invoked upon completion.
        Never blocks the caller.
        """
        with self._lock:
            if self._dll is not None and self._state is not None:
                if on_complete:
                    threading.Thread(
                        target=on_complete,
                        args=(True, None),
                        name="DeepFilterNetDoneCallback",
                        daemon=True,
                    ).start()
                return self._load_thread or threading.current_thread()

            if on_complete:
                self._callbacks.append(on_complete)

            if self._is_loading and self._load_thread is not None and self._load_thread.is_alive():
                return self._load_thread

            self._is_loading = True

        def _worker() -> None:
            try:
                success = self.load_model()
                with self._lock:
                    err = self._load_error if not success else None
            except Exception as ex:
                success = False
                err = str(ex)
                with self._lock:
                    self._load_error = err

            with self._lock:
                self._is_loading = False
                cbs = list(self._callbacks)
                self._callbacks.clear()

            for cb in cbs:
                try:
                    cb(success, err)
                except Exception:
                    pass

        t = threading.Thread(target=_worker, name="DeepFilterNetLoader", daemon=True)
        with self._lock:
            self._load_thread = t
        t.start()
        return t

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
            self._fifo_in = np.zeros(0, dtype=np.float32)
            self._fifo_out = np.zeros(0, dtype=np.float32)
            self._callbacks.clear()

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
