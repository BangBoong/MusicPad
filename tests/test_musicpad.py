"""Comprehensive unit and integration tests for MusicPad."""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import threading
import time
import unittest
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import main
from musicpad import audio_engine as ae_mod
from musicpad import library as lib_mod
from musicpad.audio_engine import (
    CHANNELS,
    CHUNK_BYTES,
    SAMPLE_RATE,
    AudioEngine,
    AudioEngineError,
    StreamTrack,
)
from musicpad.library import LocalTrack, format_duration, scan_file, scan_folder
from musicpad.pyaudio_mixer import MixerDevices, PyAudioMixerError, RealtimePcmMixer, mix_audio


def _create_sine_wav(path: Path, duration_sec: float = 0.25, freq: float = 440.0, rate: int = 44100) -> None:
    num_frames = int(rate * duration_sec)
    samples = array("h")
    for i in range(num_frames):
        val = int(12000 * math.sin(2.0 * math.pi * freq * i / rate))
        samples.append(val)
        samples.append(val)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


class TestLibrary(unittest.TestCase):
    def test_format_duration_cases(self) -> None:
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(-15.5), "0:00")
        self.assertEqual(format_duration(5.9), "0:05")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3661), "61:01")

    def test_scan_folder_and_file_real_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav_a = root / "b_track.wav"
            wav_b = root / "a_track.wav"
            ignored = root / "notes.txt"
            _create_sine_wav(wav_a, duration_sec=0.5)
            _create_sine_wav(wav_b, duration_sec=1.25)
            ignored.write_text("not audio", encoding="utf-8")

            tracks = scan_folder(str(root))
            self.assertEqual(len(tracks), 2)
            self.assertEqual(tracks[0].title, "a_track")
            self.assertEqual(tracks[1].title, "b_track")
            self.assertAlmostEqual(tracks[0].duration, 1.25, places=1)
            self.assertAlmostEqual(tracks[1].duration, 0.5, places=1)

            self.assertIsNone(scan_file(ignored))
            self.assertIsNone(scan_file(root / "nonexistent.mp3"))
            self.assertEqual(scan_folder(str(root / "missing_dir")), [])

    def test_scan_folder_with_mocked_mutagen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            song_path = Path(tmp) / "song.mp3"
            song_path.write_bytes(b"ID3")
            fake_meta = MagicMock()
            fake_meta.info.length = 187.4
            fake_meta.get.return_value = ["Tagged Title"]
            with patch.object(lib_mod, "MutagenFile", return_value=fake_meta):
                tracks = scan_folder(tmp)
                self.assertEqual(len(tracks), 1)
                self.assertEqual(tracks[0].title, "Tagged Title")
                self.assertAlmostEqual(tracks[0].duration, 187.4, places=2)


class TestPyAudioMixer(unittest.TestCase):
    def test_mix_audio_stereo_and_mono_and_clipping(self) -> None:
        music = array("h", [1000, -1000, 30000, -30000]).tobytes()
        mic_stereo = array("h", [500, 500, 10000, -10000]).tobytes()
        mixed = array("h")
        mixed.frombytes(mix_audio(music, mic_stereo, music_volume=1.0, mic_enabled=True))
        self.assertEqual(list(mixed), [1500, -500, 32767, -32768])

        mic_mono = array("h", [200, -300]).tobytes()
        mixed_mono = array("h")
        mixed_mono.frombytes(mix_audio(music, mic_mono, music_volume=0.5, mic_enabled=True))
        self.assertEqual(list(mixed_mono), [700, -300, 14700, -15300])

        muted = array("h")
        muted.frombytes(mix_audio(music, mic_stereo, music_volume=0.5, mic_enabled=False))
        self.assertEqual(list(muted), [500, -500, 15000, -15000])

        mismatched = array("h")
        mismatched.frombytes(mix_audio(music, b"\x00\x00", music_volume=1.0, mic_enabled=True))
        self.assertEqual(list(mismatched), [1000, -1000, 30000, -30000])

    def test_find_devices_skips_cable_output_for_mic(self) -> None:
        fake_devices = [
            {"name": "CABLE Output (VB-Audio Virtual)", "maxInputChannels": 2, "maxOutputChannels": 0},
            {"name": "Microphone (5- fifine Microphone)", "maxInputChannels": 2, "maxOutputChannels": 0},
            {"name": "CABLE Input (VB-Audio Virtual)", "maxInputChannels": 0, "maxOutputChannels": 2},
        ]
        with patch.object(RealtimePcmMixer, "list_devices", return_value=fake_devices):
            devs = RealtimePcmMixer.find_devices()
            self.assertEqual(devs, MixerDevices(input_device_index=1, output_device_index=2))

    def test_find_devices_errors(self) -> None:
        with patch.object(RealtimePcmMixer, "list_devices", return_value=[]):
            with self.assertRaises(PyAudioMixerError):
                RealtimePcmMixer.find_devices()


class TestAudioEngineUnit(unittest.TestCase):
    def setUp(self) -> None:
        AudioEngine.clear_cache()

    def test_pcm_helpers_and_edge_cases(self) -> None:
        self.assertEqual(AudioEngine._scale_pcm(b"", 0.5), b"")
        self.assertEqual(AudioEngine._mix_pcm(b"", b"", 2), b"")
        self.assertEqual(AudioEngine._resample_pcm(b"", 44100, 48000), b"")
        # Odd byte length should not raise ValueError
        self.assertEqual(len(AudioEngine._scale_pcm(b"\x01\x02\x03", 0.5)), 4)
        self.assertEqual(len(AudioEngine._mix_pcm(b"\x01\x02\x03\x04", b"\x01", 1)), 4)

        raw = array("h", [10000, -10000, 20000, -20000]).tobytes()
        scaled = array("h")
        scaled.frombytes(AudioEngine._scale_pcm(raw, 0.5))
        self.assertEqual(list(scaled), [5000, -5000, 10000, -10000])

        mic_mono = array("h", [1000, 2000]).tobytes()
        mixed = array("h")
        mixed.frombytes(AudioEngine._mix_pcm(raw, mic_mono, 1))
        self.assertEqual(list(mixed), [11000, -9000, 22000, -18000])

        same_rate = AudioEngine._resample_pcm(raw, 44100, 44100)
        self.assertEqual(same_rate, raw)

        upsampled = AudioEngine._resample_pcm(raw * 16, 44100, 48000)
        self.assertGreater(len(upsampled), len(raw * 16))
        self.assertEqual(len(upsampled) % 4, 0)

    def test_dsp_float32_mixer_and_soft_knee_limiter(self) -> None:
        import numpy as np

        # Linear transparency under threshold 0.89125 (-1 dBFS)
        quiet_sig = np.array([[-0.5, 0.5], [0.8, -0.8]], dtype=np.float32)
        limited_quiet = AudioEngine.apply_soft_knee_limiter(quiet_sig)
        np.testing.assert_allclose(limited_quiet, quiet_sig, atol=1e-6)

        # Smooth saturation above threshold and strict clamp to [-1.0, 1.0]
        loud_sig = np.array([[-1.5, 1.5], [3.0, -4.0]], dtype=np.float32)
        limited_loud = AudioEngine.apply_soft_knee_limiter(loud_sig)
        self.assertTrue(np.all(limited_loud >= -1.0))
        self.assertTrue(np.all(limited_loud <= 1.0))
        self.assertAlmostEqual(float(limited_loud[1, 0]), 1.0, places=4)
        self.assertAlmostEqual(float(limited_loud[1, 1]), -1.0, places=4)

        # Float32 mixing with separate gain scaling
        music = np.ones((100, 2), dtype=np.float32) * 0.4
        mic = np.ones((100, 2), dtype=np.float32) * 0.3
        mixed = AudioEngine.mix_float32(music, mic, mic_volume=0.5, track_volume=1.0)
        np.testing.assert_allclose(mixed, 0.55, atol=1e-5)

        # Muted mic
        mixed_muted = AudioEngine.mix_float32(music, mic, mic_volume=1.0, track_volume=1.0, mic_muted=True)
        np.testing.assert_allclose(mixed_muted, 0.4, atol=1e-5)

    def test_dsp_resampler_to_48k_and_channels(self) -> None:
        import numpy as np

        mono_44k = np.sin(np.linspace(0, 2 * np.pi * 10, 441)).astype(np.float32)
        stereo_48k = AudioEngine.resample_to_48k_float32(mono_44k, source_rate=44100, source_channels=1)
        self.assertEqual(stereo_48k.shape[1], 2)
        self.assertEqual(stereo_48k.shape[0], 480)
        np.testing.assert_allclose(stereo_48k[:, 0], stereo_48k[:, 1], atol=1e-6)

        stereo_22k = np.ones((2205, 2), dtype=np.float32) * 0.7
        resampled_48k = AudioEngine.resample_to_48k_float32(stereo_22k, source_rate=22050, source_channels=2)
        self.assertEqual(resampled_48k.shape, (4800, 2))
        np.testing.assert_allclose(resampled_48k, 0.7, atol=1e-5)

    def test_float32_ring_buffer_concurrency(self) -> None:
        import numpy as np
        from musicpad.audio_engine import Float32RingBuffer

        rb = Float32RingBuffer(capacity_frames=1024, channels=2)
        self.assertEqual(rb.available(), 0)
        self.assertEqual(rb.write_space(), 1024)

        w_data = np.ones((500, 2), dtype=np.float32) * 0.8
        written = rb.write(w_data)
        self.assertEqual(written, 500)
        self.assertEqual(rb.available(), 500)

        r_data, count = rb.read(300)
        self.assertEqual(count, 300)
        np.testing.assert_allclose(r_data, 0.8)

        r_data2, count2 = rb.read(300)
        self.assertEqual(count2, 200)
        np.testing.assert_allclose(r_data2[:200], 0.8)
        np.testing.assert_allclose(r_data2[200:], 0.0)

        rb.write(w_data)
        rb.reset()
        self.assertEqual(rb.available(), 0)

    def test_blocksize_range_configuration(self) -> None:
        engine = AudioEngine()
        self.assertEqual(engine.blocksize, 384)
        engine.set_blocksize(256)
        self.assertEqual(engine.blocksize, 256)
        engine.set_blocksize(512)
        self.assertEqual(engine.blocksize, 512)
        engine.set_blocksize(128)
        self.assertEqual(engine.blocksize, 256)
        engine.set_blocksize(1024)
        self.assertEqual(engine.blocksize, 512)

    def test_device_selection_helpers_with_mock_devices(self) -> None:
        mock_devs = [
            {"name": "Realtek HD Audio 2nd output", "max_output_channels": 2, "max_input_channels": 0, "hostapi": 0},
            {"name": "CABLE In 16ch (VB-Audio)", "max_output_channels": 2, "max_input_channels": 0, "hostapi": 0},
            {"name": "CABLE Input (VB-Audio Virtual C", "max_output_channels": 2, "max_input_channels": 0, "hostapi": 0},
            {"name": "CABLE Output (VB-Audio Virtual", "max_output_channels": 0, "max_input_channels": 2, "hostapi": 0},
            {"name": "Microphone (Steam Streaming Mic", "max_output_channels": 0, "max_input_channels": 2, "hostapi": 0},
            {"name": "Microphone (5- fifine Microphon", "max_output_channels": 0, "max_input_channels": 2, "hostapi": 0},
        ]
        with patch.object(AudioEngine, "list_devices", return_value=mock_devs):
            cable = AudioEngine.find_cable_output()
            self.assertEqual(cable, 2)
            mic = AudioEngine.find_physical_mic()
            self.assertEqual(mic, 5)
            mon = AudioEngine.find_default_monitor(exclude_device=cable)
            self.assertEqual(mon, 0)

    def test_resolve_source_local_and_youtube_mocked(self) -> None:
        path, title = AudioEngine._resolve_source(StreamTrack("My Song", "test.mp3", is_url=False))
        self.assertTrue(os.path.isabs(path))
        self.assertEqual(title, "My Song")

        fake_ytm = MagicMock()
        fake_ytm.search.return_value = [
            {"videoId": "abc123xyz00", "title": "Never Gonna", "artists": [{"name": "Rick"}]}
        ]
        fake_ydl_instance = MagicMock()
        fake_ydl_instance.__enter__.return_value = fake_ydl_instance
        fake_ydl_instance.extract_info.return_value = {
            "url": "https://audio.example/stream.webm",
            "title": "Direct Title",
        }
        with (
            patch.object(ae_mod, "YTMusic", return_value=fake_ytm),
            patch.object(ae_mod, "YoutubeDL", return_value=fake_ydl_instance),
        ):
            url, res_title = AudioEngine._resolve_source(
                StreamTrack("never gonna", "never gonna", is_url=True)
            )
            self.assertEqual(url, "https://audio.example/stream.webm")
            self.assertEqual(res_title, "Never Gonna — Rick")

            url2, res_title2 = AudioEngine._resolve_source(
                StreamTrack("https://youtu.be/abc123xyz00", "https://youtu.be/abc123xyz00", is_url=True)
            )
            self.assertEqual(url2, "https://audio.example/stream.webm")
            self.assertEqual(res_title2, "Direct Title")

            fake_ytm.search.return_value = []
            with self.assertRaises(AudioEngineError):
                AudioEngine._resolve_source(StreamTrack("missing", "missing", is_url=True))

    def test_instant_track_switching_and_completion_mocked(self) -> None:
        out_stream = MagicMock()
        mon_stream = MagicMock()

        def make_out_stream(**kwargs):
            return out_stream if kwargs.get("device") == 1 else mon_stream

        proc1_chunks = [b"\x01\x00\x01\x00" * 1024 for _ in range(50)]
        proc1 = MagicMock()
        proc1.poll.return_value = None

        def proc1_read(n: int) -> bytes:
            time.sleep(0.01)
            if proc1.terminate.called or not proc1_chunks:
                return b""
            return proc1_chunks.pop(0)

        proc1.stdout.read.side_effect = proc1_read

        proc2 = MagicMock()
        proc2.poll.return_value = None
        proc2.stdout.read.side_effect = [b"\x02\x00\x02\x00" * 1024, b""]

        finished_event = threading.Event()
        titles: list[str] = []

        with (
            patch.object(ae_mod.sd, "RawOutputStream", side_effect=make_out_stream),
            patch.object(
                ae_mod.sd,
                "query_devices",
                return_value={"name": "Headphones", "default_samplerate": 48000, "max_output_channels": 2},
            ),
            patch.object(AudioEngine, "check_ffmpeg", return_value=True),
            patch("subprocess.Popen", side_effect=[proc1, proc2]) as mock_popen,
        ):
            engine = AudioEngine(
                output_device=1,
                input_device=None,
                monitor_device=2,
                on_finished=finished_event.set,
                on_track_title=titles.append,
            )
            engine.play(StreamTrack("Track 1", "t1.mp3"))
            time.sleep(0.04)
            engine.play(StreamTrack("Track 2", "t2.mp3"))
            self.assertTrue(finished_event.wait(timeout=2.0), "Engine should finish Track 2 and call on_finished")
            self.assertTrue(proc1.terminate.called, "Track 1 process should be terminated on switch")
            self.assertEqual(mock_popen.call_count, 2)
            self.assertEqual(titles, ["Track 1", "Track 2"])
            self.assertTrue(out_stream.write.called)
            self.assertTrue(mon_stream.write.called)
            engine.shutdown()

    def test_live_device_switching_and_seek_mocked(self) -> None:
        opened_devices: list[int | None] = []

        def make_out_stream(**kwargs):
            opened_devices.append(kwargs.get("device"))
            return MagicMock()

        proc1_chunks = [b"\x01\x00\x01\x00" * 1024 for _ in range(30)]
        proc1 = MagicMock()
        proc1.poll.return_value = None

        def proc1_read(n: int) -> bytes:
            time.sleep(0.01)
            if proc1.terminate.called or not proc1_chunks:
                return b""
            return proc1_chunks.pop(0)

        proc1.stdout.read.side_effect = proc1_read

        proc2 = MagicMock()
        proc2.poll.return_value = None
        proc2.stdout.read.side_effect = [b"\x02\x00\x02\x00" * 1024, b""]

        done = threading.Event()
        with (
            patch.object(ae_mod.sd, "RawOutputStream", side_effect=make_out_stream),
            patch.object(
                ae_mod.sd,
                "query_devices",
                return_value={"name": "Dev", "default_samplerate": 44100, "max_output_channels": 2},
            ),
            patch.object(AudioEngine, "check_ffmpeg", return_value=True),
            patch("subprocess.Popen", side_effect=[proc1, proc2]) as mock_popen,
        ):
            engine = AudioEngine(output_device=1, monitor_device=2, on_finished=done.set)
            engine.play(StreamTrack("Seekable", "s.mp3"))
            time.sleep(0.03)
            engine.set_monitor_device(4)
            time.sleep(0.03)
            engine.seek(12.5)
            self.assertTrue(done.wait(timeout=2.0))
            self.assertIn(4, opened_devices)
            seek_cmd = mock_popen.call_args_list[1][0][0]
            self.assertIn("-ss", seek_cmd)
            self.assertIn("12.50", seek_cmd)
            engine.shutdown()

    def test_stop_with_mic_enabled_stops_music_immediately(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        chunks = [b"\x01\x00\x01\x00" * 1024 for _ in range(8)]

        def read_pipe(n: int) -> bytes:
            time.sleep(0.01)
            return chunks.pop(0) if chunks else b""

        proc.stdout.read.side_effect = read_pipe
        in_mock = MagicMock()
        in_mock.read.return_value = (b"\x00" * 4096, False)

        with (
            patch.object(ae_mod.sd, "RawOutputStream", return_value=MagicMock()),
            patch.object(ae_mod.sd, "RawInputStream", return_value=in_mock),
            patch.object(
                ae_mod.sd,
                "query_devices",
                return_value={
                    "name": "Mic",
                    "max_input_channels": 2,
                    "max_output_channels": 2,
                    "default_samplerate": 44100,
                },
            ),
            patch.object(AudioEngine, "check_ffmpeg", return_value=True),
            patch("subprocess.Popen", return_value=proc),
        ):
            engine = AudioEngine(output_device=1, input_device=2)
            engine.play(StreamTrack("Song", "song.mp3"))
            time.sleep(0.02)
            engine.stop()
            time.sleep(0.08)
            self.assertEqual(engine.get_position(), 0.0)
            engine.shutdown()

    def test_real_ffmpeg_and_winmm_dual_output_and_mic_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav_file = Path(tmp) / "tone.wav"
            _create_sine_wav(wav_file, duration_sec=0.18)
            cable_idx = AudioEngine.find_cable_output()
            mic_idx = AudioEngine.find_physical_mic()
            mon_idx = AudioEngine.find_default_monitor(exclude_device=cable_idx)

            # Verify first microphone read waits for buffer completion rather than returning 0ms overflow
            if mic_idx is not None and hasattr(ae_mod, "_WinMMRawInputStream"):
                in_stream = ae_mod._WinMMRawInputStream(device=mic_idx, blocksize=1024)
                try:
                    in_stream.start()
                    t_read = time.monotonic()
                    data, overflow = in_stream.read(1024)
                    read_dt = time.monotonic() - t_read
                    self.assertFalse(overflow, "First WinMM mic read must not overflow due to WIM_OPEN")
                    self.assertGreaterEqual(read_dt, 0.01)
                    self.assertEqual(len(data), 4096)
                finally:
                    in_stream.stop()
                    in_stream.close()

            done_evt = threading.Event()
            errors: list[str] = []
            engine = AudioEngine(
                output_device=cable_idx,
                input_device=mic_idx,
                monitor_device=mon_idx,
                on_error=errors.append,
                on_finished=done_evt.set,
                monitor_mic=False,
            )
            engine.set_volume(0.02)
            t0 = time.monotonic()
            engine.play(StreamTrack("Tone", str(wav_file)))
            self.assertTrue(done_evt.wait(timeout=4.0), f"Real playback did not finish; errors={errors}")
            elapsed = time.monotonic() - t0
            self.assertGreaterEqual(elapsed, 0.12, "Audio output must play out rather than truncating early")
            self.assertEqual(errors, [])
            engine.shutdown()


class TestMusicPadAppGUI(unittest.TestCase):
    def test_app_startup_controls_and_hotkeys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav1 = Path(tmp) / "first.wav"
            wav2 = Path(tmp) / "second.wav"
            cfg_file = Path(tmp) / "test_state.json"
            _create_sine_wav(wav1, duration_sec=0.2)
            _create_sine_wav(wav2, duration_sec=0.3)

            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    self.assertIn("CABLE Input", app.output_var.get())
                    self.assertNotIn("CABLE Output", app.mix_mic_var.get())
                    self.assertNotEqual(app.monitor_var.get(), "(Без моніторингу)")

                    tracks = scan_folder(tmp)
                    app._set_tracks(tracks)
                    app.update()
                    self.assertEqual(len(app.visible_tracks), 2)

                    # Test hotkey formatting helper (including Windows Numpad keycode & Ukrainian layout keycode)
                    ev_f1 = SimpleNamespace(keysym="F1", state=0)
                    self.assertEqual(main._format_tk_hotkey(ev_f1), "F1")
                    ev_alt1 = SimpleNamespace(keysym="1", state=0x0008)
                    self.assertEqual(main._format_tk_hotkey(ev_alt1), "Alt+1")
                    ev_num5 = SimpleNamespace(keysym="KP_5", state=0)
                    self.assertEqual(main._format_tk_hotkey(ev_num5), "Num5")
                    ev_win_num1 = SimpleNamespace(keysym="1", keycode=0x61, state=0)
                    self.assertEqual(main._format_tk_hotkey(ev_win_num1), "Num1")
                    ev_ukr_q = SimpleNamespace(keysym="??", keycode=ord("Q"), state=0)
                    self.assertEqual(main._format_tk_hotkey(ev_ukr_q), "Q")

                    self.assertTrue(main.MusicPadApp._is_typing_conflict_hotkey("Space"))
                    self.assertTrue(main.MusicPadApp._is_typing_conflict_hotkey("Q"))
                    self.assertFalse(main.MusicPadApp._is_typing_conflict_hotkey("F1"))
                    self.assertFalse(main.MusicPadApp._is_typing_conflict_hotkey("Alt+1"))

                    # Assign hotkey and filter by hotkey
                    app.local_tracks[0].hotkey = "F1"
                    app._filter_tracks()
                    app.search_var.set("f1")
                    app.update()
                    self.assertEqual(len(app.visible_tracks), 1)
                    self.assertEqual(app.visible_tracks[0].title, "first")

                    # Clear hotkey
                    app.track_list.selection_set("0")
                    app._clear_hotkey_selected()
                    self.assertEqual(app.local_tracks[0].hotkey, "")

                    app.search_var.set("")
                    app.update()
                    self.assertEqual(len(app.visible_tracks), 2)

                    # Shut down any real engine before replacing with mock
                    if hasattr(app, "engine") and hasattr(app.engine, "shutdown"):
                        app.engine.shutdown()

                    # Test play, pause, resume, stop with mocked engine
                    app.engine = MagicMock()
                    app.engine._process = MagicMock()
                    app.engine._active_track = StreamTrack("first", str(wav1))
                    app.is_paused = False
                    app._toggle_pause()
                    self.assertTrue(app.is_paused)
                    app.engine.pause.assert_called_once()
                    app._toggle_pause()
                    self.assertFalse(app.is_paused)
                    app.engine.resume.assert_called_once()

                    app._stop()
                    app.engine.stop.assert_called_once()

                    # Test next / previous navigation
                    with patch.object(app, "_start") as mock_start:
                        app._next()
                        self.assertEqual(app.current_index, 0)
                        mock_start.assert_called_with(StreamTrack("first", str(wav1)))
                        app._previous()
                        self.assertEqual(app.current_index, 1)
                        mock_start.assert_called_with(StreamTrack("second", str(wav2)))
                finally:
                    app._exit()

    def test_youtube_search_panel_and_left_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # Verify left panel categories exist
                    self.assertTrue(hasattr(app, "cat_item_frame"))
                    self.assertTrue(hasattr(app, "cat_yt_frame"))
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.yt_search_frame.winfo_ismapped(), 0)

                    # Select YouTube category
                    app._select_category("youtube")
                    app.update()
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.yt_search_frame.winfo_manager(), "place")

                    # Perform search
                    mock_results = [
                        {
                            "title": "Cleric Beast — SIE Sound Team",
                            "url": "https://www.youtube.com/watch?v=mock1",
                            "duration": 185.0,
                        },
                        {
                            "title": "Cleric Beast (From Bloodborne)",
                            "url": "https://www.youtube.com/watch?v=mock2",
                            "duration": 289.0,
                        },
                    ]
                    app.yt_query_var.set("cleric beast")
                    with patch.object(AudioEngine, "search_youtube", return_value=mock_results):
                        app._perform_yt_search()
                        for _ in range(50):
                            app.update()
                            if len(app.yt_tree.get_children()) == 2:
                                break
                            time.sleep(0.05)

                    self.assertEqual(len(app.yt_tree.get_children()), 2)
                    self.assertIn("2", app.yt_status_lbl.cget("text"))

                    # Test picking first track to play
                    with patch.object(app, "_start") as mock_start:
                        app.yt_tree.selection_set("0")
                        app._yt_play_picked(add_only=False)
                        self.assertEqual(len(app.local_tracks), 1)
                        self.assertEqual(app.local_tracks[0].title, "Cleric Beast — SIE Sound Team")
                        mock_start.assert_called_once()

                    # Test closing search panel
                    app._hide_yt_search_panel()
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.yt_search_frame.winfo_manager(), "")
                finally:
                    app._exit()

    def test_wav_struct_fallback_without_wave_module(self) -> None:
        import struct

        with tempfile.TemporaryDirectory() as tmp:
            wav_file = Path(tmp) / "fallback.wav"
            _create_sine_wav(wav_file, duration_sec=0.5, rate=44100)
            with (
                patch.object(lib_mod, "wave", None),
                patch.object(lib_mod, "MutagenFile", None),
                patch.object(ae_mod, "wave", None),
            ):
                track = scan_file(wav_file)
                self.assertIsNotNone(track)
                assert track is not None
                self.assertAlmostEqual(track.duration, 0.5, places=2)

                decoder_full = ae_mod._WavPipeDecoder(str(wav_file), start_pos=0.0)
                pcm_full = decoder_full.stdout.read()
                self.assertEqual(len(pcm_full), int(44100 * 0.5) * 4)

                decoder_seek = ae_mod._WavPipeDecoder(str(wav_file), start_pos=0.25)
                pcm_seek = decoder_seek.stdout.read()
                self.assertAlmostEqual(len(pcm_seek), int(44100 * 0.25) * 4, delta=16)

            # Test 32-bit IEEE float WAV (format tag 3, unsupported by stdlib wave)
            float_wav = Path(tmp) / "float32.wav"
            float_samples = struct.pack("<4f", 0.5, -0.5, 1.0, -1.0)
            fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, 3, 2, 44100, 44100 * 8, 8, 32)
            data_chunk = struct.pack("<4sI", b"data", len(float_samples)) + float_samples
            riff_hdr = struct.pack("<4sI4s", b"RIFF", 4 + len(fmt_chunk) + len(data_chunk), b"WAVE")
            float_wav.write_bytes(riff_hdr + fmt_chunk + data_chunk)

            dec_float = ae_mod._WavPipeDecoder(str(float_wav), start_pos=0.0)
            pcm_float = array("h")
            pcm_float.frombytes(dec_float.stdout.read())
            self.assertEqual(list(pcm_float), [16384, -16384, 32767, -32767])

            # Test ffmpeg -i stderr fallback in _probe_ffprobe_metadata when ffprobe is absent
            fake_proc = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Input #0, mov,mp4,m4a:\n  Metadata:\n    title           : Cached YT Song\n  Duration: 00:03:33.50, start: 0.000000",
            )
            with (
                patch("shutil.which", return_value=None),
                patch.object(AudioEngine, "find_ffmpeg", return_value="C:/fake/ffmpeg.exe"),
                patch("subprocess.run", return_value=fake_proc),
            ):
                t_meta, d_meta = lib_mod._probe_ffprobe_metadata(Path(tmp) / "song.m4a")
                self.assertEqual(t_meta, "Cached YT Song")
                self.assertAlmostEqual(d_meta, 213.5, places=2)

    def test_worker_race_on_finish_and_stop_no_spurious_finished(self) -> None:
        proc1 = MagicMock()
        proc1.poll.return_value = None
        proc1.stdout.read.side_effect = [b"\x01\x00\x01\x00" * 1024, b""]

        proc2 = MagicMock()
        proc2.poll.return_value = None
        proc2.stdout.read.side_effect = [b"\x02\x00\x02\x00" * 1024, b""]

        titles: list[str] = []
        finish_count = 0
        second_done = threading.Event()
        engine_ref: list[AudioEngine] = []

        def on_fin() -> None:
            nonlocal finish_count
            finish_count += 1
            if finish_count == 1:
                # Trigger play(Track 2) right inside on_finished while worker thread is still alive
                engine_ref[0].play(StreamTrack("Track 2", "t2.mp3"))
            elif finish_count == 2:
                second_done.set()

        with (
            patch.object(ae_mod.sd, "RawOutputStream", return_value=MagicMock()),
            patch.object(AudioEngine, "check_ffmpeg", return_value=True),
            patch("subprocess.Popen", side_effect=[proc1, proc2]),
        ):
            engine = AudioEngine(output_device=1, on_finished=on_fin, on_track_title=titles.append)
            engine_ref.append(engine)
            engine.play(StreamTrack("Track 1", "t1.mp3"))
            self.assertTrue(
                second_done.wait(timeout=2.0),
                f"Second track queued during Track 1 finish should play and finish; titles={titles}",
            )
            self.assertEqual(titles, ["Track 1", "Track 2"])
            engine.shutdown()

        # Verify explicit stop() does not trigger on_finished when mic is kept alive
        proc_stop = MagicMock()
        proc_stop.poll.return_value = None
        stop_called = threading.Event()

        def slow_read(n: int) -> bytes:
            if proc_stop.terminate.called:
                return b""
            time.sleep(0.01)
            return b"\x01\x00\x01\x00" * 1024

        proc_stop.stdout.read.side_effect = slow_read
        in_mock = MagicMock()
        in_mock.read.return_value = (b"\x00" * 4096, False)

        with (
            patch.object(ae_mod.sd, "RawOutputStream", return_value=MagicMock()),
            patch.object(ae_mod.sd, "RawInputStream", return_value=in_mock),
            patch.object(
                ae_mod.sd,
                "query_devices",
                return_value={"name": "Mic", "max_input_channels": 2, "max_output_channels": 2, "default_samplerate": 44100},
            ),
            patch.object(AudioEngine, "check_ffmpeg", return_value=True),
            patch("subprocess.Popen", return_value=proc_stop),
        ):
            engine2 = AudioEngine(output_device=1, input_device=2, on_finished=stop_called.set)
            engine2.play(StreamTrack("StopSong", "s.mp3"))
            time.sleep(0.03)
            engine2.stop()
            time.sleep(0.06)
            self.assertFalse(stop_called.is_set(), "Explicit stop() should not fire on_finished")
            engine2.shutdown()

    def test_frozen_exe_root_and_cache_paths_and_state_pruning(self) -> None:
        expected_root = Path(main.__file__).resolve().parent
        fake_dist_exe = str(expected_root / "dist" / "MusicPad.exe")
        with (
            patch.object(main.sys, "frozen", True, create=True),
            patch.object(main.sys, "executable", fake_dist_exe),
        ):
            self.assertEqual(main.MusicPadApp._app_root_dir(), expected_root)
            self.assertEqual(
                main.MusicPadApp._config_path(),
                expected_root / ".cache" / "musicpad_state.json",
            )
            self.assertEqual(
                AudioEngine._cache_dir(),
                expected_root / ".cache" / "youtube",
            )

        # Also test memoryview / buffer handling in PCM helpers (as returned by real sounddevice)
        buf = memoryview(array("h", [4000, -4000, 8000, -8000]).tobytes())
        self.assertEqual(len(AudioEngine._scale_pcm(buf, 0.5)), 8)
        self.assertEqual(len(AudioEngine._mix_pcm(buf, buf, 2)), 8)
        self.assertEqual(len(AudioEngine._resample_pcm(buf, 44100, 48000)), 8)

        # Test that _load_saved_state prunes nonexistent local track paths and persists cleaned JSON
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "musicpad_state.json"
            valid_wav = Path(tmp) / "valid.wav"
            _create_sine_wav(valid_wav, duration_sec=0.2)
            stale_data = {
                "folder": str(Path(tmp) / "deleted_temp_dir"),
                "tracks": [
                    {"title": "first", "path": str(Path(tmp) / "missing1.wav"), "duration": 0.2, "hotkey": "", "is_url": False},
                    {"title": "second", "path": str(Path(tmp) / "missing2.wav"), "duration": 0.3, "hotkey": "", "is_url": False},
                    {"title": "valid", "path": str(valid_wav), "duration": 0.2, "hotkey": "F2", "is_url": False},
                ],
            }
            cfg_file.write_text(json.dumps(stale_data), encoding="utf-8")
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    self.assertEqual(len(app.local_tracks), 1)
                    self.assertEqual(app.local_tracks[0].title, "valid")
                    saved_after = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertEqual(len(saved_after["tracks"]), 1)
                    self.assertEqual(saved_after["tracks"][0]["title"], "valid")
                finally:
                    app._exit()

    def test_menu_bar_structure_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    mbs = [w for w in app.menu_frame.winfo_children() if isinstance(w, main.tk.Menubutton)]
                    names = [mb.cget("text") for mb in mbs]
                    # Exactly 4 menus: Файл, Редагування, Відтворення, Вікно (NO Довідка)
                    self.assertEqual(names, ["Файл", "Редагування", "Відтворення", "Вікно"])

                    # Check File menu
                    file_menu = app.nametowidget(mbs[0].cget("menu"))
                    file_labels = [file_menu.entrycget(i, "label") for i in range(file_menu.index("end") + 1) if file_menu.type(i) != "separator"]
                    self.assertIn("Додати звукові файли", file_labels)
                    self.assertIn("Новий список звуків", file_labels)
                    self.assertIn("Завантажити список звуків", file_labels)
                    self.assertIn("Зберегти список звуків", file_labels)
                    self.assertIn("Налаштування", file_labels)
                    self.assertIn("Вихід", file_labels)

                    # Check Edit menu
                    edit_menu = app.nametowidget(mbs[1].cget("menu"))
                    edit_labels = [edit_menu.entrycget(i, "label") for i in range(edit_menu.index("end") + 1) if edit_menu.type(i) != "separator"]
                    self.assertIn("Скасувати", edit_labels)
                    self.assertIn("Відновити", edit_labels)
                    self.assertIn("Вирізати", edit_labels)
                    self.assertIn("Копіювати", edit_labels)
                    self.assertIn("Вставити", edit_labels)
                    self.assertIn("Видалити вибрані записи", edit_labels)
                    self.assertIn("Перемішати список", edit_labels)
                    self.assertIn("Вибрати все", edit_labels)
                    self.assertIn("Пошук", edit_labels)

                    # Check Play menu
                    play_menu = app.nametowidget(mbs[2].cget("menu"))
                    play_labels = [play_menu.entrycget(i, "label") for i in range(play_menu.index("end") + 1) if play_menu.type(i) != "separator"]
                    self.assertIn("Відтворити вибраний файл (динаміки та мікрофон)", play_labels)
                    self.assertIn("Автозупинка (рекомендовано)", play_labels)
                    self.assertTrue(app.auto_stop_var.get())

                    # Check Window menu
                    win_menu = app.nametowidget(mbs[3].cget("menu"))
                    win_labels = [win_menu.entrycget(i, "label") for i in range(win_menu.index("end") + 1) if win_menu.type(i) != "separator"]
                    self.assertIn("Категорії", win_labels)
                    self.assertIn("Швидка панель", win_labels)
                    self.assertIn("Гарячі клавіші", win_labels)
                    self.assertIn("Записування звуку", win_labels)
                    self.assertIn("Інструмент «Колір»", win_labels)
                    self.assertIn("Озвучення тексту", win_labels)

                    # Verify menu buttons are clickable and open popups below each button
                    popups_triggered = []
                    for label in ["Файл", "Редагування", "Відтворення", "Вікно"]:
                        btn = app._menu_buttons[label]
                        m = app.nametowidget(btn.cget("menu"))
                        m.tk_popup = lambda x, y, lbl=label: popups_triggered.append((lbl, x, y))
                        btn.event_generate("<Button-1>")
                        app.update()
                    self.assertEqual(len(popups_triggered), 4)
                    self.assertEqual([p[0] for p in popups_triggered], ["Файл", "Редагування", "Відтворення", "Вікно"])

                    # Test actions: Shuffle, Select All, Undo
                    t1 = main.LocalTrack("Track A", "C:/fake/a.wav", 1.0)
                    t2 = main.LocalTrack("Track B", "C:/fake/b.wav", 2.0)
                    t3 = main.LocalTrack("Track C", "C:/fake/c.wav", 3.0)
                    app._set_tracks([t1, t2, t3])
                    app.update()
                    self.assertEqual(len(app.local_tracks), 3)

                    app._select_all_tracks()
                    self.assertEqual(len(app.track_list.selection()), 3)

                    # Cut track
                    app.track_list.selection_set("0")
                    app._cut_track()
                    self.assertEqual(len(app.local_tracks), 2)

                    # Undo cut
                    app._undo()
                    self.assertEqual(len(app.local_tracks), 3)

                    # Save sound list
                    app._save_sound_list()
                    self.assertIn("збережено", app.status_var.get())
                finally:
                    app._exit()

    def test_menu_bar_hover_switching_and_border_styling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_menu_hover.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()

                    # 1. Border Styling & Theme Colors
                    c_dark = app._get_theme_colors()
                    self.assertEqual(c_dark["menu_border"], "#3f3f3f")
                    for name in ["Файл", "Редагування", "Відтворення", "Вікно"]:
                        btn = app._menu_buttons[name]
                        m = app.nametowidget(btn.cget("menu"))
                        self.assertEqual(str(m.cget("relief")), "solid")
                        self.assertEqual(int(m.cget("bd")), 1)
                        self.assertEqual(int(m.cget("activeborderwidth")), 0)

                    # Context menus border styling
                    for ctx_menu in [app._track_menu, app._cat_menu]:
                        self.assertEqual(str(ctx_menu.cget("relief")), "solid")
                        self.assertEqual(int(ctx_menu.cget("bd")), 1)
                        self.assertEqual(int(ctx_menu.cget("activeborderwidth")), 0)

                    # Light theme border colors
                    app._toggle_theme()
                    app.update()
                    c_light = app._get_theme_colors()
                    self.assertEqual(c_light["menu_border"], "#d0d4da")
                    for name in ["Файл", "Редагування", "Відтворення", "Вікно"]:
                        btn = app._menu_buttons[name]
                        m = app.nametowidget(btn.cget("menu"))
                        self.assertEqual(str(m.cget("relief")), "solid")
                        self.assertEqual(int(m.cget("bd")), 1)
                        self.assertEqual(int(m.cget("activeborderwidth")), 0)

                    # Switch back to dark theme
                    app._toggle_theme()
                    app.update()

                    # 2. State tracking
                    self.assertFalse(app.is_menu_active())
                    self.assertIsNone(app._active_menu_btn)
                    self.assertFalse(app._is_menu_open)

                    # Bounding boxes caching
                    app._cache_menu_button_rects()
                    self.assertEqual(len(app._button_rects), 4)
                    for btn in app._menu_buttons.values():
                        self.assertIn(btn, app._button_rects)
                        x, y, w, h = app._button_rects[btn]
                        self.assertGreaterEqual(w, 0)
                        self.assertGreaterEqual(h, 0)

                    # 3. Hover switching behavior:
                    # When a menu is open, hovering over a neighboring button switches the menu
                    btn_file = app._menu_buttons["Файл"]
                    btn_edit = app._menu_buttons["Редагування"]
                    btn_play = app._menu_buttons["Відтворення"]
                    m_file = app._button_to_menu[btn_file]
                    m_edit = app._button_to_menu[btn_edit]

                    # Mock tk_popup on m_file to simulate user hovering over "Редагування"
                    switched_sequence: list[str] = []
                    def _mock_file_popup(x: int, y: int) -> None:
                        switched_sequence.append("Файл")
                        self.assertTrue(app.is_menu_active())
                        self.assertEqual(app._active_menu_btn, btn_file)
                        # Cursor moves over "Редагування"
                        app._on_menu_enter(None, btn_edit)

                    def _mock_edit_popup(x: int, y: int) -> None:
                        switched_sequence.append("Редагування")
                        self.assertTrue(app.is_menu_active())
                        self.assertEqual(app._active_menu_btn, btn_edit)
                        # Cursor leaves menu bar into track list / window area
                        app._on_menu_leave(None, btn_edit)
                        # Menu should still remain active!
                        self.assertTrue(app.is_menu_active())

                    m_file.tk_popup = _mock_file_popup  # type: ignore[assignment]
                    m_edit.tk_popup = _mock_edit_popup  # type: ignore[assignment]

                    app._open_menu_dropdown(None, btn_file, m_file)
                    self.assertEqual(switched_sequence, ["Файл", "Редагування"])
                    self.assertFalse(app.is_menu_active())
                    self.assertIsNone(app._active_menu_btn)

                    # 4. Moving cursor when NO menu is active only highlights the button
                    self.assertFalse(app.is_menu_active())
                    app._on_menu_enter(None, btn_play)
                    self.assertEqual(str(btn_play.cget("bg")), "#333333")
                    app._on_menu_leave(None, btn_play)
                    self.assertEqual(str(btn_play.cget("bg")), app._get_theme_colors()["menu_bg"])

                    # 5. Debounce test on re-clicking the same menu button
                    app._last_closed_menu_btn = btn_file
                    app._menu_close_time = time.monotonic()
                    res = app._open_menu_dropdown(None, btn_file, m_file)
                    self.assertEqual(res, "break")
                    self.assertFalse(app.is_menu_active())

                    # 6. Tracker thread lifecycle & no thread leak on repeated start/stop
                    app._start_menu_hover_tracker()
                    t1 = app._hover_monitor_thread
                    self.assertIsNotNone(t1)
                    app._stop_menu_hover_tracker()
                    self.assertFalse(t1.is_alive())

                    # Repeated start/stop must cleanly stop old thread
                    app._start_menu_hover_tracker()
                    t2 = app._hover_monitor_thread
                    app._start_menu_hover_tracker()
                    t3 = app._hover_monitor_thread
                    self.assertIsNotNone(t3)
                    self.assertFalse(t2.is_alive())
                    app._stop_menu_hover_tracker()
                    self.assertFalse(t3.is_alive())

                    # 7. Cross-thread safety of _close_current_popup
                    bg_err: list[Exception] = []
                    def _call_close_bg() -> None:
                        try:
                            app._close_current_popup()
                        except Exception as e:
                            bg_err.append(e)
                    bg_thread = threading.Thread(target=_call_close_bg)
                    bg_thread.start()
                    bg_thread.join(timeout=1.0)
                    self.assertEqual(len(bg_err), 0)

                    # 8. Windows Dark popup helper methods test (safe execution)
                    app._style_native_popup_menu(0)
                    app._style_native_popup_menu(12345)
                    app._setup_windows_dark_mode()
                    app._teardown_windows_hooks()
                finally:
                    app._exit()

    def test_mute_icon_state_and_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 1. Unmuted (volume = 1.0) -> icon is headphones without cross
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["headphones"]))

                    # 2. Toggle mute -> volume = 0.0 -> icon is mute with cross
                    app._toggle_mute()
                    app.update()
                    self.assertEqual(float(app.volume_var.get()), 0.0)
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["mute"]))

                    # 3. Toggle mute again -> volume restored -> icon is headphones
                    app._toggle_mute()
                    app.update()
                    self.assertGreater(float(app.volume_var.get()), 0.0)
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["headphones"]))

                    # 4. Volume changed to 0 via slider
                    app.volume_var.set(0.0)
                    app.update()
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["mute"]))

                    # 5. Volume changed to > 0 via slider
                    app.volume_var.set(0.65)
                    app.update()
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["headphones"]))
                finally:
                    app._exit()


    def test_settings_dialog_tabs_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / 'test_state.json'
            with patch.object(main.MusicPadApp, '_config_path', return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app._open_settings()
                    app.update()

                    # Check dialog properties
                    self.assertTrue(app._settings_window.winfo_exists())
                    self.assertEqual(app._settings_window.title(), 'Налаштування')

                    # Check 4 tabs
                    nb = app.settings_notebook
                    tab_names = [nb.tab(i, 'text') for i in range(len(nb.tabs()))]
                    self.assertEqual(tab_names, ['Аудіо', 'Гарячі клавіші', 'Інтерфейс', 'Пристрої'])

                    # Check Tab 1: Audio controls
                    self.assertEqual(app.norm_volume_val.get(), '89,0 дБ')
                    self.assertEqual(app.norm_mode_var.get(), 'dynamic')
                    self.assertFalse(app.block_voice_var.get())
                    self.assertFalse(app.ducking_enabled_var.get())

                    # Check Tab 2: Hotkeys table
                    hk_items = app.special_hotkeys_tree.get_children()
                    self.assertEqual(len(hk_items), 19)
                    first_action = app.special_hotkeys_tree.item(hk_items[0], 'values')[0]
                    self.assertEqual(first_action, 'Зупинити відтворення')
                    self.assertTrue(app.auto_stop_var.get())

                    # Check Tab 3: Interface controls
                    self.assertEqual(app.lang_var.get(), 'За замовчуванням')
                    self.assertEqual(app.tb_style_var.get(), 'Темний')
                    self.assertEqual(app.pos_refresh_rate_var.get(), 60)
                    self.assertTrue(app.linear_volume_slider_var.get())
                    self.assertTrue(app.typing_starts_search_var.get())
                    self.assertTrue(app.remember_playback_state_var.get())

                    # Check Tab 4: Devices (4 separate ComboBoxes)
                    self.assertTrue(hasattr(app, 'combo_phys_mic'))
                    self.assertTrue(hasattr(app, 'combo_track_source'))
                    self.assertTrue(hasattr(app, 'combo_headphones'))
                    self.assertTrue(hasattr(app, 'combo_virtual_cable'))
                    self.assertTrue(hasattr(app, 'output_combo'))
                    self.assertTrue(hasattr(app, 'settings_mic_combo'))
                    self.assertTrue(hasattr(app, 'rec_devices_tree'))
                    self.assertTrue(app.check_device_config_on_launch_var.get())
                    self.assertEqual(
                        app.track_source_var.get(),
                        "Вбудований аудіоплеєр (Локальні файли + YouTube Music)",
                    )

                    # Test Apply
                    app.norm_volume_val.set('91,0 дБ')
                    app._apply_settings()
                    self.assertTrue(app._settings_window.winfo_viewable())

                    # Test OK closes window
                    app._ok_settings()
                    self.assertFalse(app._settings_window.winfo_viewable())

                    # Test Reset
                    app._open_settings()
                    app._reset_settings_to_defaults()
                    self.assertEqual(app.norm_volume_val.get(), '89,0 дБ')
                    self.assertEqual(
                        app.track_source_var.get(),
                        "Вбудований аудіоплеєр (Локальні файли + YouTube Music)",
                    )

                    # Test Cancel closes window
                    app._cancel_settings()
                    self.assertFalse(app._settings_window.winfo_viewable())
                finally:
                    app._exit()

    def test_four_device_combos_and_state_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / 'test_state_devices.json'
            with patch.object(main.MusicPadApp, '_config_path', return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app._open_settings()
                    app.update()

                    # Verify 4 device comboboxes
                    self.assertTrue(hasattr(app, 'combo_phys_mic'))
                    self.assertTrue(hasattr(app, 'combo_track_source'))
                    self.assertTrue(hasattr(app, 'combo_headphones'))
                    self.assertTrue(hasattr(app, 'combo_virtual_cable'))

                    # Verify default values
                    self.assertEqual(
                        app.track_source_var.get(),
                        main.DEFAULT_TRACK_SOURCE,
                    )
                    sources = app._get_track_sources()
                    self.assertIn(main.DEFAULT_TRACK_SOURCE, sources)

                    # Test changing devices and applying
                    if app.output_map:
                        out_choice = list(app.output_map.keys())[0]
                        app.output_var.set(out_choice)
                    if app.input_map:
                        in_choice = list(app.input_map.keys())[0]
                        app.mix_mic_var.set(in_choice)
                    if app.monitor_map:
                        mon_choice = list(app.monitor_map.keys())[0]
                        app.monitor_var.set(mon_choice)

                    app._apply_settings()

                    # Verify persistence in config file
                    self.assertTrue(cfg_file.is_file())
                    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertEqual(saved.get("track_source"), main.DEFAULT_TRACK_SOURCE)
                    self.assertIn("settings_custom", saved)
                    self.assertEqual(saved["settings_custom"].get("track_source"), main.DEFAULT_TRACK_SOURCE)

                    # Verify reload
                    app.track_source_var.set("Custom")
                    app._load_saved_state()
                    self.assertEqual(app.track_source_var.get(), main.DEFAULT_TRACK_SOURCE)

                    # Test refresh devices
                    app._load_devices()
                    self.assertIn(main.DEFAULT_TRACK_SOURCE, app.combo_track_source["values"])
                finally:
                    app._exit()

    def test_notification_manager_and_speech_bubble_system(self) -> None:
        # 1. Test polygon generation
        pts = main._calculate_bubble_polygon(320, 86, 10, 288, 16, 12)
        self.assertGreater(len(pts), 20)
        # Tail tip should be present in polygon
        self.assertIn((288.0, 84.0), [(round(x, 1), round(y, 1)) for x, y in pts])

        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 2. Verify removal of old status label and addition of btn_b_msg
                    self.assertFalse(hasattr(app, "status_label"))
                    self.assertTrue(hasattr(app, "btn_b_msg"))
                    self.assertTrue(hasattr(app, "bottom_bar_spacer"))
                    self.assertTrue(hasattr(app, "notification_manager"))
                    self.assertIsInstance(app.status_var, main.NotificationStringVar)

                    nm = app.notification_manager
                    self.assertEqual(nm._target_widget, app.btn_b_msg)

                    # 3. Test notify_info and notify_error
                    app.notify_info("Налаштування застосовано!")
                    app.update()
                    self.assertEqual(nm.last_message, "Налаштування застосовано!")
                    self.assertEqual(nm.last_category, "info")
                    self.assertEqual(app.status_var.get(), "Налаштування застосовано!")

                    bubble = nm.bubble
                    self.assertEqual(bubble.current_message, "Налаштування застосовано!")
                    self.assertEqual(bubble.current_category, "info")
                    self.assertEqual(bubble.icon_label.cget("text"), "ℹ")
                    self.assertEqual(bubble.text_label.cget("text"), "Налаштування застосовано!")

                    app.notify_error("Збій завантаження YouTube!")
                    app.update()
                    self.assertEqual(nm.last_message, "Збій завантаження YouTube!")
                    self.assertEqual(nm.last_category, "error")
                    self.assertEqual(app.status_var.get(), "Збій завантаження YouTube!")
                    self.assertEqual(bubble.icon_label.cget("text"), "✕")

                    # 4. Test backwards compatibility with status_var.set
                    app.status_var.set("Дію скасовано.")
                    app.update()
                    self.assertEqual(nm.last_message, "Дію скасовано.")
                    self.assertEqual(nm.last_category, "info")
                    self.assertEqual(bubble.icon_label.cget("text"), "ℹ")

                    app.status_var.set("Помилка аудіопотоку")
                    app.update()
                    self.assertEqual(nm.last_message, "Помилка аудіопотоку")
                    self.assertEqual(nm.last_category, "error")
                    self.assertEqual(bubble.icon_label.cget("text"), "✕")

                    # Empty string does not trigger notification
                    app.status_var.set("")
                    self.assertEqual(nm.last_message, "Помилка аудіопотоку")

                    # 5. Test theme adaptation (Dark vs Light)
                    app.theme = "dark"
                    bubble.set_theme("dark")
                    bubble._redraw()
                    dark_colors = bubble._get_colors()
                    self.assertEqual(dark_colors["bubble_bg"], "#232323")
                    self.assertEqual(dark_colors["text"], "#ffffff")

                    app.theme = "light"
                    bubble.set_theme("light")
                    bubble._redraw()
                    light_colors = bubble._get_colors()
                    self.assertEqual(light_colors["bubble_bg"], "#ffffff")
                    self.assertEqual(light_colors["text"], "#1a1a1a")

                    app._apply_theme()
                    self.assertEqual(bubble.theme, "light")

                    # 6. Test toggle bubble via bottom-right message button
                    nm.dismiss(immediate=True)
                    app.update()
                    self.assertFalse(nm.is_visible)

                    app._toggle_notification_bubble()
                    app.update()
                    self.assertTrue(nm.is_visible)
                    self.assertEqual(bubble.current_message, "Помилка аудіопотоку")

                    app._toggle_notification_bubble()
                    nm.dismiss(immediate=True)
                    app.update()
                    self.assertFalse(nm.is_visible)

                    # 7. Test rapid notifications update (in-place text & category update without flicker)
                    app.notify_info("Перше сповіщення")
                    app.notify_info("Друге сповіщення")
                    app.notify_error("Третє швидке сповіщення")
                    app.update()
                    self.assertEqual(bubble.current_message, "Третє швидке сповіщення")
                    self.assertEqual(bubble.current_category, "error")
                    # Alpha should remain 1.0 (no dimming/flickering to 0.25 on rapid update)
                    self.assertAlmostEqual(float(bubble.attributes("-alpha")), 1.0, places=2)

                    # 8. Test immediate click to dismiss
                    bubble.hide_immediate()
                    app.update()
                    self.assertFalse(nm.is_visible)

                    # 9. Test multi-monitor positioning with negative coordinates
                    app.geometry("960x680+-1000+200")
                    app.update()
                    bubble._reposition()
                    geom = bubble.geometry()
                    self.assertTrue("+-" in geom or "320x86+-" in geom)

                    # Restore normal geometry
                    app.geometry("960x680+100+100")
                    app.update()

                    # 10. Test minimized / withdrawn state suppression (no floating ghost bubble)
                    app.withdraw()
                    app.notify_info("Повідомлення у фоні")
                    app.update()
                    self.assertEqual(nm.last_message, "Повідомлення у фоні")
                    self.assertFalse(nm.is_visible)
                    app.deiconify()
                    app.update()

                    # 11. Test thread-safe notification from worker thread (both app.notify and status_var.set)
                    bg_done = threading.Event()

                    def worker() -> None:
                        app.notify("Сповіщення з потоку", "info")
                        app.status_var.set("Сповіщення через status_var з потоку")
                        bg_done.set()

                    t = threading.Thread(target=worker, daemon=True)
                    t.start()
                    t.join(timeout=2.0)
                    self.assertTrue(bg_done.is_set())

                    for _ in range(15):
                        app.update()
                        if nm.last_message == "Сповіщення через status_var з потоку":
                            break
                        time.sleep(0.02)
                    self.assertEqual(nm.last_message, "Сповіщення через status_var з потоку")
                finally:
                    app._exit()

    def test_complete_light_dark_theme_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_theme_state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 1. Dark Theme Initial Verification
                    self.assertEqual(app.theme, "dark")
                    c_dark = app._get_theme_colors()
                    self.assertEqual(app.cget("bg"), c_dark["bg"])
                    self.assertEqual(app.top_bar.cget("bg"), c_dark["top_bg"])
                    self.assertEqual(app.left_panel.cget("bg"), c_dark["panel_bg"])
                    self.assertEqual(app.table_container.cget("bg"), c_dark["table_bg"])
                    self.assertEqual(app.info_panel.cget("bg"), c_dark["info_bg"])
                    self.assertEqual(app.bottom_bar.cget("bg"), c_dark["bottom_bg"])
                    self.assertEqual(app.btn_wrench.cget("bg"), c_dark["info_bg"])
                    self.assertEqual(app.seek_scale.theme, "dark")
                    self.assertEqual(app.volume_scale.theme, "dark")
                    self.assertEqual(app.cat_item_frame.cget("bg"), c_dark["cat_sel_bg"])

                    # YouTube panel in dark theme
                    self.assertEqual(app.yt_search_frame.cget("bg"), c_dark["table_bg"])
                    self.assertEqual(app.yt_titlebar.cget("bg"), c_dark["hdr_bg"])
                    self.assertEqual(app.yt_top.cget("bg"), c_dark["table_bg"])
                    self.assertEqual(app.yt_bottom.cget("bg"), c_dark["table_bg"])
                    self.assertEqual(app.btn_yt_play.cget("bg"), c_dark["top_bg"])

                    # Recursive menus in dark theme
                    for child in app.menubar.winfo_children():
                        if isinstance(child, main.tk.Menu):
                            self.assertEqual(str(child.cget("bg")), c_dark["menu_bg"])

                    # 2. Toggle to Light Theme
                    app._toggle_theme()
                    app.update()
                    self.assertEqual(app.theme, "light")
                    c_light = app._get_theme_colors()
                    self.assertEqual(app.cget("bg"), "#f4f5f7")
                    self.assertEqual(app.top_bar.cget("bg"), "#eaedf1")
                    self.assertEqual(app.left_panel.cget("bg"), "#eceff2")
                    self.assertEqual(app.table_container.cget("bg"), "#ffffff")
                    self.assertEqual(app.info_panel.cget("bg"), "#e6e9ee")
                    self.assertEqual(app.bottom_bar.cget("bg"), "#e0e3e8")
                    self.assertEqual(app.btn_wrench.cget("bg"), "#e6e9ee")
                    self.assertEqual(app.seek_scale.theme, "light")
                    self.assertEqual(app.volume_scale.theme, "light")
                    self.assertEqual(app.cat_item_frame.cget("bg"), "#d0e4f7")

                    # YouTube panel completely light
                    self.assertEqual(app.yt_search_frame.cget("bg"), "#ffffff")
                    self.assertEqual(app.yt_titlebar.cget("bg"), "#e0e3e8")
                    self.assertEqual(app.yt_top.cget("bg"), "#ffffff")
                    self.assertEqual(app.yt_bottom.cget("bg"), "#ffffff")
                    self.assertEqual(app.btn_yt_play.cget("bg"), "#eaedf1")

                    # Recursive menus completely light
                    for child in app.menubar.winfo_children():
                        if isinstance(child, main.tk.Menu):
                            self.assertEqual(str(child.cget("bg")), "#eaedf1")

                    # ComboBox in Settings verifies light listbox popup
                    app._open_settings()
                    app.update()
                    cb = app.combo_track_source
                    app.tk.eval(f"ttk::combobox::PopdownWindow {cb}")
                    lb = f"{cb}.popdown.f.l"
                    self.assertEqual(app.tk.eval(f"{lb} cget -background"), "#ffffff")
                    self.assertEqual(app.tk.eval(f"{lb} cget -foreground"), "#20242b")

                    # 3. Toggle back to Dark Theme
                    app._toggle_theme()
                    app.update()
                    self.assertEqual(app.theme, "dark")
                    self.assertEqual(app.cget("bg"), "#1f1f1f")
                    self.assertEqual(app.top_bar.cget("bg"), "#212121")
                    self.assertEqual(app.left_panel.cget("bg"), "#222222")
                    self.assertEqual(app.table_container.cget("bg"), "#1e1e1e")
                    self.assertEqual(app.info_panel.cget("bg"), "#262626")
                    self.assertEqual(app.bottom_bar.cget("bg"), "#1c1c1c")
                    self.assertEqual(app.btn_wrench.cget("bg"), "#262626")
                    self.assertEqual(app.seek_scale.theme, "dark")
                    self.assertEqual(app.volume_scale.theme, "dark")
                    self.assertEqual(app.yt_search_frame.cget("bg"), "#1e1e1e")
                    self.assertEqual(app.yt_titlebar.cget("bg"), "#292929")

                    # Popdown listbox dynamically reverted to dark
                    self.assertEqual(app.tk.eval(f"{lb} cget -background"), "#262626")
                    self.assertEqual(app.tk.eval(f"{lb} cget -foreground"), "#ffffff")
                finally:
                    app._exit()

    def test_settings_dialog_size_scrolling_and_pinned_bottom_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_settings_scroll.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app._open_settings()
                    app.update()

                    # 1. Dialog Geometry & Minsize (640x760, minsize 560x600)
                    min_w, min_h = app._settings_window.minsize()
                    self.assertEqual((min_w, min_h), (560, 600))
                    geom = app._settings_window.geometry()
                    self.assertTrue(geom.startswith("640x760"), f"Expected 640x760 geometry, got {geom}")

                    # 2. Scrollable Canvases on all 4 tabs
                    self.assertEqual(len(app._settings_canvases), 4)
                    for cnv in app._settings_canvases:
                        self.assertIsInstance(cnv, main.tk.Canvas)
                        self.assertTrue(cnv.winfo_exists())

                    # 3. Bottom Bar firmly pinned outside scrollable canvases
                    self.assertTrue(hasattr(app, "settings_bottom_bar"))
                    self.assertIsInstance(app.settings_bottom_bar, main.ttk.Frame)
                    self.assertEqual(app.settings_bottom_bar.master, app._settings_window)

                    # All 4 action buttons exist
                    self.assertEqual(app.btn_settings_reset.cget("text"), "Скинути")
                    self.assertEqual(app.btn_settings_ok.cget("text"), "OK")
                    self.assertEqual(app.btn_settings_cancel.cget("text"), "Скасувати")
                    self.assertEqual(app.btn_settings_apply.cget("text"), "Застосувати")

                    # 4. Mousewheel binding exists on the settings window
                    bindings = app._settings_window.bind()
                    self.assertIn("<MouseWheel>", bindings)
                finally:
                    app._exit()

    def test_wrench_icon_and_info_panel_settings_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_wrench_icon.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 1. Wrench icon exists in _icons
                    self.assertIn("wrench", app._icons)
                    self.assertIsNotNone(app._icons["wrench"])

                    # 2. Settings button on info panel uses the wrench icon
                    self.assertTrue(hasattr(app, "btn_wrench"))
                    self.assertTrue(str(app.btn_wrench.cget("image")).strip() != "")

                    # 3. Button click opens settings dialog
                    self.assertFalse(app._settings_window.winfo_viewable())
                    app.btn_wrench.invoke()
                    app.update()
                    self.assertTrue(app._settings_window.winfo_viewable())
                    self.assertEqual(app._settings_window.title(), "Налаштування")
                finally:
                    app._exit()

    def test_compiled_exe_window_and_self_test(self) -> None:
        import subprocess

        exe_path = Path(main.__file__).resolve().parent / "dist" / "MusicPad.exe"
        if not exe_path.is_file():
            self.skipTest("dist/MusicPad.exe not present")

        with tempfile.TemporaryDirectory() as tmp:
            rep_file = Path(tmp) / "self_test_report.json"
            proc_st = subprocess.run(
                [str(exe_path), "--self-test", str(rep_file)],
                timeout=45,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(proc_st.returncode, 0)
            rep = json.loads(rep_file.read_text(encoding="utf-8"))
            self.assertTrue(rep.get("ok"), f"Self-test failed: {rep}")
            self.assertTrue(rep.get("frozen"))
            self.assertEqual(rep.get("window_title"), "MusicPad")
            self.assertTrue(rep.get("window_exists"))
            self.assertEqual(rep.get("sd_module"), "sounddevice")
            self.assertTrue(rep.get("has_wave"))
            self.assertTrue(rep.get("has_numpy"))
            self.assertTrue(rep.get("finished"))
            self.assertTrue(rep.get("finished_48k"))
            self.assertEqual(rep.get("errors"), [])
            self.assertEqual(
                Path(rep["config_path"]),
                Path(main.__file__).resolve().parent / ".cache" / "musicpad_state.json",
            )
            self.assertEqual(
                Path(rep["cache_dir"]),
                Path(main.__file__).resolve().parent / ".cache" / "youtube",
            )


if __name__ == "__main__":
    unittest.main()
