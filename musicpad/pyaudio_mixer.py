"""Real-time microphone and FFmpeg PCM mixer using PyAudio and NumPy."""

from __future__ import annotations

import threading
from array import array
from dataclasses import dataclass
from typing import BinaryIO, Callable, Optional

try:
    import numpy as np
except ModuleNotFoundError:
    np = None  # type: ignore[assignment]

try:
    import pyaudio
except ModuleNotFoundError:
    pyaudio = None  # type: ignore[assignment]


PYAUDIO_ERROR = getattr(pyaudio, "PyAudioError", RuntimeError)


SAMPLE_RATE = 48000
CHANNELS = 2
CHUNK = 1024
SAMPLE_WIDTH = 2
FRAME_BYTES = CHANNELS * SAMPLE_WIDTH


class PyAudioMixerError(RuntimeError):
    """An expected microphone or output-device configuration error."""


def mix_audio(
    music_bytes: bytes,
    mic_bytes: bytes,
    music_volume: float = 1.0,
    mic_enabled: bool = True,
) -> bytes:
    """Mix equal-length stereo int16 PCM chunks with saturation."""
    if np is not None:
        music_data = np.frombuffer(music_bytes, dtype=np.int16)
        music_scaled = np.asarray(music_data, dtype=np.float32) * music_volume
        music_scaled = music_scaled.astype(np.int32)
        if not mic_enabled:
            return np.clip(music_scaled, -32768, 32767).astype(np.int16).tobytes()

        mic_data = np.frombuffer(mic_bytes, dtype=np.int16)
        if len(mic_bytes) * CHANNELS == len(music_bytes):
            mic_data = np.repeat(mic_data, CHANNELS)
        elif len(music_bytes) != len(mic_bytes):
            return np.clip(music_scaled, -32768, 32767).astype(np.int16).tobytes()
        if len(mic_data) != len(music_data):
            return np.clip(music_scaled, -32768, 32767).astype(np.int16).tobytes()

        mixed = music_scaled + mic_data.astype(np.int32)
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()

    music_samples = array("h")
    music_samples.frombytes(music_bytes)
    vol = float(music_volume)
    scaled = [int(s * vol) for s in music_samples]
    if not mic_enabled:
        return array("h", (max(-32768, min(32767, v)) for v in scaled)).tobytes()

    if len(mic_bytes) * CHANNELS == len(music_bytes):
        raw_mic = array("h")
        raw_mic.frombytes(mic_bytes)
        mic_expanded: list[int] = []
        for s in raw_mic:
            mic_expanded.append(s)
            mic_expanded.append(s)
    elif len(music_bytes) == len(mic_bytes):
        raw_mic = array("h")
        raw_mic.frombytes(mic_bytes)
        mic_expanded = list(raw_mic)
    else:
        return array("h", (max(-32768, min(32767, v)) for v in scaled)).tobytes()

    if len(mic_expanded) != len(scaled):
        return array("h", (max(-32768, min(32767, v)) for v in scaled)).tobytes()

    return array(
        "h",
        (max(-32768, min(32767, m + c)) for m, c in zip(scaled, mic_expanded)),
    ).tobytes()


@dataclass(frozen=True)
class MixerDevices:
    input_device_index: int
    output_device_index: int


class RealtimePcmMixer:
    """Mixes an FFmpeg stdout PCM stream with a physical microphone."""

    def __init__(
        self,
        input_device_index: Optional[int] = None,
        output_device_index: Optional[int] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.input_device_index = input_device_index
        self.output_device_index = output_device_index
        self.on_error = on_error
        self.music_volume = 1.0
        self.mic_enabled = True
        self._audio: Optional[pyaudio.PyAudio] = None
        self._input_stream: Optional[pyaudio.Stream] = None
        self._output_stream: Optional[pyaudio.Stream] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    @staticmethod
    def list_devices() -> list[dict]:
        if pyaudio is None:
            raise PyAudioMixerError("PyAudio не встановлено. Виконайте pip install PyAudio.")
        audio = pyaudio.PyAudio()
        try:
            return [audio.get_device_info_by_index(i) for i in range(audio.get_device_count())]
        finally:
            audio.terminate()

    @classmethod
    def find_devices(cls) -> MixerDevices:
        devices = cls.list_devices()
        fallback_input: Optional[int] = None
        physical_input: Optional[int] = None
        output_index: Optional[int] = None
        for index, device in enumerate(devices):
            name = str(device.get("name", "")).lower()
            if device.get("maxInputChannels", 0) > 0:
                if fallback_input is None:
                    fallback_input = index
                if physical_input is None and "cable output" not in name and "vb-audio" not in name:
                    physical_input = index
            if output_index is None and "cable input" in name and device.get("maxOutputChannels", 0) > 0:
                output_index = index
        input_index = physical_input if physical_input is not None else fallback_input
        if input_index is None:
            raise PyAudioMixerError("Не знайдено пристрій запису з вхідними каналами.")
        if output_index is None:
            raise PyAudioMixerError("Не знайдено пристрій 'CABLE Input'.")
        return MixerDevices(input_index, output_index)

    def set_mic_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.mic_enabled = enabled

    def set_music_volume(self, volume: float) -> None:
        with self._lock:
            self.music_volume = max(0.0, min(1.0, volume))

    def start(self, music_stream: BinaryIO) -> None:
        with self._lock:
            self.stop()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(music_stream,),
                name="musicpad-pyaudio-mixer",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=3)
            except Exception:
                pass
        self._close_streams()
        self._thread = None

    def _run(self, music_stream: BinaryIO) -> None:
        try:
            self._open_streams()
            music_bytes_per_chunk = CHUNK * FRAME_BYTES
            while not self._stop_event.is_set():
                music_bytes = music_stream.read(music_bytes_per_chunk)
                if not music_bytes:
                    break
                mic_bytes = self._input_stream.read(CHUNK, exception_on_overflow=False)
                mixed = mix_audio(
                    music_bytes,
                    mic_bytes,
                    self.music_volume,
                    self.mic_enabled,
                )
                self._output_stream.write(mixed)
        except (OSError, IOError, ValueError, PYAUDIO_ERROR) as exc:
            if not self._stop_event.is_set() and self.on_error:
                self.on_error(f"Помилка PyAudio-мікшування: {exc}")
        finally:
            self._close_streams()

    def _open_streams(self) -> None:
        if pyaudio is None:
            raise PyAudioMixerError("PyAudio не встановлено. Виконайте pip install PyAudio.")
        self._audio = pyaudio.PyAudio()
        if self.input_device_index is None or self.output_device_index is None:
            devices = self.find_devices()
            self.input_device_index = self.input_device_index or devices.input_device_index
            self.output_device_index = self.output_device_index or devices.output_device_index
        input_info = self._audio.get_device_info_by_index(self.input_device_index)
        input_channels = 1 if input_info.get("maxInputChannels", 0) == 1 else CHANNELS
        self._input_stream = self._audio.open(
            format=pyaudio.paInt16,
            rate=SAMPLE_RATE,
            channels=input_channels,
            input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=CHUNK,
        )
        self._output_stream = self._audio.open(
            format=pyaudio.paInt16,
            rate=SAMPLE_RATE,
            channels=CHANNELS,
            output=True,
            output_device_index=self.output_device_index,
            frames_per_buffer=CHUNK,
        )

    def _close_streams(self) -> None:
        for stream in (self._input_stream, self._output_stream):
            if stream is not None:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
        self._input_stream = None
        self._output_stream = None
        if self._audio is not None:
            self._audio.terminate()
            self._audio = None
