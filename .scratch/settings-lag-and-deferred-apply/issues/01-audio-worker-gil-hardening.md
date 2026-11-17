# 01: Audio worker priority, GIL switch interval & unchanged-setter guard

**What to build:** Harden the audio worker against main-thread UI and Python GIL bursts by raising the audio worker's Windows thread priority while active, lowering Python's GIL switch interval during audio processing (restoring the previous interval on stop), ensuring PortAudio output stream buffering provides sufficient headroom without degrading microphone latency, and ignoring unchanged audio setter calls so playback stays smooth without redundant stream restarts.

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] Audio worker elevates its Windows thread priority while active and exits cleanly on stop without failing on non-Windows or mocked streams.
- [x] Python's GIL switch interval is lowered during active audio engine operation and restored cleanly when stopped.
- [x] Unchanged device, routing mode, or Voice Processing setter calls do not trigger `_devices_dirty` resets or redundant DeepFilterNet reloads.
- [x] Automated tests verify priority/switch-interval lifecycle, unchanged-setter idempotency, and playback cadence resilience under concurrent main-thread CPU bursts.
