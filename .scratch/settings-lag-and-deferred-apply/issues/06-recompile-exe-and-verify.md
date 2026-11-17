# 06: Recompile portable MusicPad.exe & run full verification

**What to build:** Rebuild the portable Windows `onedir` executable (`MusicPad.exe`) after all code and test changes are complete, verify it passes the packaged `--self-test`, and run the complete automated test suite cleanly.

**Blocked by:** 01: Audio worker priority, GIL switch interval & unchanged-setter guard, 04: Lazy Settings tab construction & caching, 05: Non-blocking Registry driver status, Refresh Devices & async state persistence

**Status:** resolved

- [x] Full automated test suite passes after final edits.
- [x] `MusicPad.exe` is freshly recompiled via PyInstaller (`MusicPad.spec`) in `dist/MusicPad/`.
- [x] Compiled `MusicPad.exe --self-test` exits cleanly with `ok: true`.
