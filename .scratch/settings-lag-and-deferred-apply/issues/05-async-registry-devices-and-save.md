# 05: Non-blocking Registry driver status, Refresh Devices & async state persistence

**What to build:** Move remaining synchronous Windows Registry, PortAudio enumeration, and disk I/O calls off the Tkinter main thread during playback: run Driver Status / capture-endpoint lookups asynchronously (including live Driver Status preview when picking a draft Target Microphone), run *Refresh Devices* enumeration in the background without touching live streams, and write `state.json` asynchronously in a coalescing background writer that flushes synchronously on app exit.

**Blocked by:** 03: Deferred Devices & Routing Mode with Apply-time UAC prompts

**Status:** resolved

- [x] Opening Settings and selecting a draft Target Microphone update Driver Status asynchronously on a background worker with staleness guards so stale results do not overwrite newer selections.
- [x] Clicking *Refresh Devices* enumerates PortAudio devices in a background thread and updates dropdown lists + draft selection without blocking the UI or restarting active audio streams.
- [x] `_save_state()` snapshots state cleanly on the UI thread and performs JSON serialization + atomic disk replacement off the main thread, coalescing rapid saves and flushing synchronously during application exit.
