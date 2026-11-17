"""Windows Installed Apps (Settings -> Apps) registration and uninstaller for MusicPad."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from musicpad.apo_installer import (
    BACKUP_FILE,
    DATA_DIR,
    INSTALL_DIR,
    SYSTEM32_APO_DLL,
    get_driver_status,
    run_driver_helper_uac,
)
from musicpad.app_paths import detect_saved_ui_prefs
from musicpad.version import get_app_version

if sys.platform == "win32":
    import winreg
else:  # pragma: no cover - Windows-only module
    winreg = None  # type: ignore[assignment]


UNINSTALL_REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicPad"
APP_DISPLAY_NAME = "MusicPad"
APP_PUBLISHER = "MusicPad"
APP_DISPLAY_VERSION = get_app_version()

_UNINSTALL_I18N: dict[str, dict[str, str]] = {
    "uk": {
        "title": "MusicPad — Видалення",
        "heading": "Видалити MusicPad?",
        "body_app": "Програму та всі її файли буде видалено з:\n{0}",
        "body_driver": "Також буде виконано деінсталяцію системного драйвера мікрофона (MusicPad APO, знадобиться UAC) і очищення C:\\ProgramData\\MusicPad.",
        "keep_cache": "Зберегти налаштування та кеш звуків (.cache)",
        "btn_uninstall": "Видалити",
        "btn_cancel": "Скасувати",
        "err_title": "MusicPad — Помилка видалення",
        "err_driver": "Не вдалося видалити драйвер MusicPad (або запит UAC скасовано):\n{0}\n\nВидалення програми перервано, щоб ви не втратили можливість деінсталювати драйвер.",
        "err_unsafe": "Неможливо виконати автоматичне видалення з поточної папки розробки або незапакованого середовища.",
    },
    "en": {
        "title": "MusicPad — Uninstall",
        "heading": "Uninstall MusicPad?",
        "body_app": "MusicPad and all application files will be removed from:\n{0}",
        "body_driver": "The MusicPad APO microphone driver (requires UAC confirmation) and C:\\ProgramData\\MusicPad will also be removed.",
        "keep_cache": "Keep settings and audio cache (.cache)",
        "btn_uninstall": "Uninstall",
        "btn_cancel": "Cancel",
        "err_title": "MusicPad — Uninstall Error",
        "err_driver": "Failed to uninstall the MusicPad driver (or UAC was cancelled):\n{0}\n\nUninstall has been aborted so you can still manage or remove the driver.",
        "err_unsafe": "Automatic uninstallation is disabled outside a packaged MusicPad installation directory.",
    },
}


def _tr_uninstall(lang: str, key: str, *args: object) -> str:
    table = _UNINSTALL_I18N["en"] if lang == "en" else _UNINSTALL_I18N["uk"]
    text = table.get(key, _UNINSTALL_I18N["uk"].get(key, key))
    return text.format(*args) if args else text


def is_safe_uninstall_dir(install_dir: Path) -> bool:
    """Return True if *install_dir* is a valid non-root, non-source directory safe to delete."""
    try:
        resolved = install_dir.resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    # Never allow deleting filesystem roots (e.g. C:\ or /)
    if resolved.parent == resolved:
        return False
    # Protect source checkouts / repositories
    if (resolved / "MusicPad.spec").is_file() or (resolved / ".git").exists():
        return False
    return True


def estimate_dir_size_kb(install_dir: Path) -> int:
    """Estimate directory size in kilobytes (excluding .cache user media)."""
    total_bytes = 0
    stack: list[Path] = [install_dir]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() == ".cache":
                                continue
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return max(1, total_bytes // 1024)


def register_windows_uninstall(
    exe_path: Optional[Path] = None,
    is_frozen: Optional[bool] = None,
    reg_subkey: str = UNINSTALL_REG_SUBKEY,
    display_version: Optional[str] = None,
) -> bool:
    """Register MusicPad in HKCU Windows Installed Apps (no admin privileges required)."""
    if sys.platform != "win32" or winreg is None:
        return False
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen
    if not frozen:
        return False

    try:
        exe = Path(sys.executable if exe_path is None else exe_path).resolve()
    except OSError:
        return False

    install_dir = exe.parent
    if not exe.is_file() or not is_safe_uninstall_dir(install_dir):
        return False

    size_kb = estimate_dir_size_kb(install_dir)
    resolved_version = get_app_version() if display_version is None else display_version
    values_sz = {
        "DisplayName": APP_DISPLAY_NAME,
        "DisplayVersion": resolved_version,
        "Publisher": APP_PUBLISHER,
        "InstallLocation": str(install_dir),
        "DisplayIcon": f"{exe},0",
        "UninstallString": f'"{exe}" --uninstall',
        "QuietUninstallString": f'"{exe}" --uninstall --silent',
    }
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_subkey, 0, winreg.KEY_WRITE) as key:
            for name, val in values_sz.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        return True
    except OSError:
        return False


def unregister_windows_uninstall(reg_subkey: str = UNINSTALL_REG_SUBKEY) -> bool:
    """Remove MusicPad's HKCU Installed Apps registry key."""
    if sys.platform != "win32" or winreg is None:
        return False
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_subkey)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def detect_uninstall_ui_prefs(
    state_path: Optional[Path] = None,
    ui_lang_id: Optional[int] = None,
) -> tuple[str, str]:
    """Return ``(lang, theme)`` from saved state or Windows UI language."""
    return detect_saved_ui_prefs(state_path=state_path, ui_lang_id=ui_lang_id)


def is_driver_present() -> bool:
    """Return True if MusicPad APO driver registry entries or system files exist."""
    try:
        if get_driver_status().state != "not_installed":
            return True
    except Exception:
        pass
    return SYSTEM32_APO_DLL.is_file() or INSTALL_DIR.is_dir() or BACKUP_FILE.is_file()


def _find_running_app_pids(exe_path: Path, exclude_pid: Optional[int] = None) -> list[int]:
    """Return PIDs of running processes whose executable matches *exe_path*."""
    if sys.platform != "win32":
        return []

    target_resolved = str(exe_path.resolve()).lower()
    target_name = exe_path.name.lower()
    skip_pid = os.getpid() if exclude_pid is None else exclude_pid

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []

    pids: list[int] = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            pid = int(pe.th32ProcessID)
            if pid != skip_pid and pe.szExeFile.lower() == target_name:
                proc_h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if proc_h:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        size = wintypes.DWORD(len(buf))
                        if k32.QueryFullProcessImageNameW(proc_h, 0, buf, ctypes.byref(size)):
                            if buf.value.lower() == target_resolved:
                                pids.append(pid)
                    finally:
                        k32.CloseHandle(proc_h)
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return pids


def _post_close_to_pids(target_pids: set[int]) -> None:
    """Send WM_CLOSE to top-level windows owned by *target_pids*."""
    if sys.platform != "win32" or not target_pids:
        return
    u32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in target_pids:
            u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    u32.EnumWindows(EnumWindowsProc(_cb), 0)


def close_running_instances(exe_path: Path, timeout_sec: float = 2.0) -> list[int]:
    """Close other running MusicPad processes launched from *exe_path*."""
    if sys.platform != "win32":
        return []

    pids = _find_running_app_pids(exe_path)
    if not pids:
        return []

    _post_close_to_pids(set(pids))
    k32 = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001

    deadline = time.monotonic() + timeout_sec
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        still_up: list[int] = []
        for pid in remaining:
            h = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if h:
                try:
                    # WAIT_TIMEOUT = 0x102
                    if k32.WaitForSingleObject(h, 50) == 0x102:
                        still_up.append(pid)
                finally:
                    k32.CloseHandle(h)
        remaining = still_up
        if remaining:
            time.sleep(0.05)

    for pid in remaining:
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            try:
                k32.TerminateProcess(h, 0)
            finally:
                k32.CloseHandle(h)

    return pids


def _find_icon_path(install_dir: Path, theme: str) -> Optional[Path]:
    names = [f"musicpad_{theme}.ico", "musicpad.ico"]
    roots = [
        Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", None) else None,
        install_dir,
        install_dir / "_internal",
    ]
    for root in roots:
        if root is None:
            continue
        for name in names:
            for cand in (root / "assets" / name, root / name):
                if cand.is_file():
                    return cand
    return None


def confirm_uninstall_dialog(
    install_dir: Path,
    driver_installed: bool,
    lang: str,
    theme: str,
) -> tuple[bool, bool]:
    """Show uninstall confirmation dialog. Returns ``(confirmed, keep_cache)``."""
    import tkinter as tk
    from tkinter import ttk

    dark = theme == "dark"
    bg = "#1f1f1f" if dark else "#f4f6f9"
    fg = "#e8eaed" if dark else "#1b2028"
    muted = "#a7adb7" if dark else "#56606e"
    accent = "#0084d1"
    btn_bg = "#2c3038" if dark else "#e2e6ec"

    root = tk.Tk()
    root.title(_tr_uninstall(lang, "title"))
    root.configure(bg=bg)
    root.resizable(False, False)

    ico = _find_icon_path(install_dir, theme)
    if ico is not None:
        try:
            root.iconbitmap(default=str(ico))
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
            val = ctypes.c_int(1 if dark else 0)
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
            if res != 0:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), 4)
        except Exception:
            pass

    result = {"confirmed": False, "keep_cache": False}
    keep_var = tk.BooleanVar(value=False)

    frame = tk.Frame(root, bg=bg, padx=20, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=_tr_uninstall(lang, "heading"),
        bg=bg,
        fg=fg,
        font=("Segoe UI", 12, "bold"),
        anchor="w",
    ).pack(fill="x", pady=(0, 8))

    tk.Label(
        frame,
        text=_tr_uninstall(lang, "body_app", str(install_dir)),
        bg=bg,
        fg=fg,
        font=("Segoe UI", 9),
        justify="left",
        wraplength=420,
        anchor="w",
    ).pack(fill="x", pady=(0, 6))

    if driver_installed:
        tk.Label(
            frame,
            text=_tr_uninstall(lang, "body_driver"),
            bg=bg,
            fg=muted,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=420,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

    style = ttk.Style(root)
    style.configure("Uninstall.TCheckbutton", background=bg, foreground=fg, font=("Segoe UI", 9))
    style.map("Uninstall.TCheckbutton", background=[("active", bg)], foreground=[("active", fg)])
    ttk.Checkbutton(
        frame,
        text=_tr_uninstall(lang, "keep_cache"),
        variable=keep_var,
        style="Uninstall.TCheckbutton",
    ).pack(fill="x", pady=(4, 14))

    btn_row = tk.Frame(frame, bg=bg)
    btn_row.pack(fill="x")

    def _on_confirm() -> None:
        result["confirmed"] = True
        result["keep_cache"] = bool(keep_var.get())
        root.destroy()

    def _on_cancel() -> None:
        result["confirmed"] = False
        root.destroy()

    tk.Button(
        btn_row,
        text=_tr_uninstall(lang, "btn_cancel"),
        command=_on_cancel,
        bg=btn_bg,
        fg=fg,
        activebackground=btn_bg,
        activeforeground=fg,
        relief="flat",
        padx=14,
        pady=5,
        font=("Segoe UI", 9),
        cursor="hand2",
    ).pack(side="right", padx=(8, 0))

    tk.Button(
        btn_row,
        text=_tr_uninstall(lang, "btn_uninstall"),
        command=_on_confirm,
        bg=accent,
        fg="#ffffff",
        activebackground="#0072b5",
        activeforeground="#ffffff",
        relief="flat",
        padx=14,
        pady=5,
        font=("Segoe UI", 9, "bold"),
        cursor="hand2",
    ).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", _on_cancel)
    root.bind("<Escape>", lambda _e: _on_cancel())
    root.bind("<Return>", lambda _e: _on_confirm())

    root.update_idletasks()
    w = root.winfo_reqwidth()
    h = root.winfo_reqheight()
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 2)
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.focus_force()
    root.mainloop()

    return result["confirmed"], result["keep_cache"]


def _show_error_box(title: str, message: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"{title}: {message}", file=sys.stderr)


def build_cleanup_script_content(keep_cache: bool) -> str:
    """Return a UTF-8 batch script that deletes the install folder passed in ``%1``."""
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        'set "TARGET=%~1"',
        'if "%TARGET%"=="" goto :self_del',
        "set /a COUNT=0",
        ":retry",
        "set /a COUNT+=1",
        "if %COUNT% gtr 25 goto :self_del",
        "ping 127.0.0.1 -n 2 >nul",
    ]
    if keep_cache:
        lines.extend(
            [
                'for /f "delims=" %%F in (\'dir /b /a "%TARGET%" 2^>nul\') do call :del_item "%%F"',
                'set "STILL_HAS=0"',
                'for /f "delims=" %%F in (\'dir /b /a "%TARGET%" 2^>nul\') do if /i not "%%F"==".cache" set "STILL_HAS=1"',
                'if "%STILL_HAS%"=="1" goto :retry',
                "goto :self_del",
                ":del_item",
                'if /i "%~1"==".cache" goto :eof',
                'if exist "%TARGET%\\%~1\\*" (',
                '    rd /s /q "%TARGET%\\%~1" >nul 2>&1',
                ") else (",
                '    del /f /q /a "%TARGET%\\%~1" >nul 2>&1',
                ")",
                "goto :eof",
            ]
        )
    else:
        lines.extend(
            [
                'rd /s /q "%TARGET%" >nul 2>&1',
                'if exist "%TARGET%" goto :retry',
                "goto :self_del",
            ]
        )
    lines.extend(
        [
            ":self_del",
            '(goto) 2>nul & del /f /q "%~f0" >nul 2>&1',
        ]
    )
    return "\r\n".join(lines) + "\r\n"


def spawn_cleanup_script(install_dir: Path, keep_cache: bool, wait_pid: Optional[int] = None) -> Optional[Path]:
    """Spawn a detached hidden cmd.exe script to remove *install_dir* after process exit."""
    if sys.platform != "win32" or not is_safe_uninstall_dir(install_dir):
        return None

    pid_tag = os.getpid() if wait_pid is None else wait_pid
    script_path = Path(tempfile.gettempdir()) / f"musicpad_uninstall_{pid_tag}.cmd"
    script_path.write_text(build_cleanup_script_content(keep_cache=keep_cache), encoding="utf-8")

    creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(script_path), str(install_dir.resolve())],
        cwd=tempfile.gettempdir(),
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return script_path


def run_windows_uninstall(
    silent: bool = False,
    exe_path: Optional[Path] = None,
    is_frozen: Optional[bool] = None,
    driver_present_fn: Optional[Callable[[], bool]] = None,
    run_driver_uac_fn: Optional[Callable[[str], Any]] = None,
    confirm_fn: Optional[Callable[[Path, bool, str, str], tuple[bool, bool]]] = None,
    close_instances_fn: Optional[Callable[[Path], list[int]]] = None,
    unregister_fn: Optional[Callable[[], bool]] = None,
    spawn_cleanup_fn: Optional[Callable[[Path, bool], Any]] = None,
    program_data_dir: Optional[Path] = None,
) -> int:
    """Execute `--uninstall` flow: confirm, close app, remove APO driver, unregister HKCU, delete folder."""
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen
    exe = Path(sys.executable if exe_path is None else exe_path).resolve()
    install_dir = exe.parent
    lang, theme = detect_uninstall_ui_prefs()

    if not frozen or not is_safe_uninstall_dir(install_dir):
        if not silent:
            _show_error_box(_tr_uninstall(lang, "err_title"), _tr_uninstall(lang, "err_unsafe"))
        return 1

    drv_present = (driver_present_fn or is_driver_present)()

    if silent:
        confirmed = True
        keep_cache = False
    else:
        confirmed, keep_cache = (confirm_fn or confirm_uninstall_dialog)(
            install_dir,
            drv_present,
            lang,
            theme,
        )

    if not confirmed:
        return 0

    (close_instances_fn or close_running_instances)(exe)

    if drv_present:
        res = (run_driver_uac_fn or run_driver_helper_uac)("uninstall")
        ok = bool(res[0]) if isinstance(res, tuple) else bool(res)
        msg = str(res[-1]) if isinstance(res, tuple) and len(res) > 1 else ""
        if not ok:
            if not silent:
                _show_error_box(_tr_uninstall(lang, "err_title"), _tr_uninstall(lang, "err_driver", msg))
            return 1

    data_dir = DATA_DIR if program_data_dir is None else program_data_dir
    if data_dir.is_dir():
        shutil.rmtree(data_dir, ignore_errors=True)

    (unregister_fn or unregister_windows_uninstall)()
    (spawn_cleanup_fn or spawn_cleanup_script)(install_dir, keep_cache)
    return 0
