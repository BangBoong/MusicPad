"""Application paths and repository root resolution for MusicPad."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional


def check_unextracted_archive_guard(
    executable: Optional[str] = None,
    is_frozen: Optional[bool] = None,
    exit_on_error: bool = True,
    show_dialog: bool = True,
    ui_lang_id: Optional[int] = None,
) -> bool:
    """Check if the application is being launched from an unextracted ZIP archive.

    In Windows Explorer, double-clicking an .exe inside a ZIP copies only the executable
    to a temporary folder in %TEMP% (e.g. Temp1_<name>.zip). In onedir mode, _internal/
    is not present next to the exe, or the path is inside a zip temp directory.
    If detected, displays a native MessageBox and exits.

    Returns:
        True if safe to proceed, False if unextracted ZIP execution was detected.
    """
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen
    if not frozen:
        return True

    exe_path = Path(sys.executable if executable is None else executable).resolve()
    exe_dir = exe_path.parent
    exe_str = str(exe_path)

    has_internal = (exe_dir / "_internal").is_dir()
    is_zip_temp = bool(re.search(r"[\\/]Temp\d+_[^\\/]+\.zip", exe_str, re.IGNORECASE))

    if has_internal and not is_zip_temp:
        return True

    # Determine message language
    if ui_lang_id is None:
        try:
            import ctypes
            ui_lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        except Exception:
            ui_lang_id = 1033

    # Primary language ID 0x22 is Ukrainian (1058 is uk-UA)
    is_ukrainian = (ui_lang_id & 0xFF) == 0x22

    if is_ukrainian:
        title = "MusicPad"
        msg = "Будь ласка, спочатку повністю розпакуйте ZIP-архів у окрему папку перед запуском MusicPad."
    else:
        title = "MusicPad"
        msg = "Please extract the ZIP archive into a folder before running MusicPad."

    if show_dialog and sys.platform == "win32":
        try:
            import ctypes
            # MB_OK (0x0) | MB_ICONERROR (0x10)
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)
        except Exception:
            pass

    if exit_on_error:
        sys.exit(1)

    return False


def get_app_root_dir(executable: Optional[str] = None, is_frozen: Optional[bool] = None) -> Path:
    """Resolve the root application directory where .cache and user data reside.

    Rules:
    1. If not frozen (running from python sources), returns the repository root
       (parent directory of the musicpad package).
    2. If frozen:
       - If exe is directly in 'dist/' (e.g. dist/MusicPad.exe): returns repo root (exe_dir.parent).
       - If exe is in 'dist/<subfolder>/' (e.g. dist/MusicPad/MusicPad.exe) AND 'MusicPad.spec'
         exists in the parent of dist: returns repo root (exe_dir.parent.parent).
       - Otherwise (standalone release extracted by user, or without MusicPad.spec):
         returns exe_dir (portable application folder).
    """
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen
    if not frozen:
        return Path(__file__).resolve().parent.parent

    exe_path = Path(sys.executable if executable is None else executable).resolve()
    exe_dir = exe_path.parent

    # Case 1: exe directly in dist/ (e.g. dist/MusicPad.exe)
    if exe_dir.name.lower() == "dist":
        return exe_dir.parent

    # Case 2: exe in dist/<subfolder>/ (e.g. dist/MusicPad/MusicPad.exe)
    if exe_dir.parent.name.lower() == "dist":
        spec_candidate = exe_dir.parent.parent / "MusicPad.spec"
        if spec_candidate.is_file():
            return exe_dir.parent.parent
        return exe_dir

    # Case 3: standard release / portable folder
    return exe_dir
