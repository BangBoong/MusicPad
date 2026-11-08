"""Comprehensive unit and integration tests for MusicPad."""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import threading
import time
import tkinter as tk
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
    ROUTING_MODE_INJECTION,
    ROUTING_MODE_VIRTUAL_CABLE,
    SAMPLE_RATE,
    AudioEngine,
    AudioEngineError,
    StreamTrack,
)
from musicpad.apo_installer import (
    MUSICPAD_APO_CLSID,
    CaptureEndpoint,
    DriverStatus,
    _delete_hklm_subkey_tree,
    _enable_backup_restore_privileges,
    _ensure_win32_prototypes,
    _open_hklm_key_backup_restore,
    find_endpoint_for_sounddevice,
    get_driver_status,
    run_driver_cli,
)
from musicpad.dsp_core import DspCore
from musicpad.injection_shm import (
    InjectionShm,
    MPAD_HEARTBEAT_TIMEOUT_NS,
    MPAD_RING_CAPACITY_SAMPLES,
    MPAD_WRITER_LEASE_TIMEOUT_NS,
    OFF_READ_SAMPLE_IDX,
    OFF_WRITE_SAMPLE_IDX,
)
from musicpad.library import LocalTrack, format_album_duration, format_duration, scan_file, scan_folder


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

    def test_format_album_duration_cases(self) -> None:
        self.assertEqual(format_album_duration(12, 2880), "12 тр. • 48 хв")
        self.assertEqual(format_album_duration(14, 3660), "14 тр. • 1 год 1 хв")
        self.assertEqual(format_album_duration(10, duration_str="1 hour, 15 minutes"), "10 тр. • 1 год 15 хв")
        self.assertEqual(format_album_duration(8, duration_str="50 min"), "8 тр. • 50 хв")
        self.assertEqual(format_album_duration(5), "5 тр.")
        self.assertEqual(format_album_duration(None, 120), "2 хв")
        self.assertEqual(format_album_duration(None, None, ""), "")

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


class TestDspCoreAndInjectionShm(unittest.TestCase):
    def test_dsp_core_voice_chain_mix_and_clipping(self) -> None:
        import numpy as np

        dsp = DspCore()
        self.assertTrue(dsp.is_native_loaded)

        mic = np.array([[0.25, -0.25], [0.95, -0.95]], dtype=np.float32)
        track = np.array([[0.25, 0.50], [0.80, -0.80]], dtype=np.float32)
        mixed = dsp.mix_voice_and_track(mic, track, frames=2, channels=2, mic_gain=1.0, mic_mute=False)
        self.assertAlmostEqual(float(mixed[0, 0]), 0.50, places=5)
        self.assertAlmostEqual(float(mixed[0, 1]), 0.25, places=5)
        self.assertAlmostEqual(float(mixed[1, 0]), 1.0, places=5)
        self.assertAlmostEqual(float(mixed[1, 1]), -1.0, places=5)

        muted = dsp.mix_voice_and_track(mic, track, frames=2, channels=2, mic_gain=1.5, mic_mute=True)
        self.assertAlmostEqual(float(muted[0, 0]), 0.25, places=5)
        self.assertAlmostEqual(float(muted[0, 1]), 0.50, places=5)

    def test_injection_shm_ring_buffer_wrap_flush_and_lease(self) -> None:
        import struct
        import numpy as np

        dsp = DspCore()
        with tempfile.TemporaryDirectory() as tmp:
            shm_path = Path(tmp) / "test_injection_v1.shm"
            shm = InjectionShm(path=shm_path, pid=1111)
            shm_other = InjectionShm(path=shm_path, pid=2222)
            try:
                self.assertTrue(shm.open(acquire_lease=True))
                shm.touch_heartbeat(now_ns=1_000_000_000)

                # Contending writer with different PID while heartbeat is fresh should be rejected
                shm_other.open(acquire_lease=False)
                with patch("musicpad.injection_shm._is_pid_running", return_value=True):
                    self.assertFalse(shm_other.acquire_writer_lease(now_ns=1_000_000_000 + 10_000_000))
                    # After lease timeout (>600ms), new writer can take over lease
                    self.assertTrue(
                        shm_other.acquire_writer_lease(
                            now_ns=1_000_000_000 + MPAD_WRITER_LEASE_TIMEOUT_NS + 5_000_000
                        )
                    )
                shm_other.release_writer_lease()
                self.assertTrue(shm.acquire_writer_lease(now_ns=2_000_000_000))

                # Test wrap-around near MPAD_RING_CAPACITY_SAMPLES
                assert shm._mmap is not None
                near_end = MPAD_RING_CAPACITY_SAMPLES - 32
                struct.pack_into("<Q", shm._mmap, OFF_WRITE_SAMPLE_IDX, near_end)
                struct.pack_into("<Q", shm._mmap, OFF_READ_SAMPLE_IDX, near_end)
                frames = np.arange(64 * 2, dtype=np.float32).reshape(64, 2) / 200.0
                written = shm.write_samples(frames, now_ns=2_000_000_000)
                self.assertEqual(written, 128)
                self.assertEqual(shm.available_to_read(), 128)

                # Simulate APO processing via shared C core
                shm.set_state(app_active=True, mic_mute=False, mic_gain=1.0, track_playing=True, now_ns=2_000_000_000)
                mic_zero = np.zeros((64, 2), dtype=np.float32)
                out_arr, active, last_flush = dsp.simulate_apo_shm_block(
                    shm._mmap, mic_zero, sample_rate=48000.0, now_ns=2_010_000_000, last_flush_seq=0
                )
                self.assertEqual(active, 1)
                self.assertTrue(np.allclose(out_arr, frames, atol=1e-6))
                self.assertEqual(shm.available_to_read(), 0)

                # Write again and verify flush() causes APO to discard buffered samples on next block
                shm.write_samples(frames, now_ns=2_020_000_000)
                self.assertEqual(shm.available_to_read(), 128)
                shm.flush()
                out_flushed, _, last_flush = dsp.simulate_apo_shm_block(
                    shm._mmap, mic_zero, sample_rate=48000.0, now_ns=2_030_000_000, last_flush_seq=last_flush
                )
                self.assertEqual(shm.available_to_read(), 0)
                self.assertTrue(np.allclose(out_flushed, 0.0, atol=1e-6))

                # Test reset_storage wipes buffer, offsets and stats
                shm.write_samples(np.ones(64, dtype=np.float32))
                self.assertGreater(shm.available_to_read(), 0)
                shm.reset_storage()
                self.assertEqual(shm.available_to_read(), 0)
                self.assertEqual(shm.get_apo_status().underrun_count, 0)
            finally:
                shm_other.close()
                shm.close()

    def test_apo_installer_helpers_and_cli(self) -> None:
        fake_eps = [
            CaptureEndpoint(
                guid="{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
                endpoint_name="Мікрофон",
                adapter_name="3- fifine Microphone",
                enhancements_enabled=True,
                exclusive_allowed=False,
                efx_clsid=MUSICPAD_APO_CLSID,
            )
        ]
        ep = find_endpoint_for_sounddevice("Мікрофон (3- fifine Microphone)", endpoints=fake_eps)
        self.assertIsNotNone(ep)
        assert ep is not None
        self.assertEqual(ep.guid, "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}")

        with patch("musicpad.apo_installer.install_driver") as mock_inst, patch(
            "musicpad.apo_installer.uninstall_driver"
        ) as mock_uninst:
            self.assertEqual(run_driver_cli("install", "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}"), 0)
            self.assertEqual(run_driver_cli("repair", "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}"), 0)
            self.assertEqual(run_driver_cli("uninstall"), 0)
            self.assertEqual(run_driver_cli("unknown"), 2)
            self.assertEqual(mock_inst.call_count, 2)
            mock_uninst.assert_called_once()

        # Win32 privilege & fallback tests
        _ensure_win32_prototypes()
        _enable_backup_restore_privileges()

        with patch("ctypes.windll.advapi32.RegCreateKeyExW") as mock_reg:
            def fake_create(hkey, subkey, res, cls, opts, sam, sec, phk, pdisp):
                if opts == 4:  # REG_OPTION_BACKUP_RESTORE
                    return 5  # ERROR_ACCESS_DENIED
                phk._obj.value = 12345
                return 0

            mock_reg.side_effect = fake_create
            h = _open_hklm_key_backup_restore("SOFTWARE\\MusicPadFake")
            self.assertEqual(h, 12345)
            self.assertEqual(mock_reg.call_count, 2)

        with patch("ctypes.windll.advapi32.RegCreateKeyExW", return_value=5):
            with patch("winreg.CreateKeyEx") as mock_winreg:
                mock_k = MagicMock()
                mock_k.Detach.return_value = 54321
                mock_winreg.return_value = mock_k
                h3 = _open_hklm_key_backup_restore("SOFTWARE\\MusicPadFake3")
                self.assertEqual(h3, 54321)

        with patch("ctypes.windll.advapi32.RegDeleteTreeW", return_value=2):
            _delete_hklm_subkey_tree("SOFTWARE\\MusicPadFakeDelete")


class TestAudioEngineUnit(unittest.TestCase):
    def setUp(self) -> None:
        AudioEngine.clear_cache()

    def test_reinitialize_audio_subsystem(self) -> None:
        with patch("sounddevice._terminate") as mock_term, patch("sounddevice._initialize") as mock_init:
            engine = AudioEngine(shm_path=None)
            engine.reinitialize_audio_subsystem()
            mock_term.assert_called_once()
            mock_init.assert_called_once()
            engine.shutdown()

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

        # Multi-reader testing (decoupled injection vs monitor)
        rb.reset()
        rb.register_reader(1)
        self.assertEqual(rb.available(0), 0)
        self.assertEqual(rb.available(1), 0)
        rb.write(w_data)  # 500 frames
        self.assertEqual(rb.available(0), 500)
        self.assertEqual(rb.available(1), 500)

        # Reader 0 reads ahead by 300 frames
        r0, n0 = rb.read(300, reader_id=0)
        self.assertEqual(n0, 300)
        self.assertEqual(rb.available(0), 200)
        self.assertEqual(rb.available(1), 500)  # Reader 1 still has all 500 frames

        # Reader 1 reads 200 frames from the start
        r1, n1 = rb.read(200, reader_id=1)
        self.assertEqual(n1, 200)
        self.assertEqual(rb.available(1), 300)
        np.testing.assert_allclose(r1, r0[:200])

        # Write space is governed by master reader
        self.assertEqual(rb.write_space(), 1024 - 200)  # Reader 0 is default master (read 300)
        rb.set_master_reader(1)
        self.assertEqual(rb.write_space(), 1024 - 300)  # Reader 1 is master (read 200)
        rb.unregister_reader(1)

    def test_audio_normalization_clipping_and_downmix(self) -> None:
        import numpy as np
        from musicpad.audio_engine import Float32RingBuffer

        # 1. Float32RingBuffer int16 normalization and clipping
        rb = Float32RingBuffer(capacity_frames=256, channels=2)
        int16_data = np.array([[32767, -32768], [16384, -16384]], dtype=np.int16)
        written = rb.write(int16_data)
        self.assertEqual(written, 2)
        read_arr, n_read = rb.read(2)
        self.assertEqual(n_read, 2)
        self.assertEqual(read_arr.dtype, np.float32)
        self.assertAlmostEqual(float(read_arr[0, 0]), 32767 / 32768.0, places=4)
        self.assertAlmostEqual(float(read_arr[0, 1]), -1.0, places=4)
        self.assertAlmostEqual(float(read_arr[1, 0]), 0.5, places=4)
        self.assertAlmostEqual(float(read_arr[1, 1]), -0.5, places=4)

        # 2. Hard clipping on excessive amplitude (> 1.0 or < -1.0)
        loud_data = np.array([[50.0, -100.0], [0.5, -0.5]], dtype=np.float32)
        rb.reset()
        rb.write(loud_data)
        clipped_read, _ = rb.read(2)
        self.assertEqual(float(clipped_read[0, 0]), 1.0)
        self.assertEqual(float(clipped_read[0, 1]), -1.0)

        # 3. Mono-to-stereo expansion in buffer
        mono_data = np.array([[0.75], [-0.25]], dtype=np.float32)
        rb.reset()
        rb.write(mono_data)
        stereo_read, _ = rb.read(2)
        self.assertEqual(stereo_read.shape, (2, 2))
        self.assertEqual(float(stereo_read[0, 0]), 0.75)
        self.assertEqual(float(stereo_read[0, 1]), 0.75)

        # 4. Stereo-to-mono downmixing in 1-channel buffer
        mono_rb = Float32RingBuffer(capacity_frames=256, channels=1)
        stereo_in = np.array([[0.4, 0.8], [-0.6, -0.2]], dtype=np.float32)
        mono_rb.write(stereo_in)
        mono_read, _ = mono_rb.read(2)
        self.assertEqual(mono_read.shape, (2, 1))
        self.assertAlmostEqual(float(mono_read[0, 0]), 0.6, places=5)
        self.assertAlmostEqual(float(mono_read[1, 0]), -0.4, places=5)

        # 5. resample_float32_channels normalization, downmixing & clipping
        resampled_mono = AudioEngine.resample_float32_channels(
            stereo_in,
            source_rate=48000,
            target_rate=48000,
            target_channels=1,
        )
        self.assertEqual(resampled_mono.shape, (2, 1))
        self.assertAlmostEqual(float(resampled_mono[0, 0]), 0.6, places=5)

        # int16 input to resample_float32_channels
        int16_resampled = AudioEngine.resample_float32_channels(
            int16_data,
            source_rate=44100,
            target_rate=48000,
            target_channels=1,
        )
        self.assertEqual(int16_resampled.dtype, np.float32)
        self.assertTrue(np.all(int16_resampled >= -1.0))
        self.assertTrue(np.all(int16_resampled <= 1.0))

        # 6. InjectionShm.write_samples with int16 and unclipped float
        with tempfile.TemporaryDirectory() as tmp:
            test_shm_path = Path(tmp) / "test_norm_shm.bin"
            test_shm = InjectionShm(path=test_shm_path, pid=9999)
            self.assertTrue(test_shm.open(acquire_lease=True))
            # Writing int16 directly
            w_int = test_shm.write_samples(int16_data)
            self.assertEqual(w_int, 4)
            # Writing excessive float (> 1.0)
            w_loud = test_shm.write_samples(np.array([10.0, -20.0], dtype=np.float32))
            self.assertEqual(w_loud, 2)
            test_shm.close()

        # 7. AudioEngine._raw_to_float32 clipping
        raw_pcm = array("h", [32767, -32768, 0, 16384]).tobytes()
        conv_float = AudioEngine._raw_to_float32(raw_pcm, channels=2)
        self.assertEqual(conv_float.shape, (2, 2))
        self.assertEqual(conv_float.dtype, np.float32)
        self.assertTrue(np.all(conv_float >= -1.0))
        self.assertTrue(np.all(conv_float <= 1.0))

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


    def test_audio_engine_search_youtube_limit_and_metadata(self) -> None:
        mock_songs = [
            {
                "videoId": f"vid_ytm_{i}",
                "title": f"Song {i}",
                "artists": [{"name": f"Artist {i}"}],
                "duration": "3:20",
                "duration_seconds": 200,
                "thumbnails": [{"url": f"https://ytm.example.com/thumb_{i}.jpg", "width": 120, "height": 120}],
            }
            for i in range(25)
        ]
        mock_ytmusic_instance = MagicMock()
        mock_ytmusic_instance.search.side_effect = lambda query, filter: mock_songs if filter == "songs" else []
        with patch("musicpad.audio_engine.YTMusic", return_value=mock_ytmusic_instance):
            results = AudioEngine.search_youtube("test query", limit=25)
            self.assertEqual(len(results), 25)
            first = results[0]
            self.assertEqual(first["title"], "Song 0")
            self.assertEqual(first["artist"], "Artist 0")
            self.assertEqual(first["full_title"], "Song 0 — Artist 0")
            self.assertEqual(first["source"], "ytmusic")
            self.assertEqual(first["thumbnail_url"], "https://ytm.example.com/thumb_0.jpg")
            self.assertEqual(first["duration"], 200.0)

    def test_audio_engine_fetch_thumbnail_cached_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("musicpad.audio_engine.AudioEngine._thumbnail_cache_dir", return_value=Path(tmp_dir)):
                self.assertIsNone(AudioEngine.fetch_thumbnail(""))

                from PIL import Image
                import io

                fake_img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
                buf = io.BytesIO()
                fake_img.save(buf, format="PNG")
                raw_bytes = buf.getvalue()

                mock_resp = MagicMock()
                mock_resp.read.return_value = raw_bytes
                mock_resp.__enter__.return_value = mock_resp
                mock_resp.__exit__.return_value = False

                with patch("urllib.request.urlopen", return_value=mock_resp):
                    path = AudioEngine.fetch_thumbnail("https://example.com/img.jpg", "mock_vid_1")
                    self.assertIsNotNone(path)
                    self.assertTrue(os.path.isfile(path))

                    with patch("urllib.request.urlopen", side_effect=AssertionError("Should use cache")):
                        cached_path = AudioEngine.fetch_thumbnail("https://example.com/img.jpg", "mock_vid_1")
                        self.assertEqual(path, cached_path)


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
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    self.assertEqual(app.info_house_lbl.cget("image"), str(app._icons.get("yt_icon", "")))
                    self.assertEqual(app.stats_frame.winfo_manager(), "")

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
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.info_house_lbl.cget("image"), str(app._icons.get("house_lg", "")))
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")
                    self.assertEqual(app.stats_sounds_lbl.cget("text"), "1")
                finally:
                    app._exit()

    def test_youtube_search_results_redesign_and_metadata_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    mock_results = [
                        {
                            "title": "Starboy",
                            "artist": "The Weeknd",
                            "full_title": "Starboy — The Weeknd",
                            "url": "https://music.youtube.com/watch?v=starboy1",
                            "duration": 230.0,
                            "source": "ytmusic",
                            "thumbnail_url": "https://example.com/starboy.jpg",
                        },
                        {
                            "title": "Believer",
                            "artist": "Imagine Dragons",
                            "full_title": "Believer — Imagine Dragons",
                            "url": "https://www.youtube.com/watch?v=believer2",
                            "duration": 204.0,
                            "source": "youtube",
                            "thumbnail_url": "https://example.com/believer.jpg",
                        },
                    ]
                    app._render_yt_results(mock_results)
                    self.assertEqual(len(app.yt_tree.get_children()), 2)
                    self.assertEqual(app.yt_tree.hdr_lbl_title.cget("text"), "Назва")
                    self.assertEqual(app.yt_tree.hdr_lbl_artist.cget("text"), "Виконавець")
                    self.assertEqual(app.yt_tree.hdr_lbl_duration.cget("text"), "Тривалість")
                    self.assertEqual(app.yt_tree.hdr_lbl_source.cget("text"), "Джерело")
                    self.assertEqual(app.yt_tree.hdr_lbl_cover.cget("text"), "Обкладинка")

                    row0_widgets = app.yt_tree._row_widgets["0"]
                    self.assertEqual(row0_widgets["title"].cget("text"), "Starboy")
                    self.assertEqual(row0_widgets["artist"].cget("text"), "The Weeknd")
                    self.assertEqual(row0_widgets["source"].cget("text"), "YTMusic")

                    row1_widgets = app.yt_tree._row_widgets["1"]
                    self.assertEqual(row1_widgets["title"].cget("text"), "Believer")
                    self.assertEqual(row1_widgets["artist"].cget("text"), "Imagine Dragons")
                    self.assertEqual(row1_widgets["source"].cget("text"), "YT")

                    app.yt_tree._on_arrow_down()
                    self.assertEqual(app.yt_tree.selection(), ("1",))
                    app.yt_tree._on_arrow_up()
                    self.assertEqual(app.yt_tree.selection(), ("0",))

                    app.yt_tree.selection_set("0")
                    app._yt_play_picked(add_only=True)
                    self.assertEqual(len(app.local_tracks), 1)
                    added = app.local_tracks[0]
                    self.assertEqual(added.title, "Starboy — The Weeknd")
                    self.assertEqual(added.artist, "The Weeknd")
                    self.assertEqual(added.source, "ytmusic")

                    # Verify column fixed widths for alignment
                    self.assertEqual(app.yt_tree.h_c1.cget("width"), 180)
                    self.assertEqual(app.yt_tree.h_c2.cget("width"), 80)
                    self.assertEqual(app.yt_tree.h_c3.cget("width"), 80)
                    self.assertEqual(app.yt_tree.h_c4.cget("width"), 100)
                    self.assertEqual(row0_widgets["cell_frames"][1].cget("width"), 180)
                    self.assertEqual(row0_widgets["cell_frames"][2].cget("width"), 80)
                    self.assertEqual(row0_widgets["cell_frames"][3].cget("width"), 80)
                    self.assertEqual(row0_widgets["cell_frames"][4].cget("width"), 100)

                    # Verify category sidebar has no '#' header and no count labels
                    self.assertIsNone(app.cat_hdr_lbl2)
                    self.assertIsNone(app.category_count_lbl)
                    self.assertIsNone(app.cat_yt_count_lbl)

                    # Verify YouTube panel placement covers table header (y=0)
                    app._show_yt_search_panel()
                    place_info = app.yt_search_frame.place_info()
                    self.assertEqual(int(place_info.get("y", -1)), 0)
                finally:
                    app._exit()

    def test_unified_my_sounds_track_list_and_56px_row_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. Verify track_list is TrackListView instance
                    self.assertIsInstance(app.track_list, main.TrackListView)

                    # 2. Add local tracks with artist and hotkey
                    t1 = main.LocalTrack(
                        title="Never Too Late",
                        path="C:/fake/never.mp3",
                        duration=209.0,
                        artist="Three Days Grace",
                        hotkey="Alt+6",
                        source="file",
                    )
                    t2 = main.LocalTrack(
                        title="Animal I Have Become",
                        path="https://music.youtube.com/watch?v=123",
                        duration=231.0,
                        is_url=True,
                        artist="Three Days Grace",
                        source="ytmusic",
                    )
                    app._set_tracks([t1, t2])
                    app.update()

                    # 3. Verify columns and row count
                    self.assertEqual(len(app.track_list.get_children()), 2)
                    self.assertEqual(app.track_list.hdr_lbl_title.cget("text"), "Назва")
                    self.assertEqual(app.track_list.hdr_lbl_artist.cget("text"), "Виконавець")
                    self.assertEqual(app.track_list.hdr_lbl_duration.cget("text"), "Тривалість")
                    self.assertEqual(app.track_list.hdr_lbl_source.cget("text"), "Джерело")
                    self.assertEqual(app.track_list.hdr_lbl_cover.cget("text"), "Обкладинка")

                    # 4. Verify 56px row height
                    row0 = app.track_list._row_frames["0"]
                    self.assertEqual(row0.cget("height"), 56)

                    # 5. Verify row content and hotkey display
                    w0 = app.track_list._row_widgets["0"]
                    self.assertIn("Never Too Late", w0["title"].cget("text"))
                    self.assertIn("[Alt+6]", w0["title"].cget("text"))
                    self.assertEqual(w0["artist"].cget("text"), "Three Days Grace")
                    self.assertEqual(w0["duration"].cget("text"), "3:29")
                    self.assertEqual(w0["source"].cget("text"), "Файл")

                    # 6. Verify row 1 (ytmusic)
                    w1 = app.track_list._row_widgets["1"]
                    self.assertIn("Animal I Have Become", w1["title"].cget("text"))
                    self.assertEqual(w1["source"].cget("text"), "YTMusic")

                    # 7. Verify local cover extraction helper
                    from musicpad.library import extract_embedded_cover
                    dummy_wav = Path(tmp) / "empty.wav"
                    dummy_wav.write_bytes(bytes([82, 73, 70, 70]) + bytes(40))
                    self.assertIsNone(extract_embedded_cover(dummy_wav, cache_dir=tmp))
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
                    # 1. Removed toolbar buttons are absent and Play/Pause button remains unaffected
                    self.assertFalse(hasattr(app, "btn_tb_play"))
                    self.assertFalse(hasattr(app, "btn_tb_phones"))
                    self.assertFalse(hasattr(app, "btn_tb_mic"))
                    self.assertEqual(app.pause_button.cget("image"), str(app._icons["play"]))

                    # 2. Unmuted (volume = 1.0) -> icon is single musical note
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["note"]))
                    self.assertEqual(app._icons["mute"], app._icons["note_mute"])

                    # 3. Toggle mute -> volume = 0.0 -> icon is note with red X
                    app._toggle_mute()
                    app.update()
                    self.assertEqual(float(app.volume_var.get()), 0.0)
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["note_mute"]))
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["mute"]))

                    # 4. Toggle mute again -> volume restored -> icon returns to note
                    app._toggle_mute()
                    app.update()
                    self.assertGreater(float(app.volume_var.get()), 0.0)
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["note"]))

                    # 5. Volume changed to 0 via slider trace
                    app.volume_var.set(0.0)
                    app.update()
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["note_mute"]))

                    # 6. Volume changed to > 0 via slider trace
                    app.volume_var.set(0.65)
                    app.update()
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["note"]))

                    # 7. Fallback when PIL icons are missing
                    orig_icons = app._icons
                    app._icons = {}
                    app._update_mute_icon()
                    self.assertEqual(app.btn_tb_mute.cget("text"), "♪")
                    app.volume_var.set(0.0)
                    app._update_mute_icon()
                    self.assertEqual(app.btn_tb_mute.cget("text"), "♪✕")
                    self.assertEqual(app.btn_tb_mute.cget("fg"), "#ff2020")
                    app._icons = orig_icons
                    app.volume_var.set(0.8)
                    app._update_mute_icon()
                    self.assertEqual(app.btn_tb_mute.cget("image"), str(app._icons["note"]))
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

                    # Check 6 tabs
                    nb = app.settings_notebook
                    tab_names = [nb.tab(i, 'text') for i in range(len(nb.tabs()))]
                    self.assertEqual(
                        tab_names,
                        ['Аудіо', 'Шумозаглушення', 'Підсилення голосу', 'Гарячі клавіші', 'Інтерфейс', 'Пристрої'],
                    )

                    # Check Tab 1: Audio controls (strictly volume normalization only)
                    self.assertEqual(app.norm_volume_val.get(), '89,0 дБ')
                    self.assertEqual(app.norm_mode_var.get(), 'dynamic')
                    self.assertFalse(hasattr(app, "block_voice_var"))
                    self.assertFalse(hasattr(app, "voice_act_enabled_var"))
                    self.assertFalse(hasattr(app, "mic_latency_var"))
                    self.assertFalse(hasattr(app, "karaoke_latency_var"))
                    self.assertFalse(hasattr(app, "ducking_enabled_var"))
                    audio_lfs = [w for w in app.tab_audio.winfo_children() if isinstance(w, main.ttk.LabelFrame)]
                    self.assertEqual(len(audio_lfs), 1)
                    self.assertEqual(audio_lfs[0].cget("text"), "Нормалізація гучності")

                    # Check Tab 2: Hotkeys table
                    hk_items = app.special_hotkeys_tree.get_children()
                    self.assertEqual(len(hk_items), 8)
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

    def test_hide_to_tray_on_close_option_and_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / 'test_state_tray.json'
            with patch.object(main.MusicPadApp, '_config_path', return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 1. Default value is True
                    self.assertTrue(app.hide_to_tray.get())

                    # 2. Open settings and verify widget
                    app._open_settings()
                    app.update()
                    self.assertTrue(hasattr(app, "chk_hide_to_tray"))
                    self.assertEqual(app.chk_hide_to_tray.cget("text"), "Ховати в трей при закритті")
                    self.assertTrue(app.hide_to_tray_var.get())

                    # 3. Cancel settings reverts any unapplied toggle
                    app.hide_to_tray_var.set(False)
                    app._cancel_settings()
                    self.assertTrue(app.hide_to_tray.get())

                    # 4. Apply settings saves new value
                    app._open_settings()
                    app.hide_to_tray_var.set(False)
                    app._apply_settings()
                    self.assertFalse(app.hide_to_tray.get())
                    app._close_settings_dialog()

                    # 5. Check closing behavior when hide_to_tray is False -> calls _exit
                    with patch.object(app, "_exit") as mock_exit:
                        app._close()
                        self.assertTrue(mock_exit.called)

                    # 6. Re-enable hide_to_tray -> _close withdraws window instead of exit
                    app.hide_to_tray.set(True)
                    with patch.object(app, "_exit") as mock_exit, \
                         patch.object(app, "withdraw") as mock_withdraw:
                        app._close()
                        self.assertFalse(mock_exit.called)
                        self.assertTrue(mock_withdraw.called)

                    # 7. Localization check in English
                    app.lang_var.set("English")
                    app._apply_language()
                    if hasattr(app, "_open_settings"):
                        app._open_settings()
                        self.assertEqual(app.chk_hide_to_tray.cget("text"), "Hide to tray on close")
                        app._close_settings_dialog()
                finally:
                    app._exit()

    def test_interactive_special_hotkeys_assignment_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / 'test_state_hotkeys.json'
            with patch.object(main.MusicPadApp, '_config_path', return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app._open_settings()
                    app.update()

                    # 1. Verify 8 special hotkey items exist in tree
                    items = app.special_hotkeys_tree.get_children()
                    self.assertEqual(len(items), 8)
                    actions = [app.special_hotkeys_tree.item(it, "values")[0] for it in items]
                    self.assertEqual(
                        actions,
                        [
                            "Зупинити відтворення",
                            "Призупинити/продовжити відтворення",
                            "Скасувати",
                            "Копіювати",
                            "Вставити",
                            "Видалити вибрані записи",
                            "Пошук",
                            "Вихід",
                        ],
                    )

                    # 2. Select first item ("Зупинити відтворення") and clear it
                    app.special_hotkeys_tree.selection_set(items[0])
                    app._clear_special_hotkey()
                    self.assertEqual(app.special_hotkeys_dict["Зупинити відтворення"], "")
                    self.assertEqual(app.special_hotkeys_tree.item(items[0], "values")[1], "")

                    # 3. Simulate interactive keypress assignment (F9)
                    with patch("tkinter.Toplevel") as mock_top:
                        captured_callback = {}
                        fake_dlg = MagicMock()
                        fake_dlg.bind = lambda evt, cb: captured_callback.update({evt: cb})
                        mock_top.return_value = fake_dlg

                        app._set_special_hotkey()
                        self.assertIn("<KeyPress>", captured_callback)

                        ev_f9 = MagicMock()
                        ev_f9.keysym = "F9"
                        ev_f9.keycode = 0x78
                        ev_f9.state = 0
                        captured_callback["<KeyPress>"](ev_f9)

                    self.assertEqual(app.special_hotkeys_dict["Зупинити відтворення"], "F9")
                    self.assertEqual(app.special_hotkeys_tree.item(items[0], "values")[1], "F9")

                    # 4. Conflict detection: Assign "F9" to "Пошук" (should prompt and clear from previous)
                    app.special_hotkeys_tree.selection_set(items[6])
                    with patch("tkinter.Toplevel") as mock_top, \
                         patch("tkinter.messagebox.askyesno", return_value=True) as mock_ask:
                        captured_callback = {}
                        fake_dlg = MagicMock()
                        fake_dlg.bind = lambda evt, cb: captured_callback.update({evt: cb})
                        mock_top.return_value = fake_dlg

                        app._set_special_hotkey()
                        captured_callback["<KeyPress>"](ev_f9)

                        self.assertTrue(mock_ask.called)
                        self.assertEqual(app.special_hotkeys_dict["Пошук"], "F9")
                        self.assertEqual(app.special_hotkeys_dict["Зупинити відтворення"], "")

                    # 5. Verify config saving and loading ignores obsolete keys
                    app._save_state()
                    raw_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertNotIn("numpad_alt", raw_data.get("settings_custom", {}))
                    self.assertNotIn("auto_keys", raw_data.get("settings_custom", {}))
                    self.assertIn("special_hotkeys", raw_data.get("settings_custom", {}))

                    # Inject legacy deleted key and test reload ignores it
                    raw_data["settings_custom"]["special_hotkeys"]["Відтворити випадковий файл"] = "F12"
                    cfg_file.write_text(json.dumps(raw_data), encoding="utf-8")

                    app._load_saved_state()
                    self.assertNotIn("Відтворити випадковий файл", app.special_hotkeys_dict)
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

                    # 12. Test topmost synchronization with master window
                    app.always_on_top_var.set(False)
                    app._apply_settings()
                    bubble._sync_topmost()
                    self.assertFalse(bool(bubble.attributes("-topmost")))

                    app.always_on_top_var.set(True)
                    app._apply_settings()
                    self.assertTrue(bool(bubble.attributes("-topmost")))

                    app.always_on_top_var.set(False)
                    app._apply_settings()
                    self.assertFalse(bool(bubble.attributes("-topmost")))

                    # 13. Test automatic hiding on master unmap and focus out
                    app.notify_info("Тест приховування")
                    app.update()
                    self.assertTrue(nm.is_visible)

                    # Trigger master unmap
                    unmap_event = type("MockEvent", (), {"widget": app})()
                    bubble._on_master_unmap(unmap_event)
                    app.update()
                    self.assertFalse(nm.is_visible)

                    # Show bubble again and test focus out check
                    app.notify_info("Тест фокусу")
                    app.update()
                    self.assertTrue(nm.is_visible)

                    focus_event = type("MockEvent", (), {"widget": app})()
                    with patch.object(app, "focus_get", return_value=None):
                        bubble._on_master_focus_out(focus_event)
                        time.sleep(0.2)
                        app.update()
                        self.assertFalse(nm.is_visible)
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
                    self.assertEqual(app.yt_top.cget("bg"), c_dark["table_bg"])
                    self.assertEqual(app.btn_yt_search.cget("bg"), c_dark["top_bg"])

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
                    self.assertEqual(app.yt_top.cget("bg"), "#ffffff")
                    self.assertEqual(app.btn_yt_search.cget("bg"), "#eaedf1")

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
                    self.assertEqual(app.btn_yt_search.cget("bg"), "#212121")

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

                    # 2. Scrollable Canvases on all 6 tabs
                    self.assertEqual(len(app._settings_canvases), 6)
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

    def test_category_selection_bottom_info_panel_adaptation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_cat_panel.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()

                    # 1. Initial State ("my_sounds")
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.info_house_lbl.cget("image"), str(app._icons.get("house_lg", "")))
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")
                    self.assertEqual(app.stats_sounds_lbl.cget("text"), "0")
                    self.assertEqual(app.stats_selected_lbl.cget("text"), "0")
                    self.assertEqual(app.stats_plays_lbl.cget("text"), "0")

                    # 2. Switch to YouTube category via _on_category_select
                    app._on_category_select("youtube")
                    app.update()
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    self.assertEqual(app.info_house_lbl.cget("image"), str(app._icons.get("yt_icon", "")))
                    self.assertEqual(app.info_house_lbl.cget("fg"), "#ff1f1f")
                    self.assertEqual(app.stats_frame.winfo_manager(), "")
                    # btn_wrench remains intact on far right
                    self.assertTrue(app.btn_wrench.winfo_exists())

                    # 3. Idempotent call
                    app._on_category_select("youtube")
                    app.update()
                    self.assertEqual(app.stats_frame.winfo_manager(), "")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")

                    # 4. Add tracks, select one, and increment play count
                    t1 = LocalTrack(title="Track 1", path="track1.wav", duration=60.0)
                    t2 = LocalTrack(title="Track 2", path="track2.wav", duration=120.0)
                    app.local_tracks.extend([t1, t2])
                    app._filter_tracks()
                    app.playback_count = 7
                    app.track_list.selection_set("0")

                    # 5. Switch back to "my_sounds" via _on_category_select
                    app._on_category_select("my_sounds")
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.info_house_lbl.cget("image"), str(app._icons.get("house_lg", "")))
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")

                    # Check pack order: info_cat_name_lbl before stats_frame before btn_wrench
                    slaves = app.info_panel.pack_slaves()
                    self.assertIn(app.info_cat_name_lbl, slaves)
                    self.assertIn(app.stats_frame, slaves)
                    self.assertIn(app.btn_wrench, slaves)
                    idx_lbl = slaves.index(app.info_cat_name_lbl)
                    idx_stats = slaves.index(app.stats_frame)
                    idx_btn = slaves.index(app.btn_wrench)
                    self.assertLess(idx_lbl, idx_stats)
                    self.assertLess(idx_stats, idx_btn)

                    # Check that counters were restored with their actual current values
                    self.assertEqual(app.stats_sounds_lbl.cget("text"), "2")
                    self.assertEqual(app.stats_selected_lbl.cget("text"), "1")
                    self.assertEqual(app.stats_plays_lbl.cget("text"), "7")

                    # 6. Test theme switching adaptability
                    # In dark mode:
                    c_dark = app._get_theme_colors()
                    self.assertEqual(app.theme, "dark")
                    self.assertEqual(app.info_panel.cget("bg"), c_dark["info_bg"])
                    self.assertEqual(app.stats_frame.cget("bg"), c_dark["info_bg"])
                    self.assertEqual(app.info_cat_name_lbl.cget("fg"), c_dark["fg"])
                    self.assertEqual(app.info_house_lbl.cget("fg"), c_dark["accent"])

                    # Toggle to light mode
                    app._toggle_theme()
                    app.update()
                    self.assertEqual(app.theme, "light")
                    c_light = app._get_theme_colors()
                    self.assertEqual(app.info_panel.cget("bg"), c_light["info_bg"])
                    self.assertEqual(app.stats_frame.cget("bg"), c_light["info_bg"])
                    self.assertEqual(app.info_cat_name_lbl.cget("fg"), c_light["fg"])
                    self.assertEqual(app.info_house_lbl.cget("fg"), c_light["accent"])

                    # Switch to YouTube in light mode
                    app._on_category_select("youtube")
                    app.update()
                    self.assertEqual(app.info_panel.cget("bg"), c_light["info_bg"])
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    self.assertEqual(app.info_cat_name_lbl.cget("fg"), c_light["fg"])
                    self.assertEqual(app.info_house_lbl.cget("fg"), "#ff1f1f")
                    self.assertEqual(app.stats_frame.winfo_manager(), "")

                    # Toggle theme while on YouTube tab
                    app._toggle_theme()
                    app.update()
                    self.assertEqual(app.theme, "dark")
                    c_dark = app._get_theme_colors()
                    self.assertEqual(app.info_panel.cget("bg"), c_dark["info_bg"])
                    self.assertEqual(app.info_cat_name_lbl.cget("fg"), c_dark["fg"])
                    self.assertEqual(app.info_house_lbl.cget("fg"), "#ff1f1f")
                    self.assertEqual(app.stats_frame.winfo_manager(), "")

                    # Switch back to my_sounds in dark theme
                    app._on_category_select("my_sounds")
                    app.update()
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")
                    self.assertEqual(app.stats_frame.cget("bg"), c_dark["info_bg"])
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.info_house_lbl.cget("fg"), c_dark["accent"])

                    # 7. Fallback without PIL icons
                    orig_icons = app._icons
                    app._icons = {}
                    app._update_category_info_panel("youtube")
                    self.assertEqual(app.info_house_lbl.cget("text"), "▶")
                    self.assertEqual(app.info_house_lbl.cget("fg"), "#ff1f1f")
                    app._update_category_info_panel("my_sounds")
                    self.assertEqual(app.info_house_lbl.cget("text"), "🏠")
                    self.assertEqual(app.info_house_lbl.cget("fg"), c_dark["accent"])
                    app._icons = orig_icons

                    # 8. Test public aliases: on_category_select and on_category_change
                    self.assertTrue(callable(app.on_category_select))
                    self.assertTrue(callable(app.on_category_change))
                    self.assertTrue(callable(app.update_category_info_panel))
                    app.on_category_select("youtube")
                    app.update()
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    self.assertEqual(app.stats_frame.winfo_manager(), "")

                    app.on_category_change("my_sounds")
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")

                    # Verify background uniformity on stats_frame and all child widgets
                    c_current = app._get_theme_colors()
                    self.assertEqual(app.stats_frame.cget("bg"), c_current["info_bg"])
                    self.assertEqual(app.stats_r1.cget("bg"), c_current["info_bg"])
                    self.assertEqual(app.stats_r2.cget("bg"), c_current["info_bg"])
                    for lbl in app.stats_caption_labels:
                        self.assertEqual(lbl.cget("bg"), c_current["info_bg"])
                        self.assertEqual(lbl.cget("fg"), c_current["muted_fg"])
                    self.assertEqual(app.stats_sounds_lbl.cget("bg"), c_current["info_bg"])
                    self.assertEqual(app.stats_selected_lbl.cget("bg"), c_current["info_bg"])
                    self.assertEqual(app.stats_plays_lbl.cget("bg"), c_current["info_bg"])

                    # 9. Test event callback simulation (e.g. if bound directly to widget)
                    class MockEvent:
                        def __init__(self, widget):
                            self.widget = widget
                    mock_yt_event = MockEvent(app.cat_yt_frame)
                    app._on_category_select(mock_yt_event)
                    app.update()
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")

                    mock_my_event = MockEvent(app.cat_item_frame)
                    app._on_category_select(mock_my_event)
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")

                    # 10. Robustness with None / unexpected category argument
                    app._on_category_select(None)
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")
                finally:
                    app._exit()

    def test_category_selection_edge_cases_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "test_cat_edge.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()

                    # 1. Open YouTube search via _open_yt_search
                    app._open_yt_search()
                    app.update()
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    self.assertEqual(app.stats_frame.winfo_manager(), "")

                    # 2. Add track and update playback count while in YouTube mode
                    t = LocalTrack(title="Test Track", path="test.wav", duration=45.0)
                    app.local_tracks.append(t)
                    app.playback_count += 3
                    app._update_stats_ui()
                    app.update()

                    # stats_frame must remain hidden and not pop back up
                    self.assertEqual(app.stats_frame.winfo_manager(), "")

                    # 3. Hovering on category items should not change current category or info panel
                    app._on_cat_hover("my_sounds", entering=True)
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    app._on_cat_hover("my_sounds", entering=False)
                    self.assertEqual(app.current_category, "youtube")

                    # 4. Settings wrench button click while on YouTube mode
                    self.assertTrue(app.btn_wrench.winfo_exists())
                    app.btn_wrench.invoke()
                    app.update()
                    self.assertIsNotNone(app._settings_window)
                    self.assertTrue(app._settings_window.winfo_exists())
                    # Category and info panel remain on YouTube
                    self.assertEqual(app.current_category, "youtube")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "YouTube / YouTube Music")
                    self.assertEqual(app.stats_frame.winfo_manager(), "")
                    app._settings_window.destroy()
                    app._settings_window = None

                    # 5. Close search panel
                    app._hide_yt_search_panel()
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.info_house_lbl.cget("image"), str(app._icons.get("house_lg", "")))
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")
                    self.assertEqual(app.stats_sounds_lbl.cget("text"), "1")
                    self.assertEqual(app.stats_plays_lbl.cget("text"), "3")

                    # 6. Invalid / unrecognized category string defaults safely to current_category
                    app._on_category_select("nonexistent_category")
                    app.update()
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.info_cat_name_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.stats_frame.winfo_manager(), "pack")
                finally:
                    app._exit()

    def test_interface_tab_cleaned_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app._open_settings()
                    app.update()

                    # 1. Group 1: General (formerly "Мова")
                    self.assertEqual(app.grp_general.cget("text"), "Загальне")
                    self.assertEqual(app.lbl_lang.cget("text"), "Мова: ")
                    self.assertTrue(hasattr(app, "cb_lang"))
                    self.assertIn("English", app.cb_lang["values"])
                    # Ensure Date format combobox was removed
                    self.assertFalse(hasattr(app, "cb_date_fmt"))

                    # 2. Group 2: Toolbar (only Style)
                    self.assertEqual(app.grp_toolbar.cget("text"), "Панель інструментів")
                    self.assertEqual(app.lbl_style.cget("text"), "Стиль: ")
                    self.assertTrue(hasattr(app, "cb_tb_style"))
                    # Ensure scale, size, colors, refresh rate, mic level, linear volume were removed from interface tab
                    self.assertFalse(hasattr(app, "cb_tb_scale"))
                    self.assertFalse(hasattr(app, "cb_tb_size"))
                    self.assertFalse(hasattr(app, "cb_tb_color"))
                    self.assertFalse(hasattr(app, "scale_pos_rate"))
                    self.assertFalse(hasattr(app, "chk_show_mic_lvl"))
                    self.assertFalse(hasattr(app, "chk_linear_vol"))

                    # 3. Ensure Playback mode group is completely removed
                    self.assertFalse(hasattr(app, "grp_playback_mode"))

                    # 4. Group 3: Miscellaneous (remember playback state + hide to tray on close)
                    self.assertEqual(app.grp_misc.cget("text"), "Різне")
                    self.assertTrue(hasattr(app, "chk_remember_playback"))
                    self.assertEqual(
                        app.chk_remember_playback.cget("text"),
                        "Запам'ятовувати стан відтворення при виході з програми",
                    )
                    self.assertTrue(hasattr(app, "chk_hide_to_tray"))
                    self.assertEqual(
                        app.chk_hide_to_tray.cget("text"),
                        "Ховати в трей при закритті",
                    )
                    # Count children in grp_misc - should have the 2 checkbuttons
                    misc_children = [
                        c for c in app.grp_misc.winfo_children()
                        if isinstance(c, main.ttk.Checkbutton)
                    ]
                    self.assertEqual(len(misc_children), 2)
                finally:
                    app._exit()

    def test_english_localization_dynamic_switching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 1. Initial Ukrainian default
                    menu_btn_texts = [b.cget("text") for b in app._menu_buttons.values()]
                    self.assertIn("Файл", menu_btn_texts)
                    self.assertIn("Редагування", menu_btn_texts)
                    self.assertIn("Відтворення", menu_btn_texts)
                    self.assertIn("Вікно", menu_btn_texts)
                    self.assertEqual(app.track_list.heading("index")["text"], "Інд...")
                    self.assertEqual(app.track_list.heading("title")["text"], "Тег")
                    self.assertEqual(app.track_list.heading("duration")["text"], "Тр...")
                    self.assertEqual(app.track_list.heading("hotkey")["text"], "Сполучення ...")
                    self.assertEqual(app.cat_title_lbl.cget("text"), "Мої звуки")

                    # 2. Open settings and switch to English
                    app._open_settings()
                    app.update()
                    app.lang_var.set("English")
                    app._apply_settings()
                    app.update()

                    # Verify dynamic English localization
                    self.assertTrue(app._is_english())
                    menu_btn_texts_en = [b.cget("text") for b in app._menu_buttons.values()]
                    self.assertIn("File", menu_btn_texts_en)
                    self.assertIn("Edit", menu_btn_texts_en)
                    self.assertIn("Playback", menu_btn_texts_en)
                    self.assertIn("Window", menu_btn_texts_en)

                    # Main window elements
                    self.assertEqual(app.track_list.heading("index")["text"], "Ind...")
                    self.assertEqual(app.track_list.heading("title")["text"], "Title")
                    self.assertEqual(app.track_list.heading("duration")["text"], "Dur...")
                    self.assertEqual(app.track_list.heading("hotkey")["text"], "Hot key...")
                    self.assertEqual(app.cat_title_lbl.cget("text"), "My Sounds")
                    self.assertEqual(app.lbl_stats_sounds.cget("text"), "Sounds: ")
                    self.assertEqual(app.lbl_stats_plays.cget("text"), "Playback count: ")
                    self.assertEqual(app.lbl_stats_sel.cget("text"), "Selected: ")
                    self.assertEqual(app.status_var.get(), "Settings saved and applied.")

                    # Settings dialog elements
                    nb = app.settings_notebook
                    tab_names = [nb.tab(i, "text") for i in range(len(nb.tabs()))]
                    self.assertEqual(
                        tab_names,
                        ["Audio", "Noise Suppression", "Voice Boost", "Hotkeys", "Interface", "Devices"],
                    )
                    self.assertEqual(app.grp_general.cget("text"), "General")
                    self.assertEqual(app.lbl_lang.cget("text"), "Language: ")
                    self.assertEqual(app.grp_toolbar.cget("text"), "Toolbar")
                    self.assertEqual(app.lbl_style.cget("text"), "Style: ")
                    self.assertEqual(app.grp_misc.cget("text"), "Miscellaneous")
                    self.assertEqual(
                        app.chk_remember_playback.cget("text"),
                        "Remember playback state on exit",
                    )
                    self.assertEqual(app.btn_settings_reset.cget("text"), "Reset")
                    self.assertEqual(app.btn_settings_ok.cget("text"), "OK")
                    self.assertEqual(app.btn_settings_cancel.cget("text"), "Cancel")
                    self.assertEqual(app.btn_settings_apply.cget("text"), "Apply")

                    # 3. Switch back to Ukrainian
                    app.lang_var.set("Українська")
                    app._apply_settings()
                    app.update()
                    self.assertFalse(app._is_english())
                    menu_btn_texts_uk = [b.cget("text") for b in app._menu_buttons.values()]
                    self.assertIn("Файл", menu_btn_texts_uk)
                    self.assertIn("Редагування", menu_btn_texts_uk)
                    self.assertEqual(app.track_list.heading("title")["text"], "Тег")
                    self.assertEqual(app.cat_title_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.status_var.get(), "Налаштування збережено та застосовано.")
                finally:
                    app._exit()

    def test_interactive_track_seeking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app.current_duration = 200.0
                    app.seek_scale.to = 200.0
                    app.seek_scale.winfo_width = lambda: 400
                    app.engine = MagicMock()
                    mock_seek = app.engine.seek

                    # 1. Click / Press at x=200 (middle of 400px width = 50% = 100s)
                    press_ev = type("Event", (), {"x": 200, "widget": app.seek_scale})()
                    app.seek_scale._on_press(press_ev)
                    self.assertTrue(app._seeking_drag)
                    self.assertAlmostEqual(app.seek_var.get(), 100.0, delta=1.0)
                    self.assertEqual(app.time_display_var.get(), "1:40")
                    mock_seek.assert_called_with(app.seek_var.get())

                    # 2. Drag / Motion to x=300 (75% = 150s)
                    motion_ev = type("Event", (), {"x": 300, "widget": app.seek_scale})()
                    app.seek_scale._on_motion(motion_ev)
                    self.assertTrue(app._seeking_drag)
                    self.assertAlmostEqual(app.seek_var.get(), 150.0, delta=1.0)
                    self.assertEqual(app.time_display_var.get(), "2:30")

                    # 3. Release at x=300
                    release_ev = type("Event", (), {"x": 300, "widget": app.seek_scale})()
                    app.seek_scale._on_release(release_ev)
                    self.assertFalse(app._seeking_drag)
                    self.assertAlmostEqual(app.seek_var.get(), 150.0, delta=1.0)
                    mock_seek.assert_called_with(app.seek_var.get())
                finally:
                    app._exit()

    def test_playback_state_persistence_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            sample_track = main.LocalTrack(
                title="Song A",
                path=str(Path(tmp) / "song_a.wav"),
                duration=120.0,
            )
            Path(sample_track.path).write_bytes(b"dummy")

            # Part 1: remember_playback_state is True
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app1 = main.MusicPadApp()
                try:
                    app1.update()
                    app1.local_tracks = [sample_track]
                    app1._filter_tracks()
                    app1.current_index = 0
                    app1.current_duration = 120.0
                    app1.seek_var.set(45.5)
                    app1.volume_var.set(80.0)
                    app1.is_paused = True
                    app1.remember_playback_state_var.set(True)
                    app1._save_state()

                    # Inspect JSON written to disk
                    saved_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertIn("playback_state", saved_data)
                    ps = saved_data["playback_state"]
                    self.assertIsNotNone(ps)
                    self.assertEqual(ps["title"], "Song A")
                    self.assertEqual(ps["path"], sample_track.path)
                    self.assertAlmostEqual(ps["position"], 45.5)
                    self.assertEqual(ps["status"], "paused")
                finally:
                    app1._exit()

            # Now launch a new app and load state
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                with patch.object(main.MusicPadApp, "_is_mocked_env", return_value=False):
                    app2 = main.MusicPadApp()
                    try:
                        app2.update()
                        self.assertEqual(app2.current_index, 0)
                        self.assertAlmostEqual(app2.seek_var.get(), 45.5)
                        self.assertEqual(app2.time_display_var.get(), "0:45")
                        self.assertEqual(app2.now_playing_var.get(), "Зараз грає: Song A")
                        self.assertTrue(app2.is_paused)
                        self.assertEqual(app2.pause_button.cget("text"), "▶ Продовжити")

                        # Part 2: If unchecked, clean default state
                        app2.remember_playback_state_var.set(False)
                        app2._save_state()
                        saved_data2 = json.loads(cfg_file.read_text(encoding="utf-8"))
                        self.assertIsNone(saved_data2["playback_state"])
                    finally:
                        app2._exit()

            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                with patch.object(main.MusicPadApp, "_is_mocked_env", return_value=False):
                    app3 = main.MusicPadApp()
                    try:
                        app3.update()
                        self.assertEqual(app3.current_index, -1)
                        self.assertEqual(app3.seek_var.get(), 0.0)
                        self.assertEqual(app3.time_display_var.get(), "0:00")
                        self.assertEqual(app3.now_playing_var.get(), "Нічого не відтворюється")
                    finally:
                        app3._exit()

    def test_seek_resumes_playback_from_restored_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            sample_track = main.LocalTrack(
                title="Song B",
                path=str(Path(tmp) / "song_b.wav"),
                duration=120.0,
            )
            Path(sample_track.path).write_bytes(b"dummy")

            # Save state as paused at 30.0s
            saved_state = {
                "tracks": [{"title": sample_track.title, "path": sample_track.path, "duration": 120.0, "hotkey": "", "is_url": False}],
                "remember_playback_state": True,
                "settings_custom": {"remember_playback_state": True},
                "playback_state": {
                    "title": sample_track.title,
                    "path": sample_track.path,
                    "duration": 120.0,
                    "is_url": False,
                    "position": 30.0,
                    "volume": 75.0,
                    "status": "paused",
                },
            }
            cfg_file.write_text(json.dumps(saved_state), encoding="utf-8")

            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                with patch.object(main.MusicPadApp, "_is_mocked_env", return_value=False):
                    app = main.MusicPadApp()
                    try:
                        app.update()
                        self.assertTrue(app.is_paused)
                        self.assertFalse(app.is_playing)
                        self.assertEqual(app.current_index, 0)
                        self.assertAlmostEqual(app.seek_var.get(), 30.0)

                        # User clicks or drags to 60.0s and releases mouse
                        app.seek_var.set(60.0)
                        app._on_seek_release(None)

                        # Playback must now be active from 60.0s
                        self.assertFalse(app.is_paused)
                        self.assertTrue(app.is_playing)
                        self.assertIn("Пауза", app.pause_button.cget("text"))
                    finally:
                        app._exit()

    def test_timeline_slider_disabled_and_boundary_scrubbing(self) -> None:
        root = main.tk.Tk()
        try:
            var = main.tk.DoubleVar(value=10.0)
            slider = main.SoundpadTimelineSlider(root, variable=var, to=100.0)
            slider.winfo_width = lambda: 400
            callback_called = []
            slider.bind("<ButtonRelease-1>", lambda _: callback_called.append(True))

            # When disabled, clicks and motion should be ignored
            slider.configure(state="disabled")
            self.assertTrue(slider.is_disabled())
            ev = type("Event", (), {"x": 200, "widget": slider})()
            slider._on_press(ev)
            self.assertFalse(slider._drag)
            slider._on_motion(ev)
            slider._on_release(ev)
            self.assertEqual(var.get(), 10.0)
            self.assertEqual(len(callback_called), 0)

            # When normal, boundary scrubbing should clamp cleanly
            slider.configure(state="normal")
            self.assertFalse(slider.is_disabled())
            # Beyond right edge
            ev_over = type("Event", (), {"x": 9999, "widget": slider})()
            slider._on_press(ev_over)
            self.assertEqual(var.get(), 100.0)
            # Beyond left edge
            ev_under = type("Event", (), {"x": -500, "widget": slider})()
            slider._on_motion(ev_under)
            self.assertEqual(var.get(), 0.0)
            slider._on_release(ev_under)
            self.assertEqual(len(callback_called), 1)
        finally:
            root.destroy()

    def test_english_mode_theme_and_completion_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_en.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app.lang_var.set("English")
                    app._apply_settings()
                    app.update()

                    # 1. Theme toggle sets English tb_style_var ("Dark" / "Light")
                    app.theme = "dark"
                    app._toggle_theme()
                    self.assertEqual(app.tb_style_var.get(), "Light")
                    app._toggle_theme()
                    self.assertEqual(app.tb_style_var.get(), "Dark")

                    # 2. Track finished event uses English localization
                    app.is_playing = True
                    app._on_finished()
                    app.update()
                    self.assertEqual(app.pause_button.cget("text"), "▶ Play")
                    self.assertEqual(app.now_playing_var.get(), "Nothing is playing")

                    # 3. Track title notification uses English localization
                    app._on_track_title("Awesome Song")
                    app.update()
                    self.assertEqual(app.now_playing_var.get(), "Now playing: Awesome Song")

                    # 4. Settings dialog opened after switching language
                    app._open_settings()
                    app.update()
                    self.assertEqual(app._settings_window.title(), "Settings")
                    nb = app.settings_notebook
                    self.assertEqual(
                        [nb.tab(i, "text") for i in range(len(nb.tabs()))],
                        ["Audio", "Noise Suppression", "Voice Boost", "Hotkeys", "Interface", "Devices"],
                    )
                finally:
                    app._exit()

    def test_toolbar_controls_layout_and_play_pause_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_tb.json"
            dummy_wav = Path(tmp) / "test_track.wav"
            dummy_wav.write_bytes(bytes([82, 73, 70, 70]) + bytes(40))

            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                with patch.object(main.MusicPadApp, "_is_mocked_env", return_value=False):
                    app = main.MusicPadApp()
                    try:
                        app.update()
                        # 1. First 3 left buttons and their obsolete methods must be deleted
                        self.assertFalse(hasattr(app, "btn_tb_play"))
                        self.assertFalse(hasattr(app, "btn_tb_phones"))
                        self.assertFalse(hasattr(app, "btn_tb_mic"))
                        self.assertFalse(hasattr(app, "_play_headphones_only"))
                        self.assertFalse(hasattr(app, "_play_mic_only"))

                        # 2. Remaining elements left of timer shifted to the left in exact order
                        tb_children = app.top_bar.winfo_children()
                        self.assertIn(app.pause_button, tb_children)
                        self.assertIn(app.btn_tb_stop, tb_children)
                        self.assertIn(app.btn_tb_prev, tb_children)
                        self.assertIn(app.btn_tb_next, tb_children)
                        self.assertIn(app.top_bar_sep1, tb_children)
                        self.assertIn(app.time_label, tb_children)
                        self.assertIn(app.seek_scale, tb_children)

                        p_idx = tb_children.index(app.pause_button)
                        s_idx = tb_children.index(app.btn_tb_stop)
                        pr_idx = tb_children.index(app.btn_tb_prev)
                        n_idx = tb_children.index(app.btn_tb_next)
                        sep_idx = tb_children.index(app.top_bar_sep1)
                        t_idx = tb_children.index(app.time_label)
                        sk_idx = tb_children.index(app.seek_scale)
                        self.assertTrue(p_idx < s_idx < pr_idx < n_idx < sep_idx < t_idx < sk_idx)

                        # 3. Initial stopped state shows Play button
                        self.assertEqual(app.pause_button.cget("text"), "▶ Відтворити")
                        self.assertEqual(app.pause_button.cget("image"), str(app._icons["play"]))

                        # 4. Add a track and start playback via pause_button
                        track = main.LocalTrack(title="Test Song", path=str(dummy_wav), duration=10.0)
                        app.local_tracks = [track]
                        app._filter_tracks()

                        # Click play/pause toggle when stopped -> starts playback
                        app.pause_button.invoke()
                        app.update()
                        self.assertTrue(app.is_playing)
                        self.assertFalse(app.is_paused)
                        self.assertEqual(app.pause_button.cget("text"), "⏸ Пауза")
                        self.assertEqual(app.pause_button.cget("image"), str(app._icons["pause"]))

                        # Click play/pause toggle when playing -> pauses playback
                        app.pause_button.invoke()
                        app.update()
                        self.assertTrue(app.is_paused)
                        self.assertEqual(app.pause_button.cget("text"), "▶ Продовжити")
                        self.assertEqual(app.pause_button.cget("image"), str(app._icons["play"]))

                        # Click play/pause toggle when paused -> resumes playback
                        app.pause_button.invoke()
                        app.update()
                        self.assertFalse(app.is_paused)
                        self.assertEqual(app.pause_button.cget("text"), "⏸ Пауза")
                        self.assertEqual(app.pause_button.cget("image"), str(app._icons["pause"]))

                        # Click Stop -> stops playback, resets to Play
                        app.btn_tb_stop.invoke()
                        app.update()
                        self.assertFalse(app.is_playing)
                        self.assertFalse(app.is_paused)
                        self.assertEqual(app.pause_button.cget("text"), "▶ Відтворити")
                        self.assertEqual(app.pause_button.cget("image"), str(app._icons["play"]))
                    finally:
                        app._exit()

    def test_audio_tab_simplified_and_localization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_audio_tab.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    app._open_settings()
                    app.update()

                    # 1. Verify Audio Tab in Ukrainian (default)
                    nb = app.settings_notebook
                    self.assertEqual(nb.tab(0, "text"), "Аудіо")

                    audio_frames = [w for w in app.tab_audio.winfo_children() if isinstance(w, main.ttk.LabelFrame)]
                    self.assertEqual(len(audio_frames), 1)
                    self.assertEqual(audio_frames[0].cget("text"), "Нормалізація гучності")

                    # Verify ramp canvas and theming
                    self.assertTrue(hasattr(app, "c_ramp"))
                    c_dark = app._get_theme_colors()
                    self.assertEqual(str(app.c_ramp.cget("bg")), c_dark["table_bg"])

                    # Switch to Light Theme and verify c_ramp adapts
                    app._toggle_theme()
                    app.update()
                    c_light = app._get_theme_colors()
                    self.assertEqual(str(app.c_ramp.cget("bg")), c_light["table_bg"])

                    # 2. Switch language to English and verify Audio Tab re-renders in English
                    app.lang_var.set("English")
                    app._apply_settings()
                    app.update()

                    self.assertEqual(nb.tab(0, "text"), "Audio")
                    audio_frames_en = [w for w in app.tab_audio.winfo_children() if isinstance(w, main.ttk.LabelFrame)]
                    self.assertEqual(len(audio_frames_en), 1)
                    self.assertEqual(audio_frames_en[0].cget("text"), "Volume normalization")

                    # Attack multiple theme toggles in English mode
                    for _ in range(4):
                        app._toggle_theme()
                        app.update()
                        cur_c = app._get_theme_colors()
                        self.assertEqual(str(app.c_ramp.cget("bg")), cur_c["table_bg"])
                        self.assertEqual(str(app.c_ramp.cget("highlightbackground")), cur_c["divider"])

                    # 3. Verify reset restores default normalization settings cleanly
                    app.norm_volume_val.set("92,0 дБ")
                    app.norm_mode_var.set("fixed")
                    app._reset_settings_to_defaults()
                    self.assertEqual(app.norm_volume_val.get(), "89,0 дБ")
                    self.assertEqual(app.norm_mode_var.get(), "dynamic")
                finally:
                    app._exit()

    def test_soundpad_button_and_playback_edge_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_edge.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update()
                    # 1. Test SoundpadButton icon mapping with unicode symbols and lowercase strings
                    btn = app.pause_button
                    btn.configure(text="⏸")
                    self.assertEqual(btn.cget("image"), str(app._icons["pause"]))
                    btn.configure(text="▶")
                    self.assertEqual(btn.cget("image"), str(app._icons["play"]))
                    btn.configure(text="pause")
                    self.assertEqual(btn.cget("image"), str(app._icons["pause"]))
                    btn.configure(text="play")
                    self.assertEqual(btn.cget("image"), str(app._icons["play"]))

                    # Test bracket dictionary assignment btn["text"] = ...
                    btn["text"] = "⏸"
                    self.assertEqual(btn.cget("image"), str(app._icons["pause"]))
                    btn["text"] = "▶"
                    self.assertEqual(btn.cget("image"), str(app._icons["play"]))
                    btn["text"] = "⏸ Pause"
                    self.assertEqual(btn.cget("image"), str(app._icons["pause"]))
                    btn["text"] = "▶ Play"
                    self.assertEqual(btn.cget("image"), str(app._icons["play"]))

                    # 2. Test rapid Play/Pause clicks on empty playlist
                    self.assertEqual(len(app.local_tracks), 0)
                    for _ in range(5):
                        app._toggle_pause()
                        app.update()
                    self.assertFalse(app.is_playing)
                    self.assertFalse(app.is_paused)
                    self.assertEqual(btn.cget("image"), str(app._icons["play"]))

                    # 3. Test space key shortcut guard when entry is focused vs not focused
                    toggle_called = []
                    orig_toggle = app._toggle_pause
                    app._toggle_pause = lambda: toggle_called.append(True)
                    try:
                        # Entry focused (via search bar)
                        app._toggle_search_bar()
                        app.update()
                        self.assertTrue(app._is_entry_focused())
                        app.event_generate("<space>")
                        app.update()
                        self.assertEqual(len(toggle_called), 0)

                        # Close search bar and focus treeview (not entry)
                        app._close_search_bar()
                        app.track_list.focus_set()
                        app.update()
                        self.assertFalse(app._is_entry_focused())
                        app.event_generate("<space>")
                        app.update()
                        self.assertEqual(len(toggle_called), 1)
                    finally:
                        app._toggle_pause = orig_toggle

                    # 4. Test _selected_track when visible track is not in local_tracks
                    orphan_track = main.LocalTrack(title="Orphan", path="orphan.mp3", duration=5.0)
                    app.visible_tracks = [orphan_track]
                    app.track_list.insert("", "end", iid="0", values=("1", "Orphan", "0:05", ""))
                    app.track_list.selection_set("0")
                    app.update()
                    res = app._selected_track()
                    self.assertIsNotNone(res)
                    self.assertEqual(res[0], orphan_track)
                    self.assertEqual(res[1], 0)
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

    def test_ffmpeg_and_ffprobe_embedding_and_workflow(self) -> None:
        import sys
        from musicpad.audio_engine import AudioEngine
        import musicpad.library as lib

        # 1. Verify find_ffmpeg and find_ffprobe under frozen mode priority
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dummy_ffmpeg = tmp_path / "ffmpeg.exe"
            dummy_ffprobe = tmp_path / "ffprobe.exe"
            dummy_ffmpeg.write_bytes(b"dummy_ffmpeg")
            dummy_ffprobe.write_bytes(b"dummy_ffprobe")

            orig_frozen = getattr(sys, "frozen", False)
            orig_meipass = getattr(sys, "_MEIPASS", None)
            try:
                sys.frozen = True
                sys._MEIPASS = str(tmp_path)
                self.assertEqual(AudioEngine.find_ffmpeg(), str(dummy_ffmpeg))
                self.assertEqual(AudioEngine.find_ffprobe(), str(dummy_ffprobe))

                # Verify library metadata probe uses AudioEngine.find_ffprobe
                dummy_audio = tmp_path / "sample.mp3"
                dummy_audio.write_bytes(b"dummy")
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout='{"format": {"duration": "12.5", "tags": {"title": "Test Title"}}}')
                    title, dur = lib._probe_ffprobe_metadata(dummy_audio)
                    self.assertEqual(title, "Test Title")
                    self.assertAlmostEqual(dur, 12.5)
                    self.assertEqual(mock_run.call_args[0][0][0], str(dummy_ffprobe))
            finally:
                if orig_frozen:
                    sys.frozen = orig_frozen
                elif hasattr(sys, "frozen"):
                    delattr(sys, "frozen")
                if orig_meipass is not None:
                    sys._MEIPASS = orig_meipass
                elif hasattr(sys, "_MEIPASS"):
                    delattr(sys, "_MEIPASS")

        # 2. Verify MusicPad.spec contains both ffmpeg and ffprobe bundling
        spec_text = Path("MusicPad.spec").read_text(encoding="utf-8")
        self.assertIn("ffmpeg_path", spec_text)
        self.assertIn("ffprobe_path", spec_text)
        self.assertIn("ffmpeg_binaries.append((ffmpeg_path, '.'))", spec_text)
        self.assertIn("ffmpeg_binaries.append((ffprobe_path, '.'))", spec_text)

        # 3. Verify .github/workflows/release.yml has no FedericoCarboni dependency and uses choco + both binaries
        wf_text = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertNotIn("FedericoCarboni/setup-ffmpeg", wf_text)
        self.assertIn("choco install ffmpeg -y --no-progress", wf_text)
        self.assertIn("ffmpeg.exe", wf_text)
        self.assertIn("ffprobe.exe", wf_text)
        self.assertIn("Prepare FFmpeg and FFprobe for embedding", wf_text)
        self.assertIn("native/build.ps1", wf_text)


class TestInjectionRoutingAndUi(unittest.TestCase):
    def test_audio_engine_injection_mode_streams_and_shm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shm_path = Path(tmp) / "engine_injection.shm"
            wav_path = Path(tmp) / "tone.wav"
            _create_sine_wav(wav_path, duration_sec=0.18, freq=440.0, rate=48000)

            out_streams: list[MagicMock] = []
            in_streams: list[MagicMock] = []

            def fake_out_stream(**kwargs):
                m = MagicMock()
                m.kwargs = kwargs
                out_streams.append(m)
                return m

            def fake_in_stream(**kwargs):
                m = MagicMock()
                m.kwargs = kwargs
                m.read.return_value = (b"\x00\x00\x00\x00" * 384, False)
                in_streams.append(m)
                return m

            devs = [
                {"name": "fifine Microphone", "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 48000.0},
                {"name": "Headphones", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
                {"name": "CABLE Input", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
            ]
            fake_sd = MagicMock()
            fake_sd.RawOutputStream.side_effect = fake_out_stream
            fake_sd.RawInputStream.side_effect = fake_in_stream
            fake_sd.query_devices.side_effect = lambda idx=None: devs if idx is None else devs[int(idx)]

            done = threading.Event()
            errors: list[str] = []
            with patch.object(ae_mod, "sd", fake_sd):
                engine = AudioEngine(
                    output_device=2,
                    input_device=0,
                    monitor_device=1,
                    on_error=errors.append,
                    on_finished=done.set,
                    monitor_mic=False,
                    routing_mode=ROUTING_MODE_INJECTION,
                    target_endpoint_guid="{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
                    shm_path=shm_path,
                )
                try:
                    engine.set_volume(0.5)
                    engine.set_mic_volume(1.2)
                    engine.play(StreamTrack("Tone", str(wav_path)))
                    # Wait up to 1.0s for ffmpeg / WavPipeDecoder to write samples to InjectionShm
                    reader_shm = InjectionShm(path=shm_path)
                    reader_shm.open(acquire_lease=False)
                    try:
                        deadline = time.monotonic() + 1.0
                        while time.monotonic() < deadline and reader_shm.available_to_read() == 0:
                            time.sleep(0.02)
                        self.assertEqual(errors, [])
                        self.assertEqual(
                            reader_shm.get_target_endpoint_guid(),
                            "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
                        )
                        self.assertGreater(reader_shm.available_to_read(), 0)
                    finally:
                        reader_shm.close()

                    # Verify stop() flushes shared memory immediately
                    engine.stop()
                    reader_shm2 = InjectionShm(path=shm_path)
                    reader_shm2.open(acquire_lease=False)
                    try:
                        import struct
                        from musicpad.injection_shm import OFF_FLUSH_SEQ

                        assert reader_shm2._mmap is not None
                        flush_seq = struct.unpack_from("<I", reader_shm2._mmap, OFF_FLUSH_SEQ)[0]
                        self.assertGreaterEqual(flush_seq, 1)
                    finally:
                        reader_shm2.close()

                    # Verify that in injection mode with monitor_mic=False:
                    # - output_device (2, CABLE Input) was NEVER opened
                    # - input_device (0, fifine Microphone) was NEVER opened
                    # - monitor_device (1, Headphones) WAS opened
                    opened_out_devs = [m.kwargs.get("device") for m in out_streams]
                    self.assertNotIn(2, opened_out_devs)
                    self.assertIn(1, opened_out_devs)
                    self.assertEqual(len(in_streams), 0)
                finally:
                    engine.shutdown()

    def test_settings_devices_routing_mode_and_state_persistence(self) -> None:
        fake_devs = [
            {"index": 0, "name": "Мікрофон (3- fifine Microphone)", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 48000.0},
            {"index": 1, "name": "Навушники (Realtek Audio)", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0, "default_samplerate": 48000.0},
            {"index": 2, "name": "CABLE Input (VB-Audio Virtual Cable)", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0, "default_samplerate": 48000.0},
        ]
        fake_status = DriverStatus(
            state="active",
            installed_endpoint_guid="{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
            target_endpoint_guid="{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
            reason="Драйвер активний",
        )
        fake_ep = CaptureEndpoint(
            guid="{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
            endpoint_name="Мікрофон",
            adapter_name="3- fifine Microphone",
            enhancements_enabled=True,
            exclusive_allowed=False,
            efx_clsid=MUSICPAD_APO_CLSID,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "musicpad_state.json"
            with patch.object(AudioEngine, "list_devices", return_value=fake_devs), patch.object(
                AudioEngine, "find_cable_output", return_value=2
            ), patch.object(AudioEngine, "find_physical_mic", return_value=0), patch.object(
                AudioEngine, "find_default_monitor", return_value=1
            ), patch(
                "main.find_endpoint_for_sounddevice", return_value=fake_ep
            ), patch(
                "main.get_driver_status", return_value=fake_status
            ), patch.object(
                main.MusicPadApp, "_config_path", return_value=cfg_file
            ), patch.object(
                main.MusicPadApp, "_is_mocked_env", return_value=False
            ), patch.object(
                main.MusicPadApp, "_start_hotkey_listener"
            ):
                app = main.MusicPadApp()
                try:
                    app.update_idletasks()
                    # Default for fresh state without saved config is virtual_cable (Decision 20)
                    self.assertEqual(app.routing_mode_var.get(), ROUTING_MODE_VIRTUAL_CABLE)

                    app._open_settings()
                    app.update_idletasks()
                    self.assertTrue(hasattr(app, "cb_settings_cable"))

                    # Switch to injection mode -> cable combobox becomes disabled
                    app.routing_mode_var.set(ROUTING_MODE_INJECTION)
                    app._on_routing_mode_changed()
                    self.assertEqual(str(app.cb_settings_cable.cget("state")), "disabled")
                    self.assertEqual(app._ensure_engine().routing_mode, ROUTING_MODE_INJECTION)

                    # Test UAC helper install callback (mocked)
                    with patch("main.messagebox.askyesno", return_value=True), patch(
                        "main.run_driver_helper_uac", return_value=(True, "OK")
                    ) as mock_uac:
                        app._install_driver_uac_action()
                        mock_uac.assert_called_once_with(
                            "install", "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}"
                        )

                    app._save_state()
                    self.assertTrue(cfg_file.is_file())
                    saved_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertEqual(saved_data.get("routing_mode"), ROUTING_MODE_INJECTION)
                    self.assertEqual(
                        saved_data.get("target_mic_guid"),
                        "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}",
                    )
                finally:
                    app._exit()

        with patch("main.run_driver_cli", return_value=0) as mock_cli:
            rc = main._parse_and_run_driver_cli(
                ["main.py", "--driver", "install", "--endpoint-guid", "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}"]
            )
            self.assertEqual(rc, 0)
            mock_cli.assert_called_once_with("install", "{547a2ef2-a5b3-44d4-a876-1b6a52c704c8}")
            self.assertIsNone(main._parse_and_run_driver_cli(["main.py"]))

    def test_voice_dsp_pipeline_and_ui_controls(self) -> None:
        import numpy as np
        from musicpad.deepfilter_engine import DeepFilterEngine
        from musicpad.dsp_core import MPAD_FX_DEEPFILTER, MPAD_FX_WHITE_NOISE, MPAD_FX_VOICE_BOOST

        # 1. Test DeepFilterEngine
        df_eng = DeepFilterEngine()
        self.assertTrue(df_eng.is_available)
        raw_audio = np.random.randn(384, 2).astype(np.float32) * 0.1
        clean = df_eng.process_buffer(raw_audio, wet=1.0)
        self.assertEqual(clean.shape, raw_audio.shape)
        half_clean = df_eng.process_buffer(raw_audio, wet=0.5)
        self.assertEqual(half_clean.shape, raw_audio.shape)
        df_eng.close()

        # 2. Test AudioEngine FX control setters and flags
        engine = AudioEngine(routing_mode=ROUTING_MODE_VIRTUAL_CABLE)
        self.assertFalse(engine.deepfilter_enabled)
        self.assertFalse(engine.white_noise_removal)
        self.assertFalse(engine.voice_boost_enabled)
        self.assertEqual(engine.voice_boost_db, 0.0)

        engine.set_deepfilter_state(True, 0.85)
        self.assertTrue(engine.deepfilter_enabled)
        self.assertAlmostEqual(engine.deepfilter_wet, 0.85)

        engine.set_white_noise_removal(True)
        self.assertTrue(engine.white_noise_removal)

        engine.set_voice_boost(True, 12.0)
        self.assertTrue(engine.voice_boost_enabled)
        self.assertAlmostEqual(engine.voice_boost_db, 12.0)

        flags = engine._compute_voice_fx_flags()
        self.assertEqual(flags, MPAD_FX_DEEPFILTER | MPAD_FX_WHITE_NOISE | MPAD_FX_VOICE_BOOST)
        mult = engine._effective_mic_gain()
        self.assertAlmostEqual(mult, 10.0 ** (12.0 / 20.0), places=2)

        # 3. Test App Settings UI variables, defaults, and persistence
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_fx.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Engine must be initialized immediately at startup
                    self.assertTrue(hasattr(app, "engine") and app.engine is not None)

                    # Defaults must be OFF as requested
                    self.assertFalse(app.deepfilter_enabled_var.get())
                    self.assertFalse(app.white_noise_removal_var.get())
                    self.assertFalse(app.voice_boost_enabled_var.get())
                    self.assertEqual(app.voice_boost_db_var.get(), 0.0)

                    # Toggle on and change parameters via live _on_voice_fx_changed
                    app.deepfilter_enabled_var.set(True)
                    app.deepfilter_wet_var.set(75.0)
                    app.white_noise_removal_var.set(True)
                    app.voice_boost_enabled_var.set(True)
                    app.voice_boost_db_var.set(6.0)
                    app._on_voice_fx_changed()

                    self.assertTrue(app.engine.deepfilter_enabled)
                    self.assertAlmostEqual(app.engine.deepfilter_wet, 0.75)
                    self.assertTrue(app.engine.white_noise_removal)
                    self.assertTrue(app.engine.voice_boost_enabled)
                    self.assertAlmostEqual(app.engine.voice_boost_db, 6.0)

                    # Persist state
                    app._save_state()
                    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                    sc = saved.get("settings_custom", {})
                    self.assertTrue(sc.get("deepfilter_enabled"))
                    self.assertEqual(sc.get("deepfilter_wet"), 75.0)
                    self.assertTrue(sc.get("white_noise_removal"))
                    self.assertTrue(sc.get("voice_boost_enabled"))
                    self.assertEqual(sc.get("voice_boost_db"), 6.0)
                finally:
                    app._exit()

    def test_window_geometry_dynamic_size_and_centering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_geom.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. Screen height >= 1440 (1440p / 4K) -> 1200x900
                    with patch.object(app, "winfo_screenheight", return_value=1440):
                        self.assertEqual(app._compute_initial_window_size(), (1200, 900))
                    with patch.object(app, "winfo_screenheight", return_value=2160):
                        self.assertEqual(app._compute_initial_window_size(), (1200, 900))

                    # 2. 1080p (height >= 1080) -> 1080x780
                    with patch.object(app, "winfo_screenheight", return_value=1080):
                        self.assertEqual(app._compute_initial_window_size(), (1080, 780))
                    with patch.object(app, "winfo_screenheight", return_value=1200):
                        self.assertEqual(app._compute_initial_window_size(), (1080, 780))

                    # 3. Smaller screens (e.g. 1366x768) -> 960x680
                    with patch.object(app, "winfo_screenheight", return_value=768):
                        self.assertEqual(app._compute_initial_window_size(), (960, 680))

                    # 4. Centering calculation
                    with patch.object(app, "winfo_screenwidth", return_value=1920), patch.object(
                        app, "winfo_screenheight", return_value=1080
                    ), patch.object(app, "geometry") as mock_geom:
                        app._center_window(1080, 780)
                        mock_geom.assert_called_once_with("1080x780+420+150")
                finally:
                    app._exit()

    def test_window_geometry_persistence_and_boundary_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_geom_bounds.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Persistence
                    app._current_window_geom = {"width": 1080, "height": 780, "x": 120, "y": 80}
                    app._save_state()
                    self.assertTrue(cfg_file.is_file())
                    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertEqual(saved.get("window_geometry"), {"width": 1080, "height": 780, "x": 120, "y": 80})

                    # Valid restoration within bounds
                    with patch.object(app, "_get_screen_bounds", return_value=(0, 0, 1920, 1080)), patch.object(
                        app, "geometry"
                    ) as mock_geom:
                        app._restore_window_geometry({"width": 1080, "height": 780, "x": 100, "y": 100})
                        mock_geom.assert_called_once_with("1080x780+100+100")

                    # Boundary validation: secondary monitor was disconnected (out-of-bounds coords)
                    # Window must fall back to centered on current screen
                    with patch.object(app, "_get_screen_bounds", return_value=(0, 0, 1920, 1080)), patch.object(
                        app, "winfo_screenwidth", return_value=1920
                    ), patch.object(app, "winfo_screenheight", return_value=1080), patch.object(
                        app, "geometry"
                    ) as mock_geom_recentered:
                        # x=2500, y=1500 is completely outside visible bounds
                        app._restore_window_geometry({"width": 1080, "height": 780, "x": 2500, "y": 1500})
                        mock_geom_recentered.assert_called_once_with("1080x780+420+150")

                    # Minimum size clamping: w >= 740, h >= 520
                    with patch.object(app, "_get_screen_bounds", return_value=(0, 0, 1920, 1080)), patch.object(
                        app, "geometry"
                    ) as mock_geom_clamp:
                        app._restore_window_geometry({"width": 500, "height": 300, "x": 100, "y": 100})
                        mock_geom_clamp.assert_called_once_with("740x520+100+100")
                finally:
                    app._exit()

    def test_search_results_item_type_display_and_clean_local_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_item_types.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    mock_results = [
                        {
                            "title": "Blinding Lights",
                            "artist": "The Weeknd",
                            "duration": 200.0,
                            "source": "ytmusic",
                            "result_type": "song",
                            "item_type": "Пісня",
                        },
                        {
                            "title": "After Hours",
                            "artist": "The Weeknd",
                            "duration": 3600.0,
                            "source": "ytmusic",
                            "result_type": "album",
                            "item_type": "Альбом",
                            "duration_text": "14 тр. • 60 хв",
                        },
                        {
                            "title": "Save Your Tears (Live)",
                            "artist": "The Weeknd",
                            "duration": 240.0,
                            "source": "youtube",
                            "result_type": "video",
                            "item_type": "Відео",
                        },
                    ]
                    app._render_yt_results(mock_results)
                    self.assertEqual(len(app.yt_tree.get_children()), 3)

                    # Verify entity type displayed after title with dot separator in secondary color
                    row0_widgets = app.yt_tree._row_widgets["0"]
                    self.assertEqual(row0_widgets["title"].cget("text"), "Blinding Lights")
                    self.assertIsNotNone(row0_widgets.get("item_type"))
                    self.assertEqual(row0_widgets["item_type"].cget("text"), "  • Пісня")

                    row1_widgets = app.yt_tree._row_widgets["1"]
                    self.assertEqual(row1_widgets["title"].cget("text"), "After Hours")
                    self.assertIsNotNone(row1_widgets.get("item_type"))
                    self.assertEqual(row1_widgets["item_type"].cget("text"), "  • Альбом")
                    self.assertEqual(row1_widgets["duration"].cget("text"), "14 тр. • 60 хв")
                    self.assertEqual(row1_widgets["source"].cget("text"), "YTMusic")

                    row2_widgets = app.yt_tree._row_widgets["2"]
                    self.assertEqual(row2_widgets["title"].cget("text"), "Save Your Tears (Live)")
                    self.assertIsNotNone(row2_widgets.get("item_type"))
                    self.assertEqual(row2_widgets["item_type"].cget("text"), "  • Відео")

                    # Verify local tracks in "Мої звуки" keep clean title without redundant type suffix
                    app.local_tracks = [
                        LocalTrack(title="Clean Local Track", path="track.wav", duration=120.0, source="file")
                    ]
                    app._filter_tracks()
                    self.assertEqual(len(app.track_list.get_children()), 1)
                    loc_row = app.track_list._row_widgets["0"]
                    self.assertEqual(loc_row["title"].cget("text"), "Clean Local Track")
                    self.assertIsNone(loc_row.get("item_type"))
                finally:
                    app._exit()

    def test_album_search_results_and_metadata(self) -> None:
        mock_songs = [
            {"videoId": "s1", "title": "Song One", "artists": [{"name": "Daft Punk"}], "duration": "4:00", "duration_seconds": 240, "thumbnails": []}
        ]
        mock_albums = [
            {
                "browseId": "MPREb_123",
                "playlistId": "OLAK5uy_123",
                "title": "Discovery",
                "artists": [{"name": "Daft Punk"}],
                "trackCount": 14,
                "duration": "1 hour, 1 minute",
                "duration_seconds": 3660,
                "thumbnails": [{"url": "https://example.com/discovery.jpg"}],
            }
        ]
        mock_ytmusic_instance = MagicMock()
        mock_ytmusic_instance.search.side_effect = lambda query, filter=None: (
            mock_songs if filter == "songs" else (mock_albums if filter == "albums" else [])
        )
        with patch("musicpad.audio_engine.YTMusic", return_value=mock_ytmusic_instance):
            results = AudioEngine.search_youtube("Daft Punk", limit=10)
            titles = [r["title"] for r in results]
            self.assertIn("Song One", titles)
            self.assertIn("Discovery", titles)

            album_item = next(r for r in results if r["title"] == "Discovery")
            self.assertEqual(album_item["result_type"], "album")
            self.assertEqual(album_item["item_type"], "Альбом")
            self.assertEqual(album_item["artist"], "Daft Punk")
            self.assertEqual(album_item["source"], "ytmusic")
            self.assertEqual(album_item["duration_text"], "14 тр. • 1 год 1 хв")
            self.assertEqual(album_item["thumbnail_url"], "https://example.com/discovery.jpg")

    def test_album_view_navigation_and_instant_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_album_view.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    mock_search = [
                        {
                            "title": "Random Access Memories",
                            "artist": "Daft Punk",
                            "full_title": "Random Access Memories — Daft Punk",
                            "result_type": "album",
                            "item_type": "Альбом",
                            "browse_id": "MPREb_RAM",
                            "duration_text": "13 тр. • 74 хв",
                            "track_count": 13,
                            "source": "ytmusic",
                            "tracks": [
                                {"title": "Give Life Back to Music", "artist": "Daft Punk", "duration": 274.0, "videoId": "v1", "url": "https://music.youtube.com/watch?v=v1"},
                                {"title": "Get Lucky", "artist": "Daft Punk", "duration": 369.0, "videoId": "v2", "url": "https://music.youtube.com/watch?v=v2"},
                            ],
                        }
                    ]
                    app._render_yt_results(mock_search)
                    self.assertEqual(len(app.yt_tree.get_children()), 1)

                    # Double click on album row switches to album track list
                    app.yt_tree.selection_set("0")
                    album_mock_data = {
                        "title": "Random Access Memories",
                        "artist": "Daft Punk",
                        "track_count": 2,
                        "duration": 643.0,
                        "duration_text": "2 тр. • 10 хв",
                        "thumbnail_url": "https://example.com/ram.jpg",
                        "tracks": mock_search[0]["tracks"],
                    }
                    with patch.object(AudioEngine, "get_album", return_value=album_mock_data):
                        app._yt_play_picked(add_only=False, sync=True)

                    self.assertTrue(app._viewing_album)
                    self.assertEqual(
                        app.lbl_album_info.cget("text"),
                        "Альбом: Random Access Memories — Daft Punk (2 тр., 10 хв)",
                    )
                    self.assertEqual(len(app.yt_tree.get_children()), 2)
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("text"), "Give Life Back to Music")
                    self.assertEqual(app.yt_tree._row_widgets["1"]["title"].cget("text"), "Get Lucky")

                    # Back to search results without network fetch
                    app._back_to_search_results()
                    self.assertFalse(app._viewing_album)
                    self.assertEqual(len(app.yt_tree.get_children()), 1)
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("text"), "Random Access Memories")
                finally:
                    app._exit()

    def test_album_playback_queue_and_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_album_queue.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    album_tracks = [
                        {"title": "Track 1", "artist": "Artist", "duration": 180.0, "url": "https://music.youtube.com/watch?v=1", "video_id": "1"},
                        {"title": "Track 2", "artist": "Artist", "duration": 200.0, "url": "https://music.youtube.com/watch?v=2", "video_id": "2"},
                        {"title": "Track 3", "artist": "Artist", "duration": 220.0, "url": "https://music.youtube.com/watch?v=3", "video_id": "3"},
                    ]
                    album_item = {
                        "title": "Test Album",
                        "artist": "Artist",
                        "track_count": 3,
                        "duration_text": "3 тр. • 10 хв",
                        "tracks": album_tracks,
                    }
                    app._render_album_view(album_item, album_item)
                    self.assertEqual(len(app.yt_tree.get_children()), 3)

                    # Play Track 1 (index 0) from album view
                    app.yt_tree.selection_set("0")
                    with patch.object(app, "_play_url"):
                        app._yt_play_picked(add_only=False)

                    self.assertIsNotNone(app._active_album_queue)
                    self.assertEqual(len(app._active_album_queue), 3)
                    self.assertEqual(app._album_track_index, 0)

                    # Green indicator highlighting on active song
                    app.is_playing = True
                    app._refresh_album_view_playing_indicator()
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("text"), "▶  Track 1")
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("fg"), "#4ade80")
                    self.assertEqual(app.yt_tree._row_widgets["1"]["title"].cget("text"), "Track 2")
                    # When unselected, active track title remains green (#38d430)
                    app.yt_tree.selection_remove("0")
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("fg"), "#38d430")
                    app.yt_tree.selection_set("0")

                    # Autoplay next track on _on_finished
                    with patch.object(app, "_play_album_track") as mock_play_track:
                        app._on_finished()
                        app.update()
                        self.assertEqual(app._album_track_index, 1)
                        mock_play_track.assert_called_once_with(album_tracks[1])

                    # Toolbar "Next track" (>>) navigation within album
                    with patch.object(app, "_play_album_track") as mock_play_next:
                        app._next()
                        self.assertEqual(app._album_track_index, 2)
                        mock_play_next.assert_called_once_with(album_tracks[2])

                    # Toolbar "Previous track" (<<) navigation within album
                    with patch.object(app, "_play_album_track") as mock_play_prev:
                        app._previous()
                        self.assertEqual(app._album_track_index, 1)
                        mock_play_prev.assert_called_once_with(album_tracks[1])
                    # Verification of queue isolation: playing a non-album song resets album queue
                    app._viewing_album = False
                    non_album_item = {
                        "title": "Single Song",
                        "artist": "Solo Artist",
                        "url": "https://music.youtube.com/watch?v=solo",
                        "result_type": "song",
                    }
                    app.yt_results_data = [non_album_item]
                    app.yt_tree.selection_set("0")
                    with patch.object(app, "_play_url"):
                        app._yt_play_picked(add_only=False)
                    self.assertIsNone(app._active_album_queue)
                    self.assertIsNone(app._album_track_index)
                    self.assertIsNone(app._active_album_id)
                finally:
                    app._exit()

    def test_format_album_duration_edge_cases(self) -> None:
        # Colon-separated durations
        self.assertEqual(format_album_duration(12, 0, "48:12"), "12 тр. • 48 хв")
        self.assertEqual(format_album_duration(14, None, "1:01:00"), "14 тр. • 1 год 1 хв")
        # Hours without minutes
        self.assertEqual(format_album_duration(10, 7200, ""), "10 тр. • 2 год")
        # Text hours and minutes
        self.assertEqual(format_album_duration(15, None, "1 hour, 15 minutes"), "15 тр. • 1 год 15 хв")
        # Non-positive or None track count
        self.assertEqual(format_album_duration(0, 1800, ""), "30 хв")
        self.assertEqual(format_album_duration(None, 1800, ""), "30 хв")
        self.assertEqual(format_album_duration(None, None, ""), "")

    def test_window_geometry_negative_coordinates_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_geom_neg.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Tkinter string format with negative coordinates: +-1920
                    with patch.object(app, "_get_screen_bounds", return_value=(-1920, 0, 1920, 1080)), patch.object(
                        app, "geometry"
                    ) as mock_geom:
                        app._restore_window_geometry("1080x780+-1920+0")
                        mock_geom.assert_called_once_with("1080x780+-1920+0")
                finally:
                    app._exit()

    def test_album_search_real_world_snippet_resolves_metadata(self) -> None:
        # Real ytmusic search returns snippet with duration=None and no trackCount
        real_snippet = {
            "browseId": "MPREb_Real",
            "playlistId": "OLAK5uy_Real",
            "title": "Real World Album",
            "artists": [{"name": "Great Artist"}],
            "duration": None,
            "thumbnails": [{"url": "https://example.com/art.jpg"}],
        }
        resolved_album = {
            "title": "Real World Album",
            "artist": "Great Artist",
            "trackCount": 10,
            "duration": "45 minutes",
            "duration_seconds": 2700,
            "tracks": [],
        }
        mock_yt = MagicMock()
        mock_yt.search.side_effect = lambda q, filter=None: [real_snippet] if filter == "albums" else []
        mock_yt.get_album.return_value = resolved_album
        with patch("musicpad.audio_engine.YTMusic", return_value=mock_yt):
            results = AudioEngine.search_youtube("Real World Album", limit=5)
            self.assertEqual(len(results), 1)
            alb = results[0]
            self.assertEqual(alb["title"], "Real World Album")
            self.assertEqual(alb["duration_text"], "10 тр. • 45 хв")
            self.assertEqual(alb["track_count"], 10)

    def test_get_album_with_playlist_id_resolution(self) -> None:
        mock_yt = MagicMock()
        mock_yt.get_album_browse_id.return_value = "MPREb_ResolvedFromPlaylist"
        mock_yt.get_album.return_value = {
            "title": "Playlist Album",
            "artists": [{"name": "Artist"}],
            "trackCount": 5,
            "duration_seconds": 1200,
            "tracks": [
                {"videoId": "t1", "title": "Track 1", "duration_seconds": 240}
            ],
        }
        with patch("musicpad.audio_engine.YTMusic", return_value=mock_yt):
            # Pass OLAK... playlist ID
            alb = AudioEngine.get_album("OLAK5uy_PlaylistIdTest")
            mock_yt.get_album_browse_id.assert_called_once_with("OLAK5uy_PlaylistIdTest")
            self.assertEqual(alb["browse_id"], "MPREb_ResolvedFromPlaylist")
            self.assertEqual(alb["title"], "Playlist Album")
            self.assertEqual(len(alb["tracks"]), 1)

    def test_viewing_different_album_does_not_show_false_playing_indicator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_diff_album.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Album 1 is playing
                    app._active_album_queue = [
                        {"title": "Song A1", "artist": "Artist A", "duration": 180.0},
                    ]
                    app._album_track_index = 0
                    app._active_album_id = "MPREb_Album_A"
                    app.is_playing = True

                    # User opens Album 2
                    album_2 = {
                        "browse_id": "MPREb_Album_B",
                        "title": "Album B",
                        "artist": "Artist B",
                        "track_count": 2,
                        "duration_text": "2 тр. • 6 хв",
                        "tracks": [
                            {"title": "Song B1", "artist": "Artist B", "duration": 180.0},
                            {"title": "Song B2", "artist": "Artist B", "duration": 180.0},
                        ],
                    }
                    app._render_album_view(album_2, album_2)

                    # In Album 2, index 0 must NOT have the green playing prefix
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("text"), "Song B1")
                    self.assertEqual(app.yt_tree._row_widgets["1"]["title"].cget("text"), "Song B2")

                    # Refresh indicator also keeps it clean
                    app._refresh_album_view_playing_indicator()
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("text"), "Song B1")

                    # If user returns to Album 1, index 0 shows ▶
                    album_1 = {
                        "browse_id": "MPREb_Album_A",
                        "title": "Album A",
                        "artist": "Artist A",
                        "track_count": 1,
                        "duration_text": "1 тр. • 3 хв",
                        "tracks": app._active_album_queue,
                    }
                    app._render_album_view(album_1, album_1)
                    self.assertEqual(app.yt_tree._row_widgets["0"]["title"].cget("text"), "▶  Song A1")
                finally:
                    app._exit()

    def test_empty_album_view_shows_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_empty_album.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    empty_album = {
                        "browse_id": "MPREb_Empty",
                        "title": "Empty Album",
                        "artist": "Empty Artist",
                        "track_count": 0,
                        "tracks": [],
                    }
                    app._render_album_view(empty_album, empty_album)
                    self.assertEqual(app.yt_status_lbl.winfo_manager(), "pack")
                    self.assertIn("не знайдено", app.yt_status_lbl.cget("text"))
                finally:
                    app._exit()

    def test_window_geometry_disconnected_monitor_fallback_to_center(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_geom_disco.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Visible virtual display: 0, 0 to 1920, 1080 (secondary monitor at 2560 was disconnected)
                    with patch.object(app, "_get_screen_bounds", return_value=(0, 0, 1920, 1080)), patch.object(
                        app, "_center_window"
                    ) as mock_center:
                        app._restore_window_geometry({"width": 1080, "height": 780, "x": 2600, "y": 100})
                        mock_center.assert_called_once_with(1080, 780)
                finally:
                    app._exit()

    def test_album_view_instant_back_restores_cached_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_back_status.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    cached_results = [
                        {"title": "Song 1", "artist": "Artist 1", "duration": 180.0, "source": "ytmusic", "item_type": "Пісня"},
                        {"title": "Album 1", "artist": "Artist 2", "duration": 3600.0, "source": "ytmusic", "item_type": "Альбом", "browse_id": "b1"},
                    ]
                    app.yt_results_data = list(cached_results)
                    custom_status = "Знайдено результатів: 2 для 'Daft Punk'"
                    app.yt_status_lbl.configure(text=custom_status)

                    # Enter album view
                    album_item = dict(cached_results[1])
                    app._open_album_view(album_item, sync=True)
                    self.assertTrue(app._viewing_album)

                    # Return back to search results
                    app._back_to_search_results()
                    self.assertFalse(app._viewing_album)
                    self.assertEqual(app.yt_status_lbl.cget("text"), custom_status)
                    self.assertEqual(len(app.yt_tree.get_children()), 2)
                finally:
                    app._exit()

    def test_album_queue_skips_unplayable_empty_url_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_skip_empty.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app._active_album_queue = [
                        {"title": "Track 1 (Unplayable)", "artist": "Artist", "url": "", "video_id": ""},
                        {"title": "Track 2 (Playable)", "artist": "Artist", "url": "https://music.youtube.com/watch?v=2", "video_id": "2"},
                    ]
                    app._album_track_index = 0
                    with patch.object(app, "_next") as mock_next:
                        app._play_album_track(app._active_album_queue[0])
                        # Should show status about unplayable track
                        self.assertIn("недоступний", app.yt_status_lbl.cget("text"))
                finally:
                    app._exit()

    def test_audio_engine_get_album_non_numeric_track_count(self) -> None:
        mock_yt = MagicMock()
        mock_yt.get_album.return_value = {
            "title": "Text TrackCount Album",
            "artists": [{"name": "Band"}],
            "trackCount": "12 пісень",
            "duration": "45 minutes",
            "tracks": [{"videoId": "v1", "title": "Track 1", "duration": "3:45"}],
        }
        with patch("musicpad.audio_engine.YTMusic", return_value=mock_yt):
            alb = AudioEngine.get_album("MPREb_TextTC")
            self.assertEqual(alb["track_count"], 12)
            self.assertEqual(alb["title"], "Text TrackCount Album")
            self.assertEqual(len(alb["tracks"]), 1)

    def test_audio_engine_cache_size_and_purge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            yt_cache = tmp_root / ".cache" / "youtube"
            thumb_cache = tmp_root / ".cache" / "thumbnails"
            yt_cache.mkdir(parents=True, exist_ok=True)
            thumb_cache.mkdir(parents=True, exist_ok=True)

            f1 = yt_cache / "audio1.webm"
            f1.write_bytes(b"x" * 1024)
            f2 = thumb_cache / "thumb1.png"
            f2.write_bytes(b"y" * 2048)

            with patch("musicpad.audio_engine._app_root_dir", return_value=tmp_root):
                size = AudioEngine.get_cache_size_bytes()
                self.assertEqual(size, 3072)

                ae_mod._YT_STREAM_CACHE["test"] = ("url", 1234567890.0)
                ae_mod._ALBUM_CACHE["test_alb"] = {"title": "Alb"}

                count, freed = AudioEngine.purge_cache()
                self.assertEqual(count, 2)
                self.assertEqual(freed, 3072)
                self.assertEqual(AudioEngine.get_cache_size_bytes(), 0)
                self.assertEqual(len(ae_mod._YT_STREAM_CACHE), 0)
                self.assertEqual(len(ae_mod._ALBUM_CACHE), 0)
                self.assertFalse(f1.exists())
                self.assertFalse(f2.exists())

    def test_audio_engine_search_filter_modes(self) -> None:
        mock_yt = MagicMock()
        mock_yt.search.side_effect = lambda q, filter=None, limit=25: [
            {"videoId": "s1", "title": "Song 1", "artists": [{"name": "Art"}], "duration_seconds": 180, "thumbnails": []}
        ] if filter == "songs" else [
            {"browseId": "b1", "title": "Album 1", "artists": [{"name": "Art"}], "thumbnails": []}
        ] if filter == "albums" else []

        with patch("musicpad.audio_engine.YTMusic", return_value=mock_yt), \
             patch("musicpad.audio_engine.YoutubeDL", None):
            # 1. filter_mode = "no_albums" -> only songs, no albums
            res_no_albums = AudioEngine.search_youtube("test query", limit=20, filter_mode="no_albums")
            self.assertTrue(len(res_no_albums) > 0)
            self.assertTrue(all(item.get("item_type") != "Альбом" for item in res_no_albums))

            # 2. filter_mode = "albums_only" -> only albums
            res_albums_only = AudioEngine.search_youtube("test query", limit=20, filter_mode="albums_only")
            self.assertTrue(len(res_albums_only) > 0)
            self.assertTrue(all(item.get("item_type") == "Альбом" for item in res_albums_only))

            # 3. filter_mode = "all" -> contains both songs and albums
            res_all = AudioEngine.search_youtube("test query", limit=20, filter_mode="all")
            has_album = any(item.get("item_type") == "Альбом" for item in res_all)
            has_song = any(item.get("item_type") != "Альбом" for item in res_all)
            self.assertTrue(has_album and has_song)

    def test_clear_cache_ui_visibility_and_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_cache_test.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. Clear cache button exists and text is correct
                    self.assertTrue(hasattr(app, "btn_clear_cache"))
                    self.assertEqual(app.btn_clear_cache.cget("text"), "🗑 Очистити кеш")

                    # In "my_sounds" category, it is packed
                    self.assertEqual(app.current_category, "my_sounds")
                    self.assertEqual(app.btn_clear_cache.winfo_manager(), "pack")

                    # Switch to "youtube" category -> button hidden
                    app._select_category("youtube")
                    self.assertEqual(app.btn_clear_cache.winfo_manager(), "")

                    # Switch back to "my_sounds" -> button re-packed
                    app._select_category("my_sounds")
                    self.assertEqual(app.btn_clear_cache.winfo_manager(), "pack")

                    # 2. Test click with askyesno=False
                    with patch("main.messagebox.askyesno", return_value=False), \
                         patch("musicpad.audio_engine.AudioEngine.purge_cache") as mock_purge:
                        app._on_clear_cache_clicked()
                        mock_purge.assert_not_called()

                    # 3. Test click with askyesno=True
                    with patch("main.messagebox.askyesno", return_value=True), \
                         patch("musicpad.audio_engine.AudioEngine.get_cache_size_bytes", return_value=10485760), \
                         patch("musicpad.audio_engine.AudioEngine.purge_cache", return_value=(5, 10485760)):
                        app._yt_thumb_cache["cached_img"] = "dummy"
                        app._on_clear_cache_clicked()
                        self.assertEqual(len(app._yt_thumb_cache), 0)
                        self.assertIn("10.0 МБ", app.status_var.get())
                finally:
                    app._exit()

    def test_search_filter_ui_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_filter_test.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Check filter widgets
                    self.assertTrue(hasattr(app, "btn_yt_filter"))
                    self.assertTrue(hasattr(app, "yt_filter_var"))
                    self.assertEqual(app.yt_filter_var.get(), "all")
                    self.assertIn("Всі результати", app.btn_yt_filter.cget("text"))

                    # Set filter to no_albums
                    app._set_yt_filter("no_albums")
                    self.assertEqual(app.yt_filter_var.get(), "no_albums")
                    self.assertIn("Без альбомів", app.btn_yt_filter.cget("text"))

                    # Verify saved state has yt_search_filter
                    data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertEqual(data.get("yt_search_filter"), "no_albums")

                    # Set filter to albums_only
                    app._set_yt_filter("albums_only")
                    self.assertEqual(app.yt_filter_var.get(), "albums_only")
                    self.assertIn("Тільки альбоми", app.btn_yt_filter.cget("text"))
                finally:
                    app._exit()

                # Reopen app with that config file and verify filter restored
                app2 = main.MusicPadApp()
                try:
                    self.assertEqual(app2.yt_filter_var.get(), "albums_only")
                    self.assertIn("Тільки альбоми", app2.btn_yt_filter.cget("text"))
                finally:
                    app2._exit()

    def test_album_view_tracks_inherit_thumbnails_and_cached_photo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_alb_thumb.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    album_item = {
                        "title": "Test Album",
                        "artist": "Test Artist",
                        "browse_id": "MPREb_Test",
                        "thumbnail_url": "https://example.com/cover.jpg",
                        "item_type": "Альбом",
                    }
                    album_data = {
                        "title": "Test Album",
                        "artist": "Test Artist",
                        "thumbnail_url": "https://example.com/cover.jpg",
                        "track_count": 2,
                        "tracks": [
                            {"title": "Track 1", "artist": "Test Artist", "duration": 180.0, "url": "https://yt.com/1"},
                            {"title": "Track 2", "artist": "Test Artist", "duration": 200.0, "url": "https://yt.com/2"},
                        ],
                    }
                    # Pre-cache a dummy photo for the album
                    dummy_photo = main.tk.PhotoImage(width=40, height=40)
                    app._yt_thumb_cache[album_item["thumbnail_url"]] = dummy_photo

                    app._render_album_view(album_data, album_item)

                    # Both tracks must have inherited the album thumbnail_url and dummy photo
                    self.assertEqual(len(app.yt_tree.get_children()), 2)
                    for iid in ("0", "1"):
                        item_data = app.yt_tree._items[iid]
                        self.assertEqual(item_data["thumb_url"], "https://example.com/cover.jpg")
                        self.assertEqual(item_data["cover_image"], dummy_photo)
                finally:
                    app._exit()

    def test_album_view_scroll_anchoring_single_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_scroll.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. Populate search results with 25 items so canvas is tall
                    items = [
                        {"title": f"Song {i}", "artist": "Art", "duration": 180.0, "source": "ytmusic", "item_type": "Пісня"}
                        for i in range(25)
                    ]
                    app._render_yt_results(items)
                    app.yt_tree.update_idletasks()

                    # Scroll down to bottom
                    app.yt_tree.canvas.yview_moveto(0.85)

                    # 2. Render a 1-track album (the exact scenario from user's Photo 1 & Photo 2)
                    one_track_album = {
                        "title": "Не смотри наверх",
                        "artist": "Три дня дождя",
                        "track_count": 1,
                        "tracks": [
                            {"title": "Не смотри наверх", "artist": "Три дня дождя", "duration": 219.0, "url": "https://yt.com/song"}
                        ],
                    }
                    orig_item = dict(one_track_album)
                    orig_item["browse_id"] = "b_single"
                    app._render_album_view(one_track_album, orig_item)
                    app.yt_tree.update_idletasks()

                    # 3. Canvas yview MUST be pinned to 0.0 (top)
                    y_top, _ = app.yt_tree.canvas.yview()
                    self.assertAlmostEqual(y_top, 0.0, places=2)
                    # And item "0" must exist and be selected
                    self.assertEqual(app.yt_tree.selection(), ("0",))
                finally:
                    app._exit()

    def test_back_to_search_results_restores_album_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_back_scroll.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Search results with 20 items
                    items = [
                        {"title": f"Song {i}", "artist": "Art", "duration": 180.0, "source": "ytmusic", "item_type": "Пісня"}
                        for i in range(19)
                    ]
                    items.append({
                        "title": "Target Album",
                        "artist": "Art",
                        "duration": 3600.0,
                        "source": "ytmusic",
                        "item_type": "Альбом",
                        "browse_id": "b_target",
                    })
                    app._render_yt_results(items)

                    # User selects the album at index 19 (at the bottom)
                    app.yt_tree.selection_set("19")
                    app.yt_tree.focus("19")

                    # Open album view
                    app._open_album_view(items[19], sync=True)
                    self.assertTrue(app._viewing_album)
                    self.assertEqual(app._cached_selected_album_iid, "19")

                    # Back to search results
                    with patch.object(app.yt_tree, "see") as mock_see:
                        app._back_to_search_results()
                        self.assertFalse(app._viewing_album)
                        # Selection is restored to "19"
                        self.assertEqual(app.yt_tree.selection(), ("19",))
                        mock_see.assert_called_with("19")
                finally:
                    app._exit()


    def test_redesigned_top_toolbar_elements_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_tb_layout.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. Height is 60px
                    self.assertEqual(int(app.top_bar.cget("height")), 60)

                    # 2. Controls exist in toolbar
                    self.assertTrue(hasattr(app, "btn_tb_mic_mute"))
                    self.assertTrue(hasattr(app, "btn_tb_loop"))
                    self.assertTrue(hasattr(app, "lbl_np_title"))
                    self.assertTrue(hasattr(app, "lbl_np_artist"))
                    self.assertTrue(hasattr(app, "lbl_np_cover"))

                    # 3. Loop button alias
                    self.assertIs(app.btn_b_loop, app.btn_tb_loop)

                    # 4. Elements ordering in top_bar
                    tb_children = app.top_bar.winfo_children()
                    p_idx = tb_children.index(app.pause_button)
                    s_idx = tb_children.index(app.btn_tb_stop)
                    pr_idx = tb_children.index(app.btn_tb_prev)
                    n_idx = tb_children.index(app.btn_tb_next)
                    sep_idx = tb_children.index(app.top_bar_sep1)
                    t_idx = tb_children.index(app.time_label)
                    sk_idx = tb_children.index(app.seek_scale)
                    self.assertTrue(p_idx < s_idx < pr_idx < n_idx < sep_idx < t_idx < sk_idx)
                finally:
                    app._exit()

    def test_mute_track_to_others_broadcast_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_mute_others.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    self.assertFalse(app.mute_track_to_others_var.get())
                    # Toggle on
                    app._toggle_mute_track_to_others()
                    self.assertTrue(app.mute_track_to_others_var.get())
                    self.assertTrue(app.engine.get_mute_broadcast())
                    self.assertFalse(app.engine._route_mic)
                    self.assertTrue(app.engine._route_speakers)
                finally:
                    # app._exit() saves state with mute_track_to_others=True
                    app._exit()

                with open(cfg_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertTrue(data.get("mute_track_to_others"))

                # Verify persistence on re-load
                app2 = main.MusicPadApp()
                try:
                    self.assertTrue(app2.mute_track_to_others_var.get())
                    self.assertTrue(app2.engine.get_mute_broadcast())

                    # Toggle off in app2
                    app2._toggle_mute_track_to_others()
                    self.assertFalse(app2.mute_track_to_others_var.get())
                    self.assertFalse(app2.engine.get_mute_broadcast())
                finally:
                    app2._exit()

                # Verify toggle off persisted
                app3 = main.MusicPadApp()
                try:
                    self.assertFalse(app3.mute_track_to_others_var.get())
                    self.assertFalse(app3.engine.get_mute_broadcast())
                finally:
                    app3._exit()

    def test_update_now_playing_display_title_artist_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_np_disp.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Initially nothing playing
                    self.assertIn("Нічого", app.lbl_np_title.cget("text"))
                    self.assertEqual(app.lbl_np_artist.cget("text"), "")

                    # Update with title, artist and dummy photo
                    dummy_cov = main.tk.PhotoImage(width=40, height=40)
                    app._update_now_playing_display("Bohemian Rhapsody", "Queen", dummy_cov)
                    self.assertEqual(app.lbl_np_title.cget("text"), "Bohemian Rhapsody")
                    self.assertEqual(app.lbl_np_artist.cget("text"), "  •  Queen")
                    self.assertEqual(app.lbl_np_cover.cget("image"), str(dummy_cov))

                    # Reset / clear
                    app._update_now_playing_display("", "")
                    self.assertIn("Нічого", app.lbl_np_title.cget("text"))
                    self.assertEqual(app.lbl_np_artist.cget("text"), "")
                finally:
                    app._exit()

    def test_speech_bubble_suppressed_for_playing_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_bubble_suppress.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Mock bubble.show
                    bubble = app.notification_manager.bubble
                    with patch.object(bubble, "show") as mock_show:
                        # 1. Playing notification via notify_info
                        app.notify_info("Відтворюється: Song A")
                        mock_show.assert_not_called()

                        # 2. Playing notification via status_var
                        app.status_var.set("Відтворюється: Song B")
                        mock_show.assert_not_called()

                        app.status_var.set("Зараз грає: Song C")
                        mock_show.assert_not_called()

                        # 3. Non-playback notifications should still show
                        app.status_var.set("Дію скасовано.")
                        mock_show.assert_called_once_with("Дію скасовано.", "info")
                finally:
                    app._exit()


    def test_btn_wrench_placed_to_the_right_of_clear_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_btn_order.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    app.update_idletasks()
                    # Initial state in "my_sounds": wrench is to the right of clear cache
                    self.assertEqual(app.btn_clear_cache.winfo_manager(), "pack")
                    self.assertEqual(app.btn_wrench.winfo_manager(), "pack")
                    self.assertGreater(app.btn_wrench.winfo_x(), app.btn_clear_cache.winfo_x())

                    # Switch to youtube and back
                    app._select_category("youtube")
                    app.update_idletasks()
                    self.assertEqual(app.btn_clear_cache.winfo_manager(), "")

                    app._select_category("my_sounds")
                    app.update_idletasks()
                    self.assertEqual(app.btn_clear_cache.winfo_manager(), "pack")
                    self.assertGreater(app.btn_wrench.winfo_x(), app.btn_clear_cache.winfo_x())
                finally:
                    app._exit()

    def test_local_track_artist_displayed_on_toolbar_and_preserved_across_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_artist_test.json"
            dummy_wav = Path(tmp) / "join_us.wav"
            dummy_wav.write_bytes(b"RIFF....WAVEfmt ....data....")
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    track = main.LocalTrack(
                        title="Join Us for a Bite",
                        path=str(dummy_wav),
                        duration=236.0,
                        artist="JT Music, Andrea Storm Kaden",
                    )
                    app.local_tracks = [track]
                    app.visible_tracks = [track]
                    app._filter_tracks()

                    # Play track
                    with patch.object(app.engine, "play") as mock_play:
                        app._play_local_track(track)
                        app.update()

                        # Verify StreamTrack received artist
                        call_track = mock_play.call_args[0][0]
                        self.assertEqual(getattr(call_track, "artist", ""), "JT Music, Andrea Storm Kaden")

                    # Verify toolbar UI labels show Title and Artist
                    self.assertEqual(app.lbl_np_title.cget("text"), "Join Us for a Bite")
                    self.assertEqual(app.lbl_np_artist.cget("text"), "  •  JT Music, Andrea Storm Kaden")

                    # Simulate audio engine emitting resolved track title callback (which previously wiped artist)
                    app._on_track_title("Join Us for a Bite")
                    app.update()

                    # Artist must still be preserved!
                    self.assertEqual(app.lbl_np_title.cget("text"), "Join Us for a Bite")
                    self.assertEqual(app.lbl_np_artist.cget("text"), "  •  JT Music, Andrea Storm Kaden")
                finally:
                    app._exit()

    def test_now_playing_centered_box_layout_and_elements_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_center_layout.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # np_center_box exists inside np_container
                    self.assertTrue(hasattr(app, "np_center_box"))
                    self.assertIs(app.np_center_box.master, app.np_container)

                    # np_center_box uses place with relx=0.5, rely=0.5, anchor=center
                    p_info = app.np_center_box.place_info()
                    self.assertEqual(p_info.get("relx"), "0.5")
                    self.assertEqual(p_info.get("rely"), "0.5")
                    self.assertEqual(p_info.get("anchor"), "center")

                    # Children of np_center_box are title, artist, cover in order
                    children = app.np_center_box.winfo_children()
                    self.assertIn(app.lbl_np_title, children)
                    self.assertIn(app.lbl_np_artist, children)
                    self.assertIn(app.lbl_np_cover, children)
                    t_idx = children.index(app.lbl_np_title)
                    a_idx = children.index(app.lbl_np_artist)
                    c_idx = children.index(app.lbl_np_cover)
                    self.assertTrue(t_idx < a_idx < c_idx)
                finally:
                    app._exit()

    def test_yt_panel_titlebar_and_bottom_bar_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_yt_removals.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Title bar and bottom bar are completely removed
                    self.assertIsNone(app.yt_titlebar)
                    self.assertIsNone(app.btn_yt_close_x)
                    self.assertIsNone(app.yt_bottom)
                    self.assertIsNone(app.btn_yt_play)
                    self.assertIsNone(app.btn_yt_cache)
                    self.assertIsNone(app.btn_yt_close)

                    # Table box expands fully
                    self.assertTrue(hasattr(app, "yt_table_box"))
                    self.assertEqual(app.yt_table_box.winfo_manager(), "pack")

                    # Right click context menu exists for tracks
                    self.assertTrue(hasattr(app, "_yt_context_menu"))
                finally:
                    app._exit()

    def test_full_ui_localization_uk_and_en(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_i18n.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Default: Ukrainian
                    self.assertEqual(app.cat_hdr_lbl1.cget("text"), "Категорія")
                    self.assertEqual(app.cat_title_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.lbl_stats_sounds.cget("text"), "Звуки: ")
                    self.assertEqual(app.lbl_stats_plays.cget("text"), "Лічильник відтворень: ")
                    self.assertEqual(app.lbl_stats_sel.cget("text"), "Вибрано: ")
                    self.assertEqual(app.btn_clear_cache.cget("text"), "🗑 Очистити кеш")
                    self.assertEqual(app.btn_yt_search.cget("text"), "🔍 Знайти")
                    self.assertTrue(app.btn_yt_filter.cget("text").startswith("Фільтр:"))

                    # Switch to English
                    app.lang_var.set("English")
                    app._apply_language()
                    app.update()

                    self.assertEqual(app.cat_hdr_lbl1.cget("text"), "Category")
                    self.assertEqual(app.cat_title_lbl.cget("text"), "My Sounds")
                    self.assertEqual(app.lbl_stats_sounds.cget("text"), "Sounds: ")
                    self.assertEqual(app.lbl_stats_plays.cget("text"), "Playback count: ")
                    self.assertEqual(app.lbl_stats_sel.cget("text"), "Selected: ")
                    self.assertEqual(app.btn_clear_cache.cget("text"), "🗑 Clear cache")
                    self.assertEqual(app.btn_yt_search.cget("text"), "🔍 Search")
                    self.assertTrue(app.btn_yt_filter.cget("text").startswith("Filter:"))
                    self.assertEqual(app.btn_back_to_results.cget("text"), "← Back to results")
                    # Photo 1 verification: top playback bar nothing playing display
                    self.assertEqual(app.lbl_np_title.cget("text"), "Nothing is playing")

                    # Photo 2 verification: search status results found and badges in English
                    app.yt_results_data = [{"title": "Album 1", "artist": "Artist 1", "duration": 180, "result_type": "album"}]
                    app._apply_language()
                    app.update()
                    self.assertEqual(app.yt_status_lbl.cget("text"), "Results found: 1")

                    # Switch back to Ukrainian
                    app.lang_var.set("Українська")
                    app._apply_language()
                    app.update()

                    self.assertEqual(app.cat_hdr_lbl1.cget("text"), "Категорія")
                    self.assertEqual(app.cat_title_lbl.cget("text"), "Мої звуки")
                    self.assertEqual(app.btn_clear_cache.cget("text"), "🗑 Очистити кеш")
                    self.assertEqual(app.btn_yt_search.cget("text"), "🔍 Знайти")
                finally:
                    app._exit()

    def test_filter_track_count_slider_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_filter_slider.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    self.assertEqual(app.yt_search_limit_var.get(), 35)

                    # Change track count to 20
                    app.yt_search_limit_var.set(20)
                    app._save_state()

                    # Verify saved to state file
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.assertEqual(data.get("yt_search_limit"), 20)

                    # Toggle in-frame filter panel
                    app._toggle_yt_filter_flyout()
                    self.assertIsNotNone(app._yt_filter_flyout)
                    self.assertTrue(app.yt_filter_panel.winfo_exists())
                    self.assertEqual(app.yt_filter_panel.winfo_manager(), "pack")
                    self.assertTrue(app.btn_yt_filter.cget("text").endswith("▴"))

                    # Close panel
                    app._close_yt_filter_flyout()
                    self.assertIsNone(app._yt_filter_flyout)
                    self.assertEqual(app.yt_filter_panel.winfo_manager(), "")
                    self.assertTrue(app.btn_yt_filter.cget("text").endswith("▾"))

                    # Mock search_youtube to verify f_limit parameter is passed
                    with patch.object(main.AudioEngine, "search_youtube", return_value=[]) as mock_search:
                        app.yt_query_var.set("test query")
                        app._perform_yt_search()
                        mock_search.assert_called_with("test query", limit=20, filter_mode="all", youtube_priority=False)
                finally:
                    app._exit()

    def test_album_autoplay_advances_when_broadcast_muted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_album_mute.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Mute broadcast to others
                    app.mute_track_to_others_var.set(True)

                    # Set up album queue with 2 tracks
                    track1 = {"title": "Song 1", "artist": "Artist 1", "url": "https://music.youtube.com/watch?v=1", "duration": 180.0}
                    track2 = {"title": "Song 2", "artist": "Artist 2", "url": "https://music.youtube.com/watch?v=2", "duration": 200.0}
                    app._active_album_queue = [track1, track2]
                    app._album_track_index = 0

                    with patch.object(app, "_play_album_track") as mock_play:
                        # Call _on_finished
                        app._on_finished()
                        app.update()

                        # Album track index advanced to 1, and _play_album_track called with track2
                        self.assertEqual(app._album_track_index, 1)
                        mock_play.assert_called_once_with(track2)
                finally:
                    app._exit()


    def test_youtube_search_priority_filter_and_persistence(self) -> None:
        # 1. Test AudioEngine.search_youtube with youtube_priority=True
        mock_ydl_instance = MagicMock()
        mock_ydl_instance.__enter__.return_value = mock_ydl_instance
        mock_ydl_instance.extract_info.return_value = {
            "entries": [
                {
                    "id": "abc123vid",
                    "title": "Funny Meme Sound",
                    "uploader": "Meme Channel",
                    "duration": 42.0,
                    "thumbnail": "https://img.youtube.com/vi/abc123vid/hqdefault.jpg",
                }
            ]
        }
        with patch("musicpad.audio_engine.YoutubeDL", return_value=mock_ydl_instance):
            res = AudioEngine.search_youtube("meme sound", limit=15, filter_mode="all", youtube_priority=True)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["title"], "Funny Meme Sound")
            self.assertEqual(res[0]["artist"], "Meme Channel")
            self.assertEqual(res[0]["full_title"], "Funny Meme Sound — Meme Channel")
            self.assertEqual(res[0]["source"], "youtube")
            self.assertEqual(res[0]["result_type"], "video")
            self.assertEqual(res[0]["item_type"], "Відео")
            self.assertEqual(res[0]["video_id"], "abc123vid")
            self.assertEqual(res[0]["duration"], 42.0)

        # 2. Test UI state, independence from album filter, and persistence
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state_yt_priority.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # Initial state
                    self.assertFalse(app.yt_priority_var.get())
                    self.assertEqual(app.yt_filter_var.get(), "all")
                    self.assertEqual(app.btn_yt_filter.cget("text"), "Фільтр: Всі результати ▾")

                    # Change album filter to "no_albums"
                    app._set_yt_filter("no_albums")
                    self.assertEqual(app.yt_filter_var.get(), "no_albums")
                    self.assertEqual(app.btn_yt_filter.cget("text"), "Фільтр: Без альбомів ▾")

                    # Enable YouTube priority: album filter should NOT change
                    app.yt_priority_var.set(True)
                    app._on_toggle_yt_priority()
                    self.assertTrue(app.yt_priority_var.get())
                    self.assertEqual(app.yt_filter_var.get(), "no_albums")
                    self.assertEqual(app.btn_yt_filter.cget("text"), "Фільтр: YouTube (за назвою) ▾")

                    # Open filter flyout and verify visual rendering
                    app._toggle_yt_filter_flyout()
                    self.assertTrue(app.btn_yt_filter.cget("text").endswith("▴"))

                    # Close flyout
                    app._close_yt_filter_flyout()
                    self.assertTrue(app.btn_yt_filter.cget("text").endswith("▾"))

                    # Verify _perform_yt_search passes youtube_priority=True
                    with patch.object(main.AudioEngine, "search_youtube", return_value=[]) as mock_search:
                        app.yt_query_var.set("minecraft sound")
                        app._perform_yt_search()
                        mock_search.assert_called_with("minecraft sound", limit=35, filter_mode="no_albums", youtube_priority=True)

                    # Disable YouTube priority: returns to "Без альбомів"
                    app.yt_priority_var.set(False)
                    app._on_toggle_yt_priority()
                    self.assertFalse(app.yt_priority_var.get())
                    self.assertEqual(app.yt_filter_var.get(), "no_albums")
                    self.assertEqual(app.btn_yt_filter.cget("text"), "Фільтр: Без альбомів ▾")

                    # Test persistence in state.json
                    app.yt_priority_var.set(True)
                    app._save_state()

                    raw_saved = json.loads(cfg_file.read_text(encoding="utf-8"))
                    self.assertTrue(raw_saved.get("yt_search_priority"))
                    self.assertEqual(raw_saved.get("yt_search_filter"), "no_albums")

                    # Test loading saved state
                    app.yt_priority_var.set(False)
                    app._load_saved_state()
                    self.assertTrue(app.yt_priority_var.get())
                    self.assertEqual(app.yt_filter_var.get(), "no_albums")
                    self.assertEqual(app.btn_yt_filter.cget("text"), "Фільтр: YouTube (за назвою) ▾")
                finally:
                    app._exit()

    def test_direct_url_recognition_and_playlist_parsing(self) -> None:
        # 1. URL detection
        self.assertTrue(main.MusicPadApp._is_direct_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(main.MusicPadApp._is_direct_url("http://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(main.MusicPadApp._is_direct_url("https://music.youtube.com/watch?v=123"))
        self.assertTrue(main.MusicPadApp._is_direct_url("https://www.youtube.com/shorts/sF4J0aZ8g0A"))
        self.assertTrue(main.MusicPadApp._is_direct_url("www.youtube.com/watch?v=123"))
        self.assertTrue(main.MusicPadApp._is_direct_url("youtube.com/watch?v=123"))
        self.assertTrue(main.MusicPadApp._is_direct_url("youtu.be/123"))
        self.assertTrue(main.MusicPadApp._is_direct_url("https://example.com/audio.mp3"))

        self.assertFalse(main.MusicPadApp._is_direct_url("metallica enter sandman"))
        self.assertFalse(main.MusicPadApp._is_direct_url("youtube hits 2024"))
        self.assertFalse(main.MusicPadApp._is_direct_url(""))
        self.assertFalse(main.MusicPadApp._is_direct_url("   "))

        # 2. URL normalization
        self.assertEqual(
            main.MusicPadApp._normalize_url("www.youtube.com/watch?v=123"),
            "https://www.youtube.com/watch?v=123",
        )
        self.assertEqual(
            main.MusicPadApp._normalize_url("https://youtu.be/123"),
            "https://youtu.be/123",
        )

        # 3. Playlist / album detection
        self.assertEqual(
            main.MusicPadApp._parse_playlist_url("https://www.youtube.com/playlist?list=PL12345"),
            (True, "PL12345"),
        )
        self.assertEqual(
            main.MusicPadApp._parse_playlist_url("https://music.youtube.com/playlist?list=VL12345"),
            (True, "VL12345"),
        )
        self.assertEqual(
            main.MusicPadApp._parse_playlist_url("https://music.youtube.com/browse/MPREb_123"),
            (True, "MPREb_123"),
        )

        # Single video with &list= should play the single video directly
        self.assertEqual(
            main.MusicPadApp._parse_playlist_url("https://www.youtube.com/watch?v=XYZ123&list=PL12345"),
            (False, ""),
        )
        self.assertEqual(
            main.MusicPadApp._parse_playlist_url("https://youtu.be/XYZ123?list=PL12345"),
            (False, ""),
        )
        self.assertEqual(
            main.MusicPadApp._parse_playlist_url("https://www.youtube.com/shorts/XYZ123"),
            (False, ""),
        )

    def test_direct_url_playback_on_search_and_paste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. Direct URL search invocation intercepts search_youtube and calls _play_url
                    with patch.object(main.AudioEngine, "search_youtube") as mock_search, \
                         patch.object(app, "_play_url") as mock_play_url:
                        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                        app.yt_query_var.set(test_url)
                        app._perform_yt_search()

                        # AudioEngine.search_youtube MUST NOT be called!
                        mock_search.assert_not_called()
                        # Direct playback MUST be called!
                        mock_play_url.assert_called_once()
                        self.assertEqual(app.url_var.get(), test_url)
                        self.assertIn("Відтворення за посиланням:", app.yt_status_lbl.cget("text"))

                    # 2. Entering URL in "My Sounds" search bar switches category and triggers playback
                    app._select_category("my_sounds")
                    self.assertEqual(app.current_category, "my_sounds")
                    with patch.object(app, "_play_url") as mock_play_url:
                        test_url2 = "https://youtu.be/9bZkp7q19f0"
                        app.search_var.set(test_url2)
                        res = app._on_my_sounds_return()
                        self.assertEqual(res, "break")
                        self.assertEqual(app.search_var.get(), "")
                        self.assertEqual(app.current_category, "youtube")
                        self.assertEqual(app.url_var.get(), test_url2)
                        mock_play_url.assert_called_once()

                    # 3. Playlist URL triggers _open_album_view with autoplay_first=True
                    with patch.object(app, "_open_album_view") as mock_album_view:
                        playlist_url = "https://www.youtube.com/playlist?list=PLrAlr_123"
                        app.yt_query_var.set(playlist_url)
                        app._perform_yt_search()

                        mock_album_view.assert_called_once()
                        called_item = mock_album_view.call_args[0][0]
                        called_kwargs = mock_album_view.call_args[1]
                        self.assertEqual(called_item["browse_id"], "PLrAlr_123")
                        self.assertTrue(called_kwargs.get("autoplay_first"))

                    # 4. Smart clipboard entry paste: normal text vs direct URL
                    with patch.object(app, "clipboard_get", return_value="Linkin Park - Numb"):
                        app.yt_search_entry.delete(0, tk.END)
                        ret = app._paste_into_entry(app.yt_search_entry)
                        self.assertEqual(ret, "break")
                        self.assertEqual(app.yt_query_var.get(), "Linkin Park - Numb")

                    with patch.object(app, "clipboard_get", return_value="local search keyword"):
                        app.search_entry.delete(0, tk.END)
                        ret = app._paste_into_entry(app.search_entry)
                        self.assertEqual(ret, "break")
                        self.assertEqual(app.search_var.get(), "local search keyword")

                    # 5. Right-click context menu generates successfully
                    mock_ev = MagicMock()
                    mock_ev.x_root = 100
                    mock_ev.y_root = 100
                    with patch("tkinter.Menu.tk_popup") as mock_popup:
                        app._show_entry_context_menu(app.yt_search_entry, mock_ev)
                        mock_popup.assert_called_once_with(100, 100)
                finally:
                    app._exit()

    def test_safe_ui_call_kwargs_and_async_album_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "state.json"
            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                app = main.MusicPadApp()
                try:
                    # 1. _safe_ui_call handles kwargs and _drain_ui_queue executes properly
                    called_kwargs = {}
                    def sample_fn(a: int, b: str, flag: bool = False) -> None:
                        called_kwargs.update({"a": a, "b": b, "flag": flag})

                    app._safe_ui_call(sample_fn, 42, "hello", flag=True)
                    self.assertEqual(len(called_kwargs), 0)
                    app._drain_ui_queue()
                    self.assertEqual(called_kwargs, {"a": 42, "b": "hello", "flag": True})

                    # 2. _open_album_view with sync=False and autoplay_first=True dispatches via background thread
                    mock_tracks = [
                        {"title": "Track 1", "artist": "Artist", "duration": 180.0, "url": "https://youtu.be/1"},
                        {"title": "Track 2", "artist": "Artist", "duration": 200.0, "url": "https://youtu.be/2"},
                    ]
                    album_item = {
                        "title": "Test Album",
                        "artist": "Artist",
                        "browse_id": "MPREb_test",
                        "result_type": "album",
                    }
                    with patch.object(main.AudioEngine, "get_album", return_value={"title": "Test Album", "artist": "Artist", "tracks": mock_tracks}),                          patch.object(app, "_play_album_track") as mock_play_track:
                        app._open_album_view(album_item, sync=False, autoplay_first=True)
                        time.sleep(0.15)
                        app._drain_ui_queue()

                        self.assertTrue(app._viewing_album)
                        self.assertEqual(len(app._current_album_tracks), 2)
                        self.assertEqual(app._active_album_queue, mock_tracks)
                        self.assertEqual(app._album_track_index, 0)
                        time.sleep(0.08)
                        app.update()
                        mock_play_track.assert_called_once_with(mock_tracks[0])

                    # 3. Graceful handling of fetch errors and exceptions in fetch_album_worker
                    with patch.object(main.AudioEngine, "get_album", side_effect=RuntimeError("Network explosion")):
                        err_item = {"title": "Crashing Album", "browse_id": "MPREb_crash"}
                        app._open_album_view(err_item, sync=False)
                        time.sleep(0.15)
                        app._drain_ui_queue()
                        self.assertTrue(
                            "помилка" in app.yt_status_lbl.cget("text").lower()
                            or "не знайдено" in app.yt_status_lbl.cget("text").lower()
                        )

                    # Unexpected error during worker preparation triggers on_error_ui without blocking on network
                    with patch.object(main.AudioEngine, "get_album", return_value={}):
                        bad_item = {"title": "Exploding Item", "browse_id": "MPREb_err", "tracks": 12345}
                        app._open_album_view(bad_item, sync=False)
                        time.sleep(0.1)
                        app._drain_ui_queue()
                        self.assertIn("Помилка завантаження альбому: Exploding Item", app.yt_status_lbl.cget("text"))
                finally:
                    app._exit()

    def test_ring_buffer_and_virtual_cable_master_reader(self) -> None:
        from musicpad.audio_engine import Float32RingBuffer, READER_INJECTION, READER_MONITOR, ROUTING_MODE_VIRTUAL_CABLE, ROUTING_MODE_INJECTION
        import numpy as np

        # 1. Float32RingBuffer pacing with master READER_INJECTION
        rb = Float32RingBuffer(capacity_frames=1024, channels=2)
        rb.register_reader(READER_MONITOR)
        rb.set_master_reader(READER_INJECTION)

        chunk = np.zeros((512, 2), dtype=np.float32)
        w1 = rb.write(chunk)
        w2 = rb.write(chunk)
        self.assertEqual(w1 + w2, 1024)

        # Buffer is full (0 frames space). When master READER_INJECTION reads 512 frames, space is freed immediately
        rb.read(512, reader_id=READER_INJECTION)
        space = rb.write_space()
        self.assertEqual(space, 512)
        w3 = rb.write(chunk)
        self.assertEqual(w3, 512)

        # 2. AudioEngine in Virtual Cable mode sets master reader to READER_INJECTION even with monitor active
        engine = main.AudioEngine(
            output_device=None,
            input_device=None,
            monitor_device=1,
            routing_mode=ROUTING_MODE_VIRTUAL_CABLE,
        )
        try:
            self.assertEqual(engine.routing_mode, ROUTING_MODE_VIRTUAL_CABLE)
            self.assertEqual(engine._ring_buffer._master_reader_id, READER_INJECTION)
        finally:
            engine.shutdown()

        engine_inj = main.AudioEngine(
            output_device=None,
            input_device=None,
            monitor_device=1,
            routing_mode=ROUTING_MODE_INJECTION,
        )
        try:
            self.assertEqual(engine_inj.routing_mode, ROUTING_MODE_INJECTION)
        finally:
            engine_inj.shutdown()

    def test_endpoint_matching_usb_suffix_and_mme_truncation(self) -> None:
        fake_eps = [
            CaptureEndpoint(
                guid="{11111111-1111-1111-1111-111111111111}",
                endpoint_name="Мікрофон",
                adapter_name="Realtek(R) Audio",
                enhancements_enabled=True,
                exclusive_allowed=False,
                efx_clsid=MUSICPAD_APO_CLSID,
                friendly_name="Мікрофон (Realtek(R) Audio)",
            ),
            CaptureEndpoint(
                guid="{22222222-2222-2222-2222-222222222222}",
                endpoint_name="Стерео мікшер",
                adapter_name="Realtek(R) Audio",
                enhancements_enabled=True,
                exclusive_allowed=False,
                efx_clsid="",
                friendly_name="Стерео мікшер (Realtek(R) Audio)",
            ),
            CaptureEndpoint(
                guid="{33333333-3333-3333-3333-333333333333}",
                endpoint_name="Microphone",
                adapter_name="fifine Microphone",
                enhancements_enabled=True,
                exclusive_allowed=False,
                efx_clsid="",
                friendly_name="Microphone (5- fifine Microphone)",
            ),
            CaptureEndpoint(
                guid="{44444444-4444-4444-4444-444444444444}",
                endpoint_name="Мікрофон гарнітури",
                adapter_name="HyperX Cloud II Wireless",
                enhancements_enabled=True,
                exclusive_allowed=False,
                efx_clsid="",
                friendly_name="Мікрофон гарнітури (2- HyperX Cloud II Wireless)",
            ),
        ]

        # 1. Trailing index suffix stripping
        ep1 = find_endpoint_for_sounddevice("Мікрофон (Realtek(R) Audio) (1)", endpoints=fake_eps)
        self.assertIsNotNone(ep1)
        assert ep1 is not None
        self.assertEqual(ep1.guid, "{11111111-1111-1111-1111-111111111111}")

        # 2. USB port prefix and 31-char MME truncation
        ep2 = find_endpoint_for_sounddevice("Microphone (5- fifine Microphon", endpoints=fake_eps)
        self.assertIsNotNone(ep2)
        assert ep2 is not None
        self.assertEqual(ep2.guid, "{33333333-3333-3333-3333-333333333333}")

        # 3. Truncation with trailing index
        ep3 = find_endpoint_for_sounddevice("Microphone (5- fifine Microphon (2)", endpoints=fake_eps)
        self.assertIsNotNone(ep3)
        assert ep3 is not None
        self.assertEqual(ep3.guid, "{33333333-3333-3333-3333-333333333333}")

        # 4. Disambiguation between Microphone and Stereo Mix on same Realtek adapter
        ep_mic = find_endpoint_for_sounddevice("Мікрофон (Realtek(R) Audio)", endpoints=fake_eps)
        self.assertIsNotNone(ep_mic)
        assert ep_mic is not None
        self.assertEqual(ep_mic.guid, "{11111111-1111-1111-1111-111111111111}")

        ep_mix = find_endpoint_for_sounddevice("Стерео мікшер (Realtek(R) Audio)", endpoints=fake_eps)
        self.assertIsNotNone(ep_mix)
        assert ep_mix is not None
        self.assertEqual(ep_mix.guid, "{22222222-2222-2222-2222-222222222222}")

        # 5. Long localized name prefix
        ep_long = find_endpoint_for_sounddevice("Мікрофон гарнітури (2- HyperX ", endpoints=fake_eps)
        self.assertIsNotNone(ep_long)
        assert ep_long is not None
        self.assertEqual(ep_long.guid, "{44444444-4444-4444-4444-444444444444}")

    def test_endpoint_validation_and_phantom_guid_rejection(self) -> None:
        from musicpad.apo_installer import (
            LEGACY_SPIKE_CLSIDS,
            get_driver_status,
            install_driver,
        )

        # Steam Streaming Mic must NOT be in legacy spike CLSIDs
        self.assertNotIn("{3b97825e-5d34-4c2e-8a53-7bc22f9e014a}", {c.lower() for c in LEGACY_SPIKE_CLSIDS})
        self.assertIn("{8e65b49a-5b3e-4b3d-9e5d-2c1b8a4f7e21}", {c.lower() for c in LEGACY_SPIKE_CLSIDS})

        # Phantom GUID rejection in get_driver_status
        phantom_guid = "{99999999-9999-9999-9999-999999999999}"
        with patch("winreg.OpenKey", side_effect=OSError(2, "File not found")):
            status = get_driver_status(target_endpoint_guid=phantom_guid)
            self.assertIn(status.state, ("not_installed", "inactive"))
            self.assertFalse(status.is_healthy)

        # Phantom GUID rejection in install_driver
        with patch("musicpad.apo_installer.is_admin", return_value=True):
            with patch("winreg.OpenKey", side_effect=OSError(2, "File not found")):
                with self.assertRaises(ValueError) as ctx:
                    install_driver(endpoint_guid=phantom_guid)
                self.assertIn("не знайдено", str(ctx.exception))

    def test_all_efx_modes_and_vendor_mfx_sfx_suspension(self) -> None:
        from musicpad.apo_installer import (
            ALL_EFX_PROCESSING_MODES,
            AUDIO_SIGNALPROCESSINGMODE_DEFAULT,
            AUDIO_SIGNALPROCESSINGMODE_COMMUNICATIONS,
            AUDIO_SIGNALPROCESSINGMODE_SPEECH,
            AUDIO_SIGNALPROCESSINGMODE_RAW,
            _restore_single_endpoint_from_backup,
        )

        self.assertEqual(len(ALL_EFX_PROCESSING_MODES), 4)
        self.assertIn(AUDIO_SIGNALPROCESSINGMODE_DEFAULT, ALL_EFX_PROCESSING_MODES)
        self.assertIn(AUDIO_SIGNALPROCESSINGMODE_COMMUNICATIONS, ALL_EFX_PROCESSING_MODES)
        self.assertIn(AUDIO_SIGNALPROCESSINGMODE_SPEECH, ALL_EFX_PROCESSING_MODES)
        self.assertIn(AUDIO_SIGNALPROCESSINGMODE_RAW, ALL_EFX_PROCESSING_MODES)

        # Test full restore of vendor FxProperties
        saved_backup = {
            "FxProperties": {
                "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},5": {"type": 1, "value": "{vendor-sfx-guid}"},
                "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},6": {"type": 1, "value": "{vendor-mfx-guid}"},
            },
            "Properties": {},
        }
        mock_set_vals: list[tuple[str, Any]] = []
        mock_deleted: list[str] = []

        with patch("musicpad.apo_installer._delete_hklm_subkey_tree"):
            with patch("musicpad.apo_installer._open_hklm_key_backup_restore", return_value=999):
                with patch("musicpad.apo_installer._read_all_registry_values", return_value={}):
                    with patch("winreg.DeleteValue", side_effect=lambda h, name: mock_deleted.append(name)):
                        with patch("winreg.SetValueEx", side_effect=lambda h, name, r, t, val: mock_set_vals.append((name, val))):
                            with patch("winreg.CloseKey"):
                                _restore_single_endpoint_from_backup("{11111111-1111-1111-1111-111111111111}", saved_backup)

        restored_keys = [k for k, v in mock_set_vals]
        self.assertIn("{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},5", restored_keys)
        self.assertIn("{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},6", restored_keys)

    def test_shm_uninitialized_format_and_live_telemetry_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shm_file = Path(tmp) / "uninit.shm"
            shm = InjectionShm(shm_file)
            shm.open(acquire_lease=True)
            try:
                shm.reset_storage()
                st = shm.get_apo_status()
                # Before audiodg.exe writes to shared memory, sample_rate and channels must be 0
                self.assertEqual(st.sample_rate, 0)
                self.assertEqual(st.channels, 0)
                self.assertFalse(st.is_alive)
            finally:
                shm.close()

            # Verify AudioEngine emits on_apo_warning if track is active in injection mode and APO is not alive
            warned_messages: list[str] = []
            engine = AudioEngine(
                routing_mode=ROUTING_MODE_INJECTION,
                on_apo_warning=warned_messages.append,
                shm_path=shm_file,
            )
            try:
                self.assertIsNotNone(engine.on_apo_warning)
            finally:
                engine.shutdown()

    def test_ui_routing_mode_switch_prompts_and_endpoint_transfer(self) -> None:
        app = main.MusicPadApp()
        try:
            app.routing_mode_var.set(ROUTING_MODE_VIRTUAL_CABLE)
            not_inst_status = DriverStatus(
                state="not_installed",
                installed_endpoint_guid="",
                target_endpoint_guid="{11111111-1111-1111-1111-111111111111}",
                reason="Драйвер MusicPad не встановлено",
            )
            with patch.object(app, "_refresh_driver_status", return_value=not_inst_status):
                with patch("tkinter.messagebox.askyesno", return_value=False):
                    app.routing_mode_var.set(ROUTING_MODE_INJECTION)
                    app._on_routing_mode_changed()
                    # User cancelled -> reverts to ROUTING_MODE_VIRTUAL_CABLE
                    self.assertEqual(app.routing_mode_var.get(), ROUTING_MODE_VIRTUAL_CABLE)

            # Test prompt to transfer driver on wrong_endpoint
            wrong_status = DriverStatus(
                state="wrong_endpoint",
                installed_endpoint_guid="{11111111-1111-1111-1111-111111111111}",
                target_endpoint_guid="{22222222-2222-2222-2222-222222222222}",
                reason="Драйвер встановлено на інший мікрофон (потрібно перенести)",
            )
            app.routing_mode_var.set(ROUTING_MODE_INJECTION)
            with patch.object(app, "_refresh_driver_status", return_value=wrong_status):
                with patch("tkinter.messagebox.askyesno", return_value=True) as mock_ask:
                    with patch.object(app, "_install_driver_uac_action") as mock_inst_action:
                        app.mix_mic_var.set("New Mic")
                        app._mix_mic_changed()
                        mock_ask.assert_called_once()
                        mock_inst_action.assert_called_once()

            # Test warning on starting playback when driver is inactive
            inactive_status = DriverStatus(
                state="inactive",
                installed_endpoint_guid="{11111111-1111-1111-1111-111111111111}",
                target_endpoint_guid="{11111111-1111-1111-1111-111111111111}",
                reason="Файл драйвера або реєстрація COM відсутні",
            )
            with patch.object(app, "_refresh_driver_status", return_value=inactive_status):
                with patch.object(app, "notify_error") as mock_notify:
                    with patch.object(app, "_ensure_engine") as mock_eng:
                        app._start(StreamTrack("Test", "dummy.mp3"))
                        mock_notify.assert_called_once()
                        self.assertIn("не активний", mock_notify.call_args[0][0])
        finally:
            app._exit()

    def test_cross_pc_config_migration_auto_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "foreign_config.json"
            foreign_data = {
                "routing_mode": ROUTING_MODE_INJECTION,
                "target_mic_guid": "{phantom-foreign-guid-from-other-pc}",
                "mix_mic": "Realtek Microphone",
            }
            cfg_file.write_text(json.dumps(foreign_data), encoding="utf-8")

            active_eps = [
                CaptureEndpoint(
                    guid="{active-current-pc-guid}",
                    endpoint_name="Realtek Microphone",
                    adapter_name="Realtek Audio",
                    enhancements_enabled=True,
                    exclusive_allowed=False,
                    efx_clsid="",
                    friendly_name="Realtek Microphone (Realtek Audio)",
                )
            ]

            with patch.object(main.MusicPadApp, "_config_path", return_value=cfg_file):
                with patch("main.list_capture_endpoints", return_value=active_eps):
                    app = main.MusicPadApp()
                    try:
                        resolved = app._resolve_selected_mic_guid()
                        self.assertEqual(resolved, "{active-current-pc-guid}")
                        self.assertEqual(app.target_mic_guid_var.get(), "{active-current-pc-guid}")
                    finally:
                        app._exit()



if __name__ == "__main__":
    unittest.main()
