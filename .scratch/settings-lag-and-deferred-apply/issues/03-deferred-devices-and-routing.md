# 03: Deferred Devices & Routing Mode with Apply-time UAC prompts

**What to build:** Integrate the Devices tab (Routing Mode `Virtual Cable` vs `Microphone Injection`, Target Microphone, Track Source, Monitoring, and Virtual Cable output) into the draft-state Settings lifecycle so changing devices or routing in the dialog updates in-dialog UI previews (such as enabling/disabling the Virtual Cable dropdown) without mutating `self.engine`, restarting PortAudio streams, saving `state.json`, or prompting UAC until **Apply** or **OK** is clicked. Update existing immediate-apply tests to match the Apply-only contract.

**Blocked by:** 02: Draft settings lifecycle, dirty Apply button & diffed commit

**Status:** resolved

- [x] Changing Routing Mode, Target Microphone, Track Source, Monitoring, or Virtual Cable in Settings does not call engine setters, restart streams, or write `state.json` until **Apply**/**OK**.
- [x] Toggling Routing Mode in the dialog still disables/enables the Virtual Cable dropdown visually on the draft control.
- [x] UAC install/relocation prompts for `Microphone Injection` trigger on **Apply**/**OK** when committing a changed Routing Mode or Target Microphone whose Driver Status requires action, rather than on immediate radio/dropdown click.
- [x] **Cancel** / `X` discards uncommitted device/routing edits; explicit *Install / Repair / Uninstall Driver* buttons continue to work.
- [x] Existing immediate-apply tests are updated to the Apply-only contract and pass alongside new regression tests.
