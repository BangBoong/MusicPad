"""Single-instance Win32 guard and window-restore signaling for MusicPad."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from musicpad.app_paths import detect_saved_ui_prefs

SINGLE_INSTANCE_MUTEX_NAME = r"Local\MusicPadSingleInstance"
SINGLE_INSTANCE_WAKEUP_EVENT_NAME = r"Local\MusicPadShowWindowEvent"

_ALREADY_RUNNING_I18N: dict[str, str] = {
    "uk": "MusicPad вже запущено (перевірте активне вікно або системний трей).",
    "en": "MusicPad is already running (check the active window or system tray).",
}

_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0x00000000


def _kernel32() -> Any:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.SetEvent.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def get_already_running_message(
    state_path: Optional[Path] = None,
    ui_lang_id: Optional[int] = None,
) -> str:
    """Return the localized already-running message using saved state or Windows UI language."""
    lang, _ = detect_saved_ui_prefs(state_path=state_path, ui_lang_id=ui_lang_id)
    return _ALREADY_RUNNING_I18N["en"] if lang == "en" else _ALREADY_RUNNING_I18N["uk"]


def notify_already_running(
    state_path: Optional[Path] = None,
    ui_lang_id: Optional[int] = None,
    show_dialog: bool = True,
) -> str:
    """Show the localized already-running info dialog and return the message text."""
    msg = get_already_running_message(state_path=state_path, ui_lang_id=ui_lang_id)
    if show_dialog:
        if sys.platform == "win32":
            try:
                # MB_OK (0x0) | MB_ICONINFORMATION (0x40) | MB_SETFOREGROUND (0x10000) | MB_TOPMOST (0x40000)
                ctypes.windll.user32.MessageBoxW(None, msg, "MusicPad", 0x00050040)
                return msg
            except Exception:
                pass
        print(msg, file=sys.stderr)
    return msg


class SingleInstanceGuard:
    """Per-user Win32 mutex lock with named-event wakeup to restore the active window."""

    def __init__(
        self,
        mutex_name: str = SINGLE_INSTANCE_MUTEX_NAME,
        event_name: str = SINGLE_INSTANCE_WAKEUP_EVENT_NAME,
    ) -> None:
        self.mutex_name = mutex_name
        self.event_name = event_name
        self._mutex_handle: Optional[int] = None
        self._event_handle: Optional[int] = None
        self._stop_event = threading.Event()
        self._listener_thread: Optional[threading.Thread] = None

    def acquire(self) -> bool:
        """Acquire the primary instance lock. Returns False if another instance holds it."""
        if sys.platform != "win32":
            return True
        if self._mutex_handle is not None:
            return True

        k32 = _kernel32()
        handle = k32.CreateMutexW(None, True, self.mutex_name)
        err = ctypes.get_last_error()
        if not handle or err in (_ERROR_ALREADY_EXISTS, _ERROR_ACCESS_DENIED):
            if handle:
                k32.CloseHandle(handle)
            return False

        self._mutex_handle = int(handle)
        ev_handle = k32.CreateEventW(None, False, False, self.event_name)
        if ev_handle:
            self._event_handle = int(ev_handle)
        return True

    def signal_existing_instance(self) -> bool:
        """Signal the primary instance's wakeup event to restore/focus its window."""
        if sys.platform != "win32":
            return False
        k32 = _kernel32()
        ev = k32.OpenEventW(_EVENT_MODIFY_STATE, False, self.event_name)
        if not ev:
            return False
        try:
            return bool(k32.SetEvent(ev))
        finally:
            k32.CloseHandle(ev)

    def start_wakeup_listener(self, on_wakeup: Callable[[], None]) -> None:
        """Listen on a background daemon thread for secondary-instance wakeup signals."""
        if sys.platform != "win32" or self._event_handle is None:
            return
        if self._listener_thread is not None and self._listener_thread.is_alive():
            return

        self._stop_event.clear()
        ev_handle = self._event_handle
        k32 = _kernel32()

        def _worker() -> None:
            while not self._stop_event.is_set():
                wait_res = k32.WaitForSingleObject(ev_handle, 500)
                if self._stop_event.is_set():
                    break
                if wait_res == _WAIT_OBJECT_0:
                    try:
                        on_wakeup()
                    except Exception:
                        pass

        self._listener_thread = threading.Thread(
            target=_worker,
            name="musicpad-single-instance-wakeup",
            daemon=True,
        )
        self._listener_thread.start()

    def release(self) -> None:
        """Stop the wakeup listener and release kernel handles."""
        self._stop_event.set()
        if sys.platform == "win32":
            k32 = _kernel32()
            if self._event_handle is not None:
                try:
                    k32.SetEvent(self._event_handle)
                except Exception:
                    pass
            if self._listener_thread is not None and self._listener_thread.is_alive():
                self._listener_thread.join(timeout=1.0)
            self._listener_thread = None
            if self._event_handle is not None:
                try:
                    k32.CloseHandle(self._event_handle)
                except Exception:
                    pass
                self._event_handle = None
            if self._mutex_handle is not None:
                try:
                    k32.CloseHandle(self._mutex_handle)
                except Exception:
                    pass
                self._mutex_handle = None
        else:
            self._listener_thread = None
