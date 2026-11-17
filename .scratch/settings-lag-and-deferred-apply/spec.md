Status: ready-for-agent

# Deferred Settings Application ("Apply-Only") & Lag-Free Playback

## Problem Statement

When a track is playing in MusicPad, opening the Settings dialog or applying settings causes audible music stutter and UI lag:

1. **Immediate side effects before Apply**: Several controls inside the Settings dialog — including Voice Processing toggles and sliders (DeepFilterNet, White Noise Removal, Voice Boost), Routing Mode (`Virtual Cable` vs `Microphone Injection`), Target Microphone, Track Source, Monitoring (Headphones), Virtual Cable output, Special Hotkeys, and Reset — immediately mutate live application state, reconfigure or restart audio engine streams, trigger UAC driver prompts, and write `state.json` synchronously on click/drag instead of waiting for the **Apply** (or **OK**) button.
2. **Redundant main-thread UI & I/O work on Open and Apply**:
   - Every time Settings opens or **Apply** is clicked, the app unconditionally runs full localization re-application, which rebuilds the menu bar, re-renders YouTube search results, and destroys and recreates all 6 Settings tabs from scratch — even when the language did not change.
   - Opening the Settings dialog for the first time constructs all 6 tabs synchronously in a single burst on the Tkinter main thread.
   - Opening Settings and pressing **Apply** run synchronous Windows Registry endpoint scans (checking Driver Status and active capture endpoints, sometimes twice) and synchronous JSON serialization/file replacement on the main thread.
3. **Python GIL contention with the audio loop**: Because the audio worker runs short `8 ms` (`384`-frame at `48 kHz`) blocks in Python, heavy Tkinter widget construction, Registry calls, or disk writes on the main thread starve the audio worker of the GIL and Windows CPU scheduling, causing buffer underruns and audible playback glitches.

## Solution

- **Full Draft-State ("Apply-Only") Settings Model**:
  - Isolate all Settings dialog controls into an uncommitted draft copy separate from live application state and the audio engine.
  - Modifying controls, dragging sliders, or pressing **Reset** updates only the draft UI (with responsive in-dialog visual previews such as `%` / `dB` readouts, Virtual Cable dropdown enable/disable state, and asynchronous Driver Status preview text) without touching live playback, hotkeys, language, theme, UAC prompts, or `state.json`.
  - **Apply** commits only the settings that actually changed (leaving the dialog open); **OK** commits and closes; **Cancel** / window close (`X`) discards all uncommitted draft edits cleanly.
  - Enable the **Apply** button only when draft state differs from live committed state.
- **Eliminate Main-Thread Stalls on Open & Apply**:
  - Build Settings tabs lazily (only the active tab on first open, remaining tabs on demand when selected) and keep tabs cached instead of destroying/rebuilding them on every open or apply.
  - Only invoke full language re-application when the language actually changed on **Apply**/**OK**, and only invoke theme re-application when the theme actually changed.
  - Diff draft vs live audio settings on **Apply**/**OK** so audio engine setters (and PortAudio stream restarts) only fire when a hardware device, Routing Mode, or Voice Processing value actually changed.
  - Defer driver UAC install/relocation prompts to **Apply**/**OK** when switching to `Microphone Injection` or a different Target Microphone with an unready driver state (while keeping explicit *Install / Repair / Uninstall Driver* and *Refresh Devices* buttons as direct actions).
  - Offload Windows Registry endpoint/Driver Status checks, PortAudio device enumeration (`Refresh Devices`), and `state.json` disk writes off the Tkinter main thread.
- **Harden the Audio Engine Against UI/GIL Spikes**:
  - Raise the audio worker's OS thread priority on Windows and tighten Python's thread switch interval / playback buffering resilience so brief main-thread UI bursts (e.g., lazy tab creation or theme switching) do not cause audible playback stutter, while keeping Microphone Injection and Virtual Cable monitoring latency low.
- **Recompile the Packaged Executable**:
  - Rebuild the portable Windows `onedir` executable (`MusicPad.exe`) and verify it passes the packaged self-test and automated test suite.

## User Stories

1. As a user playing a track, I want opening the Settings dialog to happen smoothly without interrupting or stuttering the music, so that I can check or adjust settings mid-playback.
2. As a user, I want changing any setting in the Settings dialog (Voice Processing, Devices, Routing Mode, Hotkeys, Audio Normalization, Interface) to have zero effect on live playback or saved state until I click **Apply** or **OK**, so that browsing or experimenting never causes unintended audio resets or changes.
3. As a user, I want clicking **Cancel** or closing the Settings window (`X`) to discard every uncommitted change across all tabs (including Voice Processing, Devices, Routing Mode, Hotkeys, Language, Theme, and Interface toggles), so that my active session remains untouched.
4. As a user, I want clicking **Reset** to restore default values only in the draft dialog fields, so that I can inspect the defaults and either confirm with **Apply**/**OK** or back out with **Cancel** without interrupting live playback.
5. As a user, I want the **Apply** button to be disabled when no settings differ from the active configuration and enabled as soon as I edit any draft setting, so that I have clear feedback on whether uncommitted changes exist.
6. As a user, I want clicking **Apply** to commit my changes, disable **Apply** again, and keep the Settings window open, so that I can continue tweaking settings if needed.
7. As a user, I want clicking **OK** to commit any uncommitted changes (same as **Apply**) and close the Settings window cleanly (and close immediately with no redundant work if nothing changed), so that **OK** behaves consistently.
8. As a user dragging the DeepFilterNet Wet/Dry or Voice Boost dB sliders in the dialog, I want the `%` and `dB` value labels next to the sliders to update live as I drag without mutating the audio engine until **Apply**, so that the controls stay responsive without premature DSP/disk overhead.
9. As a user toggling the Routing Mode radio buttons between `Virtual Cable` and `Microphone Injection` in the dialog, I want the Virtual Cable dropdown to visually enable/disable immediately to reflect the draft choice without resetting audio streams or triggering a UAC dialog until **Apply**, so that I can preview options safely.
10. As a user selecting a different Target Microphone in the Devices tab, I want the Driver Status text to update asynchronously in the background for my draft selection without freezing the UI or prompting UAC until **Apply**, so that I know whether a driver install/relocation will be needed before committing.
11. As a user switching to `Microphone Injection` (or changing Target Microphone while in `Microphone Injection`) and clicking **Apply** when the MusicPad Driver is not installed or is on another microphone, I want the UAC installation/relocation confirmation prompt to appear at **Apply** time, so that system changes only occur when I confirm my settings.
12. As a user switching between tabs inside the Settings dialog while a track is playing, I want inactive tabs to build lazily on first visit and remain cached afterwards without freezing the audio loop, so that switching tabs feels instantaneous and stutter-free.
13. As a user clicking **Apply** after editing only non-audio settings (e.g., tray behavior, playback state memory, normalization preference, or hotkeys), I want the audio engine streams to keep playing seamlessly with zero resets or pauses, so that music playback never glitches on unrelated changes.
14. As a user clicking **Apply** without having changed the UI language, I want the app to skip rebuilding the menu bar, YouTube search results, and all Settings tabs, so that applying settings does not stall the main thread.
15. As a user clicking **Apply** when I intentionally switch hardware audio devices (Virtual Cable, Monitoring Headphones, Target Microphone) or Routing Mode, I want only the actual device/routing change to take effect cleanly without redundant stream restarts if the device did not change, so that playback remains as uninterrupted as possible.
16. As a user clicking **Refresh Devices** in the Devices tab while a track is playing, I want device enumeration to run without blocking the main thread or restarting active playback streams, so that refreshing the list does not stutter the music.
17. As a user assigning or clearing Special Hotkeys in the Hotkeys tab, I want hotkey edits to stay in the draft table until **Apply**/**OK** is clicked (and revert on **Cancel**), so that hotkeys follow the exact same Apply-only rule as every other setting.
18. As a user enabling DeepFilterNet or switching Dark/Light theme on **Apply** while music is playing, I want the audio worker thread to hold priority over UI bursts so playback remains smooth without buffer underruns.
19. As a user running the packaged Windows release, I want an updated, recompiled `MusicPad.exe` containing all these fixes and verified by the self-test, so that I can run the fixed build immediately.

## Implementation Decisions

- **Draft Settings State Container in the UI Layer**:
  - Separate live committed settings from Settings dialog draft variables inside the main application UI module across all 6 tabs (Audio Normalization, Noise Reduction, Voice Boost, Special Hotkeys, Interface, and Devices).
  - Track a dirty state by comparing current draft values against live committed values (including Special Hotkeys dictionary equality) and synchronize the **Apply** button state (`normal` vs `disabled`) on any draft edit, Reset, Apply, or Open.
  - Keep in-dialog preview handlers (slider readout labels, Virtual Cable combo enabled/disabled state, and asynchronous Driver Status label preview) strictly scoped to draft widgets; move live side effects (engine calls, special hotkey rebinding, language/theme re-application, UAC prompts, and state persistence) exclusively into the commit path triggered by **Apply** / **OK**.
  - On **Reset**, reset only draft dialog variables and refresh built tab views; leave live variables, engine, and saved configuration untouched until **Apply** / **OK**.
  - On **Cancel** or window close (`WM_DELETE_WINDOW`), discard draft changes and restore dialog state from live values on next open.
- **Lazy Tab Construction, Caching & Diffed Apply**:
  - On first dialog open, create the notebook tabs and build only the initially selected tab; build each remaining tab lazily on `<<NotebookTabChanged>>` (or programmatically when accessed) and mark it as built so it is reused on subsequent opens.
  - On dialog open, sync draft values from live state without invoking full language re-application or destroying/recreating existing tab widgets.
  - On **Apply**, compare each setting category against its previous live value:
    - Run theme re-application only if theme changed.
    - Run full language re-application (and rebuild only already-built Settings tabs) only if language changed.
    - Update Special Hotkeys bindings only if the hotkeys mapping changed.
    - Call audio engine setters (`set_routing_mode`, `set_target_endpoint_guid`, `set_output_device`, `set_input_device`, `set_monitor_device`, `set_deepfilter_state`, `set_white_noise_removal`, `set_voice_boost`) only for values that changed, avoiding unnecessary `_devices_dirty` resets or DeepFilterNet re-invocations.
    - Evaluate UAC install/move prompts for `Microphone Injection` only when committing a change to Routing Mode or Target Microphone whose background-checked Driver Status requires action.
- **Non-Blocking Background Registry, Device Enumeration & State Persistence**:
  - Run Windows Registry capture-endpoint / Driver Status lookups asynchronously on a background worker with generation/staleness guards so stale results never overwrite newer selections on the UI thread.
  - Run **Refresh Devices** hardware queries in a background thread and update dropdown values + draft selection safely on the main thread via `after` without mutating live device selections or interrupting playback.
  - Serialize state snapshots cleanly on the UI thread (copying immutable data) and perform JSON file formatting and atomic disk writes asynchronously in a serialized background writer (coalescing rapid saves and flushing synchronously on app exit so no state is lost on shutdown).
- **Audio Engine GIL & Scheduling Hardening**:
  - In the audio engine module, raise the audio worker thread priority on Windows (via Win32 `SetThreadPriority`) when playback/capture is active, restoring or exiting cleanly when the thread stops.
  - Lower Python's GIL switch interval (`sys.setswitchinterval`) while the audio worker is active (and restore on shutdown) so main-thread Tkinter/Python bursts yield the GIL promptly to the `8 ms` audio loop.
  - Ensure PortAudio stream configuration provides sufficient output buffering headroom for `Virtual Cable` and `Monitoring` without adding perceptible microphone latency, and avoid redundant stream closures when audio setters receive unchanged values.
- **Executable Recompilation**:
  - Rebuild the portable `onedir` bundle using the PyInstaller packaging spec and verify it via the built-in self-test.

## Testing Decisions

- Good tests verify external observable behavior (what the user sees, what state the audio engine and persisted config hold, and whether streams or UI tabs are rebuilt) rather than private internal helper calls.
- **Seam 1 — In-process `MusicPadApp` + mocked-hardware `AudioEngine` (existing GUI test seam)**:
  - Drive Settings interactions on a real `MusicPadApp` instance with mocked `sounddevice` and an isolated temporary `state.json`.
  - Cover:
    - Editing Voice FX, Devices, Routing Mode, Hotkeys, Language, and Theme in the dialog leaves `engine` state, hotkey bindings, UI language/theme, and `state.json` untouched until **Apply** or **OK**.
    - **Apply** button transitions from disabled on open -> enabled on edit -> disabled after **Apply**.
    - **Cancel** / `X` discards all draft edits across all tabs; **Reset** resets only draft controls until **Apply**/**OK**.
    - **Apply** with unchanged settings (or non-audio changes) performs zero tab widget destructions/rebuilds, zero full language re-applications, and zero audio stream resets/re-openings.
    - Lazy tab construction builds only the initial tab on first open and builds other tabs on tab selection.
    - Asynchronous Driver Status preview / device refresh and background state saving complete cleanly and flush on exit.
    - Update existing immediate-apply tests in the suite to match the new Apply-only contract.
- **Seam 2 — `AudioEngine` concurrency & compiled executable `--self-test` (existing engine + build seams)**:
  - Verify `AudioEngine` thread-priority / switch-interval lifecycle and resilience under simulated main-thread CPU/GIL contention with mocked streams.
  - Recompile the `onedir` executable and run the compiled binary `--self-test` and full automated test suite after the final edit.

## Out of Scope

- Crossfading or hot-swapping hardware audio streams with zero gap when the user *intentionally* changes output/monitor/input devices or Routing Mode on **Apply**.
- Rewriting the audio engine or UI in a non-Python language (per ADR-0020).
- Changes to the native C++ APO/DSP DLLs or shared-memory injection ring buffer protocol.

## Further Notes

- Follow existing Glossary terms (`Цільовий мікрофон`, `Моніторинг`, `Режим маршрутизації`, `Віртуальний кабель`, `Ін'єкція в мікрофон`, `Драйвер MusicPad`, `Стан драйвера`, `Обробка голосу`).
- Ensure any background state writer flushes pending writes synchronously during application exit so state saved just before closing is never lost.
