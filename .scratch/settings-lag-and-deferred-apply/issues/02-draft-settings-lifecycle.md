# 02: Draft settings lifecycle, dirty Apply button & diffed commit (Audio, Voice FX, Hotkeys, Interface)

**What to build:** Introduce an isolated draft-state model for the Settings dialog across Audio Normalization, Noise Reduction, Voice Boost, Special Hotkeys, and Interface tabs so nothing changes live until **Apply** or **OK** is clicked: **Apply** enables only when draft settings differ from live state, **OK** commits and closes, **Cancel** / window close discards all uncommitted edits, **Reset** resets only draft controls, in-dialog slider `%`/`dB` readouts remain live without touching the engine or disk, and **Apply** diffs changes so language/theme/hotkeys/Voice FX only re-apply when changed.

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] Changing Voice Processing (DeepFilterNet, White Noise Removal, Voice Boost), Special Hotkeys, Audio Normalization, Language, or Theme inside Settings leaves live app state, audio engine, hotkeys, and `state.json` untouched until **Apply** or **OK**.
- [x] **Apply** button is disabled when draft equals live state, enables on any draft change, and disables again once applied.
- [x] **Cancel** / `X` reverts all uncommitted edits; **Reset** updates only draft fields without modifying live state until **Apply**/**OK**.
- [x] Slider value labels (`%` / `dB`) still update immediately while dragging inside the dialog.
- [x] Opening Settings or clicking **Apply** with unchanged Language/Theme skips `_apply_language()`, `_apply_theme()`, and tab rebuilds.
