"""MusicPad desktop application with authentic Soundpad design."""

from __future__ import annotations

import base64
import ctypes
import io
import json
import math
import os
import queue
import random
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Optional

try:
    from PIL import Image, ImageDraw, ImageTk
except ModuleNotFoundError:
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

try:
    import pystray
except ModuleNotFoundError:
    pystray = None  # type: ignore[assignment]

from musicpad.audio_engine import AudioEngine, AudioEngineError, StreamTrack
from musicpad.library import LocalTrack, SUPPORTED_EXTENSIONS, format_duration, scan_file, scan_folder


_VK_MAP: dict[str, int] = {
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    **{f"NUM{i}": 0x60 + i for i in range(10)},
    **{str(i): 0x30 + i for i in range(10)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    "SPACE": 0x20,
    "TAB": 0x09,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PRIOR": 0x21,
    "NEXT": 0x22,
    "ADD": 0x6B,
    "SUBTRACT": 0x6D,
    "MULTIPLY": 0x6A,
    "DIVIDE": 0x6F,
}

_MOD_VK = {
    "CTRL": 0x11,
    "ALT": 0x12,
    "SHIFT": 0x10,
}

_VK_TO_NAME: dict[int, str] = {vk: name for name, vk in _VK_MAP.items()}

DEFAULT_TRACK_SOURCE = "Вбудований аудіоплеєр (Локальні файли + YouTube Music)"


def _format_tk_hotkey(event: tk.Event) -> Optional[str]:
    keysym = str(getattr(event, "keysym", "")).upper()
    keycode = getattr(event, "keycode", None)
    if keysym in ("CONTROL_L", "CONTROL_R", "ALT_L", "ALT_R", "SHIFT_L", "SHIFT_R", "CAPS_LOCK"):
        return None
    if isinstance(keycode, int) and keycode in (0x10, 0x11, 0x12, 0x14, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
        return None
    if keysym == "ESCAPE" or keycode == 0x1B:
        return "ESCAPE"

    mods: list[str] = []
    state = int(getattr(event, "state", 0))
    if state & 0x0004:
        mods.append("Ctrl")
    if state & 0x0008 or state & 0x20000:
        mods.append("Alt")
    if state & 0x0001:
        mods.append("Shift")

    main_key: Optional[str] = None
    if isinstance(keycode, int) and 0x60 <= keycode <= 0x69:
        main_key = f"Num{keycode - 0x60}"
    elif keysym.startswith("KP_") and len(keysym) == 4 and keysym[3].isdigit():
        main_key = f"Num{keysym[3]}"
    elif keysym in _VK_MAP:
        main_key = keysym if len(keysym) <= 2 or keysym.startswith("F") else keysym.capitalize()
    elif isinstance(keycode, int) and keycode in _VK_TO_NAME:
        mapped = _VK_TO_NAME[keycode]
        if mapped.startswith("NUM") and len(mapped) == 4:
            main_key = f"Num{mapped[3]}"
        else:
            main_key = mapped if len(mapped) <= 2 or mapped.startswith("F") else mapped.capitalize()

    if not main_key:
        return None

    return "+".join(mods + [main_key])


def _is_hotkey_pressed_win32(hotkey: str) -> bool:
    if sys.platform != "win32" or not hotkey:
        return False
    u32 = ctypes.windll.user32
    parts = [p.strip().upper() for p in hotkey.split("+") if p.strip()]
    if not parts:
        return False
    main_part = parts[-1]
    vk = _VK_MAP.get(main_part)
    if vk is None:
        return False
    if not (u32.GetAsyncKeyState(vk) & 0x8000):
        return False
    req_mods = set(parts[:-1])
    for mod_name, mod_vk in _MOD_VK.items():
        is_down = bool(u32.GetAsyncKeyState(mod_vk) & 0x8000)
        if mod_name in req_mods and not is_down:
            return False
        if mod_name not in req_mods and is_down:
            return False
    return True


def _create_wav_file(path: Path, duration_sec: float = 1.0, rate: int = 44100) -> None:
    frames = max(1, int(rate * duration_sec))
    data = b"\x00\x00" * frames * 2
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 2, rate, rate * 4, 4, 16)
    d = struct.pack("<4sI", b"data", len(data)) + data
    riff = struct.pack("<4sI4s", b"RIFF", 4 + len(fmt) + len(d), b"WAVE")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(riff + fmt + d)


I18N: dict[str, dict[str, str]] = {
    "uk": {
        "menu_file": "Файл",
        "menu_edit": "Редагування",
        "menu_play": "Відтворення",
        "menu_win": "Вікно",
        # File menu
        "file_add": "Додати звукові файли",
        "file_new": "Новий список звуків",
        "file_load": "Завантажити список звуків",
        "file_save": "Зберегти список звуків",
        "file_save_as": "Зберегти список звуків як...",
        "file_load_recent": "Завантажити останній список звуків",
        "file_last_autosave": "Останнє автозбереження",
        "file_empty": "(Порожньо)",
        "file_restore": "Відновити список звуків",
        "file_restore_backup": "Відновити з резервної копії",
        "file_restore_default": "Відновити список за замовчуванням",
        "file_export": "Експорт",
        "file_export_json": "Експортувати список як JSON...",
        "file_export_m3u": "Експортувати плейлист M3U...",
        "file_export_txt": "Експортувати список як текст (TXT)...",
        "file_stats": "Статистика",
        "file_stats_overall": "Загальна статистика",
        "file_stats_reset": "Скинути лічильник відтворень",
        "file_settings": "Налаштування",
        "file_exit": "Вихід",
        # Edit menu
        "edit_undo": "Скасувати",
        "edit_redo": "Відновити",
        "edit_cut": "Вирізати",
        "edit_copy": "Копіювати",
        "edit_paste": "Вставити",
        "edit_delete": "Видалити вибрані записи",
        "edit_shuffle": "Перемішати список",
        "edit_select_all": "Вибрати все",
        "edit_select_last": "Вибрати останній файл",
        "edit_last_in_list": "Останній у списку",
        "edit_last_added": "Останній доданий файл",
        "edit_last_played": "Останній відтворений файл",
        "edit_find_missing": "Знайти відсутні аудіофайли",
        "edit_search": "Пошук",
        # Play menu
        "play_speakers_and_mic": "Відтворити вибраний файл (динаміки та мікрофон)",
        "play_headphones_only": "Відтворювати вибраний файл лише у динаміках",
        "play_mic_only": "Відтворювати вибраний файл лише у мікрофоні",
        "play_random_all": "Відтворити випадковий файл (динаміки та мікрофон)",
        "play_random_spk": "Відтворити випадковий файл тільки у динаміках",
        "play_random_mic": "Відтворити випадковий файл тільки у мікрофоні",
        "play_random_cat": "Відтворити випадковий файл із усіх категорій",
        "play_cat_both": "Динаміки та мікрофон",
        "play_cat_spk": "Тільки у динаміках",
        "play_cat_mic": "Тільки у мікрофоні",
        "play_last_menu": "Відтворити останній файл (динаміки та мікрофон)",
        "play_repeat_last": "Повторити останній відтворений",
        "play_play_last_added": "Відтворити останній доданий",
        "play_stop": "Зупинити відтворення",
        "play_pause": "Призупинити/продовжити відтворення",
        "play_prev": "Відтворити попередній файл",
        "play_next": "Відтворити наступний файл",
        "play_auto_continue": "Продовжити відтворення після закінчення поточного файлу",
        "play_loop_playlist": "Повторяти відтворення списку звуків",
        "play_loop_current": "Повторне відтворення поточного файлу",
        "play_auto_stop": "Автозупинка (рекомендовано)",
        "play_mode": "Режим відтворення",
        "play_mode_single": "Один звук (стандартний режим)",
        "play_mode_overlap": "Одночасне накладання звуків",
        "play_mode_interrupt": "Переривати поточний звук новим",
        # Window menu
        "win_cat": "Категорії",
        "win_my_sounds": "Мої звуки",
        "win_yt": "YouTube / YouTube Music",
        "win_toggle_cat": "Перемкнути видимість категорій",
        "win_quick": "Швидка панель",
        "win_search_panel": "Панель пошуку звуків",
        "win_quick_yt": "Швидкий пошук YouTube...",
        "win_hotkeys": "Гарячі клавіші",
        "win_hk_enable": "Увімкнути гарячі клавіші",
        "win_hk_disable": "Вимкнути гарячі клавіші",
        "win_hk_assign": "Призначити клавішу вибраному...",
        "win_hk_list": "Список активних комбінацій...",
        "win_record": "Записування звуку",
        "win_color": "Інструмент «Колір»",
        "win_tts": "Озвучення тексту",
        # Main table headings
        "col_index": "Інд...",
        "col_title": "Тег",
        "col_duration": "Тр...",
        "col_hotkey": "Сполучення ...",
        # Info panel
        "cat_my_sounds": "Мої звуки",
        "cat_yt": "YouTube / YouTube Music",
        "info_sounds": "Звуки: ",
        "info_plays": "Лічильник відтворень: ",
        "info_selected": "Вибрано: ",
        # Status / play button
        "btn_play": "▶ Відтворити",
        "btn_pause": "⏸ Пауза",
        "btn_resume": "▶ Продовжити",
        "nothing_playing": "Нічого не відтворюється",
        "now_playing": "Зараз грає: {0}",
        "playing": "Відтворюється: {0}",
        # Settings dialog
        "settings_title": "Налаштування",
        "tab_audio": "Аудіо",
        "tab_hotkeys": "Гарячі клавіші",
        "tab_interface": "Інтерфейс",
        "tab_devices": "Пристрої",
        # Settings - Audio tab
        "grp_audio_norm": "Нормалізація гучності",
        "lbl_audio_norm_desc": "Налаштовує гучність відтворюваного аудиофайлу так, щоб інші люди чули ваш голос і ваші звуки з однаковою гучністю.",
        "lbl_audio_voice_vol": "Гучність голосу: ",
        "lbl_audio_dynamic": "Динамічне регулювання гучності (рекомендоване)",
        "lbl_audio_fixed": "Фіксоване регулювання гучності (додатково)",
        "hint_dynamic_title": "Динамічне регулювання",
        "hint_dynamic_desc": "Динамічне регулювання аналізує амплітуду аудіо та автоматично вирівнює гучність до обраного рівня.",
        "hint_fixed_title": "Фіксоване регулювання",
        "hint_fixed_desc": "Фіксоване регулювання застосовує постійне статичне підсилення або послаблення аудіо.",
        # Settings - Interface tab
        "grp_general": "Загальне",
        "lbl_language": "Мова: ",
        "grp_toolbar": "Панель інструментів",
        "lbl_style": "Стиль: ",
        "style_dark": "Темний",
        "style_light": "Світлий",
        "grp_misc": "Різне",
        "chk_remember_playback": "Запам'ятовувати стан відтворення при виході з програми",
        # Settings - Buttons
        "btn_reset": "Скинути",
        "btn_ok": "OK",
        "btn_cancel": "Скасувати",
        "btn_apply": "Застосувати",
        # Status bar notifications
        "settings_saved": "Налаштування збережено та застосовано.",
        "settings_reset": "Параметри скинуто до значень за замовчуванням.",
    },
    "en": {
        "menu_file": "File",
        "menu_edit": "Edit",
        "menu_play": "Playback",
        "menu_win": "Window",
        # File menu
        "file_add": "Add sound files",
        "file_new": "New sound list",
        "file_load": "Load sound list",
        "file_save": "Save sound list",
        "file_save_as": "Save sound list as...",
        "file_load_recent": "Load recent sound list",
        "file_last_autosave": "Last auto-save",
        "file_empty": "(Empty)",
        "file_restore": "Restore sound list",
        "file_restore_backup": "Restore from backup",
        "file_restore_default": "Restore default list",
        "file_export": "Export",
        "file_export_json": "Export list as JSON...",
        "file_export_m3u": "Export M3U playlist...",
        "file_export_txt": "Export list as text (TXT)...",
        "file_stats": "Statistics",
        "file_stats_overall": "Overall statistics",
        "file_stats_reset": "Reset playback counter",
        "file_settings": "Settings",
        "file_exit": "Exit",
        # Edit menu
        "edit_undo": "Undo",
        "edit_redo": "Redo",
        "edit_cut": "Cut",
        "edit_copy": "Copy",
        "edit_paste": "Paste",
        "edit_delete": "Delete selected entries",
        "edit_shuffle": "Shuffle list",
        "edit_select_all": "Select all",
        "edit_select_last": "Select last file",
        "edit_last_in_list": "Last in list",
        "edit_last_added": "Last added file",
        "edit_last_played": "Last played file",
        "edit_find_missing": "Find missing audio files",
        "edit_search": "Search",
        # Play menu
        "play_speakers_and_mic": "Play selected file (speakers and mic)",
        "play_headphones_only": "Play selected file in speakers only",
        "play_mic_only": "Play selected file in microphone only",
        "play_random_all": "Play random file (speakers and mic)",
        "play_random_spk": "Play random file in speakers only",
        "play_random_mic": "Play random file in microphone only",
        "play_random_cat": "Play random file from all categories",
        "play_cat_both": "Speakers and mic",
        "play_cat_spk": "In speakers only",
        "play_cat_mic": "In microphone only",
        "play_last_menu": "Play last file (speakers and mic)",
        "play_repeat_last": "Replay last played",
        "play_play_last_added": "Play last added",
        "play_stop": "Stop playback",
        "play_pause": "Pause/resume playback",
        "play_prev": "Play previous file",
        "play_next": "Play next file",
        "play_auto_continue": "Continue playback after current file ends",
        "play_loop_playlist": "Loop sound list playback",
        "play_loop_current": "Loop current file",
        "play_auto_stop": "Auto-stop (recommended)",
        "play_mode": "Playback mode",
        "play_mode_single": "Single sound (standard mode)",
        "play_mode_overlap": "Simultaneous sound overlap",
        "play_mode_interrupt": "Interrupt current sound with new",
        # Window menu
        "win_cat": "Categories",
        "win_my_sounds": "My Sounds",
        "win_yt": "YouTube / YouTube Music",
        "win_toggle_cat": "Toggle categories visibility",
        "win_quick": "Quick panel",
        "win_search_panel": "Sound search panel",
        "win_quick_yt": "Quick YouTube search...",
        "win_hotkeys": "Hotkeys",
        "win_hk_enable": "Enable hotkeys",
        "win_hk_disable": "Disable hotkeys",
        "win_hk_assign": "Assign hotkey to selected...",
        "win_hk_list": "List of active shortcuts...",
        "win_record": "Record audio",
        "win_color": "Color tool",
        "win_tts": "Text to speech",
        # Main table headings
        "col_index": "Ind...",
        "col_title": "Title",
        "col_duration": "Dur...",
        "col_hotkey": "Hot key...",
        # Info panel
        "cat_my_sounds": "My Sounds",
        "cat_yt": "YouTube / YouTube Music",
        "info_sounds": "Sounds: ",
        "info_plays": "Playback count: ",
        "info_selected": "Selected: ",
        # Status / play button
        "btn_play": "▶ Play",
        "btn_pause": "⏸ Pause",
        "btn_resume": "▶ Resume",
        "nothing_playing": "Nothing is playing",
        "now_playing": "Now playing: {0}",
        "playing": "Playing: {0}",
        # Settings dialog
        "settings_title": "Settings",
        "tab_audio": "Audio",
        "tab_hotkeys": "Hotkeys",
        "tab_interface": "Interface",
        "tab_devices": "Devices",
        # Settings - Audio tab
        "grp_audio_norm": "Volume normalization",
        "lbl_audio_norm_desc": "Adjusts the volume of the played audio file so that other people hear your voice and your sounds at the same volume.",
        "lbl_audio_voice_vol": "Voice volume: ",
        "lbl_audio_dynamic": "Dynamic volume adjustment (recommended)",
        "lbl_audio_fixed": "Fixed volume adjustment (additional)",
        "hint_dynamic_title": "Dynamic adjustment",
        "hint_dynamic_desc": "Dynamic adjustment analyzes audio amplitude and automatically normalizes volume to the selected level.",
        "hint_fixed_title": "Fixed adjustment",
        "hint_fixed_desc": "Fixed adjustment applies a constant static amplification or attenuation of audio.",
        # Settings - Interface tab
        "grp_general": "General",
        "lbl_language": "Language: ",
        "grp_toolbar": "Toolbar",
        "lbl_style": "Style: ",
        "style_dark": "Dark",
        "style_light": "Light",
        "grp_misc": "Miscellaneous",
        "chk_remember_playback": "Remember playback state on exit",
        # Settings - Buttons
        "btn_reset": "Reset",
        "btn_ok": "OK",
        "btn_cancel": "Cancel",
        "btn_apply": "Apply",
        # Status bar notifications
        "settings_saved": "Settings saved and applied.",
        "settings_reset": "Settings reset to defaults.",
    },
}


class SoundpadTimelineSlider(tk.Canvas):
    """Custom sleek timeline seek slider authentic to Soundpad design."""

    def __init__(
        self,
        master: Any = None,
        variable: Optional[tk.DoubleVar] = None,
        to: float = 100.0,
        **kw: Any,
    ) -> None:
        kw.setdefault("height", 24)
        kw.setdefault("bg", "#212121")
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("cursor", "hand2")
        super().__init__(master, **kw)
        self.theme = "dark"
        self.to = float(to)
        self.variable = variable
        self._on_press_cb: Any = None
        self._on_motion_cb: Any = None
        self._on_release_cb: Any = None
        self._drag = False

        super().bind("<Configure>", lambda _: self._redraw())
        super().bind("<ButtonPress-1>", self._on_press)
        super().bind("<B1-Motion>", self._on_motion)
        super().bind("<ButtonRelease-1>", self._on_release)

        if self.variable is not None:
            self.variable.trace_add("write", lambda *_: self._redraw())

    def is_disabled(self) -> bool:
        try:
            return str(self.cget("state")) == "disabled"
        except Exception:
            return False

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self._redraw()

    def configure(self, cnf: Any = None, **kw: Any) -> Any:
        if "to" in kw:
            self.to = float(kw.pop("to"))
            self._redraw()
        if "variable" in kw:
            self.variable = kw.pop("variable")
            if self.variable is not None:
                self.variable.trace_add("write", lambda *_: self._redraw())
            self._redraw()
        res = super().configure(cnf, **kw) if (kw or cnf) else None
        if "state" in kw:
            try:
                super().configure(cursor="" if self.is_disabled() else "hand2")
            except Exception:
                pass
            self._redraw()
        return res

    def bind(self, sequence: Optional[str] = None, func: Any = None, add: Optional[bool] = None) -> Any:
        if sequence in ("<ButtonPress-1>", "<Button-1>"):
            self._on_press_cb = func
            return ""
        if sequence == "<B1-Motion>":
            self._on_motion_cb = func
            return ""
        if sequence in ("<ButtonRelease-1>", "<ButtonRelease>"):
            self._on_release_cb = func
            return ""
        return super().bind(sequence, func, add)

    def _val_to_x(self, val: float) -> int:
        w = self.winfo_width()
        if w <= 10 or self.to <= 0:
            return 2
        ratio = max(0.0, min(1.0, val / self.to))
        return int(2 + ratio * (w - 4))

    def _x_to_val(self, x: int) -> float:
        w = self.winfo_width()
        if w <= 10 or self.to <= 0:
            return 0.0
        ratio = max(0.0, min(1.0, (x - 2) / (w - 4)))
        return ratio * self.to

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w < 10:
            return
        dark = getattr(self, "theme", "dark") == "dark"
        disabled = self.is_disabled()
        if disabled:
            trough_fill = "#1a1a1a" if dark else "#e0e3e8"
            accent = "#4f555e" if dark else "#a8b0bc"
        else:
            trough_fill = "#141414" if dark else "#d0d4da"
            accent = "#0084d1" if dark else "#007acc"
        ty1, ty2 = 8, 16
        val = float(self.variable.get()) if self.variable else 0.0
        val = max(0.0, min(self.to, val)) if self.to > 0 else 0.0
        x_curr = self._val_to_x(val)
        # Background trough with 1px outline
        self.create_rectangle(2, ty1, w - 2, ty2, outline=accent, fill=trough_fill, width=1)
        if x_curr > 2:
            self.create_rectangle(2, ty1 + 1, min(x_curr, w - 2), ty2 - 1, outline="", fill=accent)
        # Handle indicator
        hx = min(max(2, x_curr), w - 2)
        self.create_rectangle(hx - 1, 6, hx + 1, 18, outline=accent, fill=accent)

    def _on_press(self, event: tk.Event) -> None:
        if self.is_disabled():
            return
        self._drag = True
        val = self._x_to_val(event.x)
        if self.variable:
            self.variable.set(val)
        self._redraw()
        if self._on_press_cb:
            self._on_press_cb(event)

    def _on_motion(self, event: tk.Event) -> None:
        if not self._drag or self.is_disabled():
            return
        val = self._x_to_val(event.x)
        if self.variable:
            self.variable.set(val)
        self._redraw()
        if getattr(self, "_on_motion_cb", None):
            self._on_motion_cb(event)

    def _on_release(self, event: tk.Event) -> None:
        if not self._drag or self.is_disabled():
            return
        self._drag = False
        val = self._x_to_val(event.x)
        if self.variable:
            self.variable.set(val)
        self._redraw()
        if self._on_release_cb:
            self._on_release_cb(event)


class SoundpadVolumeSlider(tk.Canvas):
    """Custom volume slider authentic to Soundpad design."""

    def __init__(
        self,
        master: Any = None,
        variable: Optional[tk.DoubleVar] = None,
        command: Any = None,
        width: int = 86,
        **kw: Any,
    ) -> None:
        kw.setdefault("height", 24)
        kw.setdefault("width", width)
        kw.setdefault("bg", "#212121")
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, **kw)
        self.theme = "dark"
        self.variable = variable
        self.command = command
        self._drag = False

        self.bind("<Configure>", lambda _: self._redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", lambda _: setattr(self, "_drag", False))

        if self.variable is not None:
            self.variable.trace_add("write", lambda *_: self._redraw())

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self._redraw()

    def _val_to_x(self, val: float) -> int:
        w = self.winfo_width()
        if w <= 16:
            return 8
        ratio = max(0.0, min(1.0, val))
        return int(4 + ratio * (w - 15))

    def _x_to_val(self, x: int) -> float:
        w = self.winfo_width()
        if w <= 16:
            return 0.0
        ratio = max(0.0, min(1.0, (x - 4) / (w - 15)))
        return ratio

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w < 16:
            return
        dark = getattr(self, "theme", "dark") == "dark"
        trough_fill = "#141414" if dark else "#d0d4da"
        accent = "#0084d1" if dark else "#007acc"
        ty1, ty2 = 9, 15
        val = float(self.variable.get()) if self.variable else 1.0
        x_knob = self._val_to_x(val)
        # Trough
        self.create_rectangle(4, ty1, w - 8, ty2, outline=accent, fill=trough_fill, width=1)
        if x_knob > 4:
            self.create_rectangle(4, ty1 + 1, min(x_knob, w - 8), ty2 - 1, outline="", fill=accent)
        # Solid thumb knob (w: 7, h: 15)
        kw_half = 3
        kx = min(max(4, x_knob), w - 8)
        self.create_rectangle(kx - kw_half, 5, kx + kw_half + 1, 20, outline=accent, fill=accent)

    def _on_press(self, event: tk.Event) -> None:
        self._drag = True
        val = self._x_to_val(event.x)
        if self.variable:
            self.variable.set(val)
        self._redraw()
        if self.command:
            self.command(str(val))

    def _on_motion(self, event: tk.Event) -> None:
        if self._drag:
            val = self._x_to_val(event.x)
            if self.variable:
                self.variable.set(val)
            self._redraw()
            if self.command:
                self.command(str(val))


class SoundpadButton(tk.Button):
    """Button supporting both dynamic text changes (e.g. from tests/playback) and icons."""

    def __init__(
        self,
        master: Any = None,
        icon_play: Any = None,
        icon_pause: Any = None,
        **kw: Any,
    ) -> None:
        self.icon_play = icon_play
        self.icon_pause = icon_pause
        text = kw.get("text")
        if text is not None and "image" not in kw:
            img = self._get_icon_for_text(text)
            if img is not None:
                kw["image"] = img
        super().__init__(master, **kw)

    def _get_icon_for_text(self, text: Any) -> Any:
        if text is None:
            return None
        s = str(text).strip().lower()
        if "⏸" in s or "пауз" in s or "pause" in s:
            return getattr(self, "icon_pause", None)
        if "▶" in s or "відтворити" in s or "продовжити" in s or "play" in s or "resume" in s:
            return getattr(self, "icon_play", None)
        return None

    def configure(self, cnf: Any = None, **kw: Any) -> Any:  # type: ignore[override]
        text = kw.get("text")
        if text is None and isinstance(cnf, dict):
            text = cnf.get("text")
        if text is not None:
            img = self._get_icon_for_text(text)
            if img is not None:
                if isinstance(cnf, dict) and "text" in cnf:
                    cnf["image"] = img
                else:
                    kw["image"] = img
        return super().configure(cnf, **kw)

    config = configure


def _calculate_bubble_polygon(
    w: int,
    h: int,
    r: int,
    tail_tip_x: int,
    tail_w: int,
    tail_h: int,
) -> list[tuple[float, float]]:
    """Generate polygon points for a rounded rectangular speech bubble with a bottom tail."""
    pts: list[tuple[float, float]] = []
    x1, y1 = 2.0, 2.0
    x2 = float(w - 2)
    y2 = float(h - tail_h - 2)
    tail_tip_y = float(h - 2)
    tail_left = float(tail_tip_x - tail_w // 2)
    tail_right = float(tail_tip_x + tail_w // 2)

    # 1. Top edge and top-right corner arc (-90 to 0 deg)
    cx, cy = x2 - r, y1 + r
    for deg in range(-90, 1, 15):
        rad = math.radians(deg)
        pts.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))

    # 2. Right edge and bottom-right corner arc (0 to 90 deg)
    cx, cy = x2 - r, y2 - r
    for deg in range(0, 91, 15):
        rad = math.radians(deg)
        px = cx + r * math.cos(rad)
        py = cy + r * math.sin(rad)
        if px >= tail_right:
            pts.append((px, py))

    # 3. Bottom edge to tail right
    pts.append((tail_right, y2))
    # 4. Tail tip pointing down
    pts.append((float(tail_tip_x), tail_tip_y))
    # 5. Tail left
    pts.append((tail_left, y2))

    # 6. Bottom edge to bottom-left corner arc (90 to 180 deg)
    cx, cy = x1 + r, y2 - r
    for deg in range(90, 181, 15):
        rad = math.radians(deg)
        px = cx + r * math.cos(rad)
        py = cy + r * math.sin(rad)
        if px <= tail_left:
            pts.append((px, py))

    # 7. Left edge and top-left corner arc (180 to 270 deg)
    cx, cy = x1 + r, y1 + r
    for deg in range(180, 271, 15):
        rad = math.radians(deg)
        pts.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))

    return pts


class SpeechBubble(tk.Toplevel):
    """Custom speech bubble popup widget with a corner tail pointing to the status bar message icon."""

    WIDTH = 320
    HEIGHT = 86
    TAIL_HEIGHT = 12
    TAIL_WIDTH = 12
    CORNER_RADIUS = 8

    def __init__(
        self,
        master: tk.Misc,
        target_widget: Optional[tk.Widget] = None,
        theme: str = "dark",
    ) -> None:
        super().__init__(master)
        self.master = master
        self.target_widget = target_widget
        self.theme = theme
        self.tail_tip_x = self.WIDTH - 20

        self._fading_out = False
        self._fade_job: Optional[str] = None
        self._auto_close_job: Optional[str] = None
        self.current_message: str = ""
        self.current_category: str = "info"

        # Toplevel configuration: borderless and stay on top
        self.overrideredirect(True)
        try:
            self.transient(master)
        except Exception:
            pass
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        # Windows chroma key transparency
        self._chroma_key = "#010101"
        self._transparent_supported = True
        try:
            self.wm_attributes("-transparentcolor", self._chroma_key)
        except Exception:
            self._transparent_supported = False
            self._chroma_key = "#1c1c1c" if self.theme == "dark" else "#e0e3e8"

        self.configure(bg=self._chroma_key)

        # Vector canvas for drawing speech bubble polygon
        self.canvas = tk.Canvas(
            self,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=self._chroma_key,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Content frame embedded within bubble body
        self.content_frame = tk.Frame(self.canvas)
        body_h = self.HEIGHT - self.TAIL_HEIGHT - 4
        self._window_id = self.canvas.create_window(
            12,
            6,
            anchor="nw",
            window=self.content_frame,
            width=self.WIDTH - 24,
            height=body_h - 6,
        )

        # Category icon
        self.icon_label = tk.Label(
            self.content_frame,
            text="ℹ",
            font=("Segoe UI", 13, "bold"),
            anchor="center",
        )
        self.icon_label.pack(side="left", padx=(2, 8), anchor="center")

        # Close button (×)
        self.close_btn = tk.Label(
            self.content_frame,
            text="×",
            font=("Segoe UI", 11, "bold"),
            anchor="ne",
            cursor="hand2",
        )
        self.close_btn.pack(side="right", padx=(2, 2), pady=(0, 0), anchor="ne")
        self.close_btn.bind("<Enter>", lambda _e: self._on_close_hover(True))
        self.close_btn.bind("<Leave>", lambda _e: self._on_close_hover(False))

        # Message text
        self.text_label = tk.Label(
            self.content_frame,
            text="",
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=235,
        )
        self.text_label.pack(side="left", fill="both", expand=True)

        # Clicking on the speech bubble itself dismisses it immediately
        for w in (self, self.canvas, self.content_frame, self.icon_label, self.text_label, self.close_btn):
            w.bind("<Button-1>", lambda _e: self.hide_immediate())
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

        self.withdraw()

    def _get_colors(self) -> dict[str, str]:
        dark = self.theme == "dark"
        if dark:
            return {
                "bubble_bg": "#232323",
                "border": "#3d3d3d",
                "text": "#ffffff",
                "close_fg": "#888888",
                "close_hover": "#ffffff",
                "info_accent": "#38bdf8",
                "error_accent": "#ff4d4f",
            }
        else:
            return {
                "bubble_bg": "#ffffff",
                "border": "#cccccc",
                "text": "#1a1a1a",
                "close_fg": "#999999",
                "close_hover": "#1a1a1a",
                "info_accent": "#0078d4",
                "error_accent": "#d93025",
            }

    def _on_close_hover(self, hovering: bool) -> None:
        colors = self._get_colors()
        self.close_btn.configure(fg=colors["close_hover"] if hovering else colors["close_fg"])

    def _redraw(self) -> None:
        colors = self._get_colors()
        bg = colors["bubble_bg"]
        border = colors["border"]
        accent = colors["error_accent"] if self.current_category == "error" else colors["info_accent"]
        text_fg = colors["text"]

        self.canvas.delete("bubble_polygon")

        pts = _calculate_bubble_polygon(
            self.WIDTH,
            self.HEIGHT,
            self.CORNER_RADIUS,
            self.tail_tip_x,
            self.TAIL_WIDTH,
            self.TAIL_HEIGHT,
        )
        flat_pts: list[float] = []
        for x, y in pts:
            flat_pts.extend((x, y))

        self.canvas.create_polygon(
            flat_pts,
            fill=bg,
            outline=border,
            width=1.5,
            tags="bubble_polygon",
        )
        self.canvas.tag_raise(self._window_id)

        self.content_frame.configure(bg=bg)
        display_text = self.current_message
        if len(display_text) > 220:
            display_text = display_text[:217] + "..."
        self.text_label.configure(bg=bg, fg=text_fg, text=display_text)
        self.close_btn.configure(bg=bg, fg=colors["close_fg"])

        icon_char = "✕" if self.current_category == "error" else "ℹ"
        self.icon_label.configure(bg=bg, fg=accent, text=icon_char)

    def _reposition(self) -> None:
        try:
            if (
                self.target_widget
                and self.target_widget.winfo_exists()
                and self.target_widget.winfo_ismapped()
            ):
                self.target_widget.update_idletasks()
                bx = self.target_widget.winfo_rootx()
                by = self.target_widget.winfo_rooty()
                bw = self.target_widget.winfo_width()
                center_x = bx + bw // 2
                top_y = by

                master = self.master
                if master and master.winfo_exists() and master.winfo_ismapped():
                    master.update_idletasks()
                    mx = master.winfo_rootx()
                    my = master.winfo_rooty()
                    mw = master.winfo_width()
                    mh = master.winfo_height()
                    master_right = mx + mw
                else:
                    mx, my, mw, mh = bx - 600, by - 400, 800, 600
                    master_right = mx + mw

                # Align right edge harmoniously with master window right border (3px margin)
                desired_margin_right = 3
                cand_win_x = master_right - self.WIDTH - desired_margin_right
                cand_tail_tip_x = center_x - cand_win_x

                half_tail = self.TAIL_WIDTH // 2
                min_tail_x = self.CORNER_RADIUS + half_tail + 4
                max_tail_x = (self.WIDTH - 2) - half_tail - 2

                tail_tip_x = max(min_tail_x, min(cand_tail_tip_x, max_tail_x))
                win_x = center_x - tail_tip_x
                win_y = top_y - self.HEIGHT - 4

                min_x = mx + 2
                max_x = master_right - self.WIDTH - 2
                if min_x <= max_x:
                    if win_x < min_x:
                        win_x = min_x
                        tail_tip_x = max(min_tail_x, min(center_x - win_x, max_tail_x))
                    elif win_x > max_x:
                        win_x = max_x
                        tail_tip_x = max(min_tail_x, min(center_x - win_x, max_tail_x))

                min_y = my + 2
                if win_y < min_y:
                    win_y = min_y

                self.tail_tip_x = tail_tip_x
            else:
                raise ValueError("Target widget not mapped")
        except Exception:
            try:
                master = self.master
                if master and master.winfo_exists():
                    master.update_idletasks()
                    mx = master.winfo_rootx()
                    my = master.winfo_rooty()
                    mw = master.winfo_width()
                    mh = master.winfo_height()
                    win_x = mx + max(0, mw - self.WIDTH - 3)
                    win_y = my + max(0, mh - self.HEIGHT - 40)
                    self.tail_tip_x = self.WIDTH - 20
                else:
                    win_x, win_y = 100, 100
                    self.tail_tip_x = self.WIDTH - 20
            except Exception:
                win_x, win_y = 100, 100
                self.tail_tip_x = self.WIDTH - 20

        try:
            self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{win_x}+{win_y}")
        except Exception:
            pass

    def show(self, message: str, category: str = "info") -> None:
        was_visible = bool(self.winfo_exists() and self.winfo_ismapped() and not self._fading_out)
        self.current_message = message
        self.current_category = "error" if str(category).lower() == "error" else "info"

        self._fading_out = False
        if self._fade_job:
            try:
                self.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None

        if self._auto_close_job:
            try:
                self.after_cancel(self._auto_close_job)
            except Exception:
                pass
            self._auto_close_job = None

        self._reposition()
        self._redraw()

        if was_visible:
            # Rapid notification update in-place: maintain full opacity without flicker or re-fading
            try:
                self.attributes("-alpha", 1.0)
                self.lift()
            except Exception:
                pass
        else:
            # Set alpha to 0 before deiconify to eliminate initial full-opacity frame flash
            try:
                self.attributes("-alpha", 0.0)
                self.deiconify()
                self.lift()
            except Exception:
                pass
            self._fade_in()

        self._auto_close_job = self.after(4000, self.dismiss)

    def dismiss(self, immediate: bool = False) -> None:
        if self._auto_close_job:
            try:
                self.after_cancel(self._auto_close_job)
            except Exception:
                pass
            self._auto_close_job = None

        if immediate:
            self.hide_immediate()
            return

        if self._fading_out:
            return

        self._fade_out()

    def hide_immediate(self) -> None:
        if self._auto_close_job:
            try:
                self.after_cancel(self._auto_close_job)
            except Exception:
                pass
            self._auto_close_job = None
        if self._fade_job:
            try:
                self.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None
        self._fading_out = False
        try:
            self.withdraw()
            self.attributes("-alpha", 1.0)
        except Exception:
            pass

    def set_theme(self, theme: str) -> None:
        if self.theme != theme:
            self.theme = theme
            if not self._transparent_supported:
                self._chroma_key = "#1c1c1c" if self.theme == "dark" else "#e0e3e8"
                try:
                    self.configure(bg=self._chroma_key)
                    self.canvas.configure(bg=self._chroma_key)
                except Exception:
                    pass
            if self.winfo_exists() and self.winfo_ismapped():
                self._redraw()

    def _fade_in(self, step: int = 0) -> None:
        alpha_steps = [0.25, 0.6, 0.9, 1.0]
        if step < len(alpha_steps):
            try:
                self.attributes("-alpha", alpha_steps[step])
            except Exception:
                pass
            self._fade_job = self.after(20, lambda: self._fade_in(step + 1))
        else:
            self._fade_job = None
            try:
                self.attributes("-alpha", 1.0)
            except Exception:
                pass

    def _fade_out(self, step: int = 0) -> None:
        self._fading_out = True
        alpha_steps = [0.75, 0.45, 0.15, 0.0]
        if step < len(alpha_steps):
            try:
                self.attributes("-alpha", alpha_steps[step])
            except Exception:
                pass
            self._fade_job = self.after(20, lambda: self._fade_out(step + 1))
        else:
            self._fading_out = False
            self._fade_job = None
            try:
                self.withdraw()
                self.attributes("-alpha", 1.0)
            except Exception:
                pass


class NotificationStringVar(tk.StringVar):
    """StringVar backwards-compatible wrapper routing writes to NotificationManager."""

    def __init__(
        self,
        master: Optional[tk.Misc] = None,
        value: str = "",
        name: Optional[str] = None,
        notification_mgr: Optional[NotificationManager] = None,
    ) -> None:
        super().__init__(master=master, value=value, name=name)
        self._notification_mgr = notification_mgr
        self._suppress = False

    def set(self, value: Any, category: Optional[str] = None) -> None:
        if threading.current_thread() is not threading.main_thread():
            if self._notification_mgr and hasattr(self._notification_mgr.app, "_safe_ui_call"):
                self._notification_mgr.app._safe_ui_call(self.set, value, category)
            elif self._notification_mgr and hasattr(self._notification_mgr.app, "after"):
                self._notification_mgr.app.after(0, lambda: self.set(value, category))
            return

        super().set(value)
        if self._suppress or self._notification_mgr is None:
            return
        text = str(value).strip() if value is not None else ""
        if not text:
            return
        cat = category
        if cat is None:
            lower = text.lower()
            if lower.startswith("помилка") or " error" in lower or lower.startswith("error") or "збій" in lower:
                cat = "error"
            else:
                cat = "info"
        self._notification_mgr.notify(text, category=cat)


class NotificationManager:
    """Centralized notification manager providing thread-safe speech bubble notifications."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.last_message: str = "Ласкаво просимо до MusicPad!"
        self.last_category: str = "info"
        self._bubble: Optional[SpeechBubble] = None
        self._target_widget: Optional[tk.Widget] = None

    def set_target_widget(self, widget: tk.Widget) -> None:
        self._target_widget = widget
        if self._bubble is not None and self._bubble.winfo_exists():
            self._bubble.target_widget = widget

    @property
    def bubble(self) -> SpeechBubble:
        if self._bubble is None or not self._bubble.winfo_exists():
            theme = getattr(self.app, "theme", "dark")
            self._bubble = SpeechBubble(
                self.app,
                target_widget=self._target_widget,
                theme=theme,
            )
        return self._bubble

    @property
    def is_visible(self) -> bool:
        if self._bubble is None or not self._bubble.winfo_exists():
            return False
        try:
            return bool(self._bubble.winfo_ismapped()) and not getattr(self._bubble, "_fading_out", False)
        except Exception:
            return False

    def notify(self, message: str, category: str = "info") -> None:
        """Post a notification. Thread-safe."""
        text = str(message).strip() if message is not None else ""
        if not text:
            return
        cat = "error" if str(category).lower() == "error" else "info"

        if threading.current_thread() is not threading.main_thread():
            if hasattr(self.app, "_safe_ui_call"):
                self.app._safe_ui_call(self._dispatch_notify, text, cat)
            else:
                self.app.after(0, lambda: self._dispatch_notify(text, cat))
            return

        self._dispatch_notify(text, cat)

    def _dispatch_notify(self, message: str, category: str) -> None:
        self.last_message = message
        self.last_category = category

        # Update backwards-compatible StringVar
        if hasattr(self.app, "status_var"):
            var = self.app.status_var
            if isinstance(var, NotificationStringVar):
                var._suppress = True
                try:
                    var.set(message)
                finally:
                    var._suppress = False
            else:
                try:
                    var.set(message)
                except Exception:
                    pass

        # Don't show floating speech bubble if app window is minimized or hidden in tray
        if hasattr(self.app, "state") and self.app.state() in ("iconic", "withdrawn"):
            return

        # Show or update speech bubble
        try:
            self.bubble.show(message, category)
        except Exception:
            pass

    def notify_info(self, message: str) -> None:
        self.notify(message, category="info")

    def notify_error(self, message: str) -> None:
        self.notify(message, category="error")

    def toggle_bubble(self) -> None:
        """Toggle open/close of the last notification bubble."""
        if self.is_visible:
            self.dismiss(immediate=True)
        else:
            self.show_last()

    def show_last(self) -> None:
        msg = self.last_message or "Немає нових сповіщень."
        cat = self.last_category or "info"
        if threading.current_thread() is not threading.main_thread():
            if hasattr(self.app, "_safe_ui_call"):
                self.app._safe_ui_call(self._dispatch_notify, msg, cat)
            else:
                self.app.after(0, lambda: self._dispatch_notify(msg, cat))
            return
        self._dispatch_notify(msg, cat)

    def dismiss(self, immediate: bool = False) -> None:
        if self._bubble is not None and self._bubble.winfo_exists():
            try:
                self._bubble.dismiss(immediate=immediate)
            except Exception:
                pass

    def set_theme(self, theme: str) -> None:
        if self._bubble is not None and self._bubble.winfo_exists():
            self._bubble.set_theme(theme)

    def on_master_configure(self) -> None:
        if self._bubble is not None and self._bubble.winfo_exists() and self.is_visible:
            try:
                if hasattr(self.app, "state") and self.app.state() in ("iconic", "withdrawn"):
                    self.dismiss(immediate=True)
                else:
                    old_tail = self._bubble.tail_tip_x
                    self._bubble._reposition()
                    if self._bubble.tail_tip_x != old_tail:
                        self._bubble._redraw()
            except Exception:
                pass

    def destroy(self) -> None:
        if self._bubble is not None and self._bubble.winfo_exists():
            try:
                self._bubble.destroy()
            except Exception:
                pass
            self._bubble = None


class MusicPadApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self._tray_icon: Any = None
        self._exiting = False
        self._capturing_hotkey = False
        self._seeking_drag = False
        self._pressed_hotkeys: set[str] = set()
        self.hide_to_tray = tk.BooleanVar(value=True)
        self.mic_enabled = tk.BooleanVar(value=False)
        self.mic_muted = tk.BooleanVar(value=False)
        self.monitor_mic = tk.BooleanVar(value=False)
        self.hotkeys_enabled = tk.BooleanVar(value=True)
        self._hotkeys_enabled: bool = True
        self.auto_keys_enabled = tk.BooleanVar(value=True)
        self.loop_enabled = tk.BooleanVar(value=False)
        self.playback_count = 0
        self.title("MusicPad")
        self.geometry("960x680")
        self.minsize(740, 520)
        self.configure(bg="#1f1f1f")
        self._set_window_icon()
        self.local_tracks: list[LocalTrack] = []
        self.visible_tracks: list[LocalTrack] = []
        self.current_index = -1
        self.current_duration = 0.0
        self.is_paused = False
        self.is_playing: bool = False
        self.theme = "dark"
        self.output_devices: list[tuple[int, dict]] = []
        self.input_devices: list[tuple[int, dict]] = []
        self.output_map: dict[str, int] = {}
        self.input_map: dict[str, int] = {}
        self.monitor_map: dict[str, Optional[int]] = {}
        self.current_category: str = "my_sounds"
        self.yt_results_data: list[dict[str, Any]] = []
        self._icons: dict[str, Any] = {}
        self._settings_window: Optional[tk.Toplevel] = None
        self._prev_volume: float = 1.0
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._is_menu_open: bool = False
        self._active_menu_btn: Optional[tk.Menubutton] = None
        self._active_menu: Optional[tk.Menu] = None
        self._next_menu_btn: Optional[tk.Menubutton] = None
        self._last_closed_menu_btn: Optional[tk.Menubutton] = None
        self._menu_close_time: float = 0.0
        self._menu_buttons: dict[str, tk.Menubutton] = {}
        self._button_to_menu: dict[tk.Menubutton, tk.Menu] = {}
        self._menu_to_button: dict[tk.Menu, tk.Menubutton] = {}
        self._button_rects: dict[tk.Menubutton, tuple[int, int, int, int]] = {}
        self._stop_hover_monitor: threading.Event = threading.Event()
        self._hover_monitor_thread: Optional[threading.Thread] = None
        self._menu_winevent_hook: Any = None

        self._build_variables()
        self._build_ui()
        self._load_devices()
        self._load_saved_state()
        self.status_var.set("")
        self._setup_windows_dark_mode()
        self._apply_dark_title_bar(self)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind_all("<Button-1>", self._on_global_click, add="+")
        self._start_tray()
        self._start_hotkey_listener()
        self._poll_ui_queue()

    def _is_english(self) -> bool:
        if not hasattr(self, "lang_var"):
            return False
        return str(self.lang_var.get()).strip().lower() in ("english", "en")

    def _tr(self, key: str, *args: Any) -> str:
        lang = "en" if self._is_english() else "uk"
        text = I18N.get(lang, {}).get(key, I18N["uk"].get(key, key))
        if args:
            try:
                text = text.format(*args)
            except Exception:
                pass
        return text

    def _apply_language(self) -> None:
        is_en = self._is_english()
        # 1. Menu bar
        if hasattr(self, "menu_frame"):
            self._build_menu()
            self._cache_menu_button_rects()

        # 2. Main window track_list headings
        if hasattr(self, "track_list"):
            self.track_list.heading("index", text=self._tr("col_index"))
            self.track_list.heading("title", text=self._tr("col_title"))
            self.track_list.heading("duration", text=self._tr("col_duration"))
            self.track_list.heading("hotkey", text=self._tr("col_hotkey"))

        # 3. Category & info panel labels
        if hasattr(self, "cat_title_lbl"):
            self.cat_title_lbl.configure(text=self._tr("cat_my_sounds"))
        if hasattr(self, "_update_category_info_panel"):
            self._update_category_info_panel()

        # 4. Play / pause button & now playing
        if hasattr(self, "pause_button"):
            if getattr(self, "is_paused", False):
                self.pause_button.configure(text=self._tr("btn_resume"))
            elif getattr(self, "is_playing", False):
                self.pause_button.configure(text=self._tr("btn_pause"))
            else:
                self.pause_button.configure(text=self._tr("btn_play"))
        if hasattr(self, "now_playing_var"):
            if getattr(self, "is_playing", False) or getattr(self, "is_paused", False):
                active = getattr(getattr(self, "engine", None), "_active_track", None)
                title = active.title if active else (self.local_tracks[self.current_index].title if hasattr(self, "current_index") and 0 <= self.current_index < len(getattr(self, "local_tracks", [])) else "")
                if title:
                    self.now_playing_var.set(self._tr("now_playing", title))
            else:
                self.now_playing_var.set(self._tr("nothing_playing"))

        # 5. Settings window if open or created
        if hasattr(self, "_settings_window") and self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.title(self._tr("settings_title"))
            if hasattr(self, "settings_notebook") and hasattr(self.settings_notebook, "tabs"):
                tabs = self.settings_notebook.tabs()
                tab_keys = ["tab_audio", "tab_hotkeys", "tab_interface", "tab_devices"]
                for i in range(min(len(tabs), len(tab_keys))):
                    self.settings_notebook.tab(i, text=self._tr(tab_keys[i]))

            if hasattr(self, "_build_tab_audio"):
                self._build_tab_audio()

            if hasattr(self, "_build_tab_interface"):
                self._build_tab_interface()

            if hasattr(self, "btn_settings_reset"):
                self.btn_settings_reset.configure(text=self._tr("btn_reset"))
            if hasattr(self, "btn_settings_ok"):
                self.btn_settings_ok.configure(text=self._tr("btn_ok"))
            if hasattr(self, "btn_settings_cancel"):
                self.btn_settings_cancel.configure(text=self._tr("btn_cancel"))
            if hasattr(self, "btn_settings_apply"):
                self.btn_settings_apply.configure(text=self._tr("btn_apply"))

    def notify(self, message: str, category: str = "info") -> None:
        """Post a notification speech bubble."""
        if hasattr(self, "notification_manager"):
            self.notification_manager.notify(message, category=category)

    def notify_info(self, message: str) -> None:
        """Post an info notification speech bubble."""
        if hasattr(self, "notification_manager"):
            self.notification_manager.notify_info(message)

    def notify_error(self, message: str) -> None:
        """Post an error notification speech bubble."""
        if hasattr(self, "notification_manager"):
            self.notification_manager.notify_error(message)

    def _toggle_notification_bubble(self) -> None:
        """Toggle speech bubble notification from bottom-right message icon."""
        if hasattr(self, "notification_manager"):
            self.notification_manager.toggle_bubble()

    def _on_configure(self, event: tk.Event) -> None:
        if event and event.widget == self and hasattr(self, "notification_manager"):
            self.notification_manager.on_master_configure()

    def _on_global_click(self, event: tk.Event) -> None:
        if hasattr(self, "notification_manager") and self.notification_manager.is_visible:
            if hasattr(self, "btn_b_msg") and (event.widget is self.btn_b_msg or event.widget == self.btn_b_msg):
                return
            bubble = self.notification_manager._bubble
            if bubble and bubble.winfo_exists():
                try:
                    if str(event.widget).startswith(str(bubble)):
                        return
                except Exception:
                    pass
            self.notification_manager.dismiss()

    def _safe_ui_call(self, func: Any, *args: Any) -> None:
        """Thread-safe dispatch from background worker threads to main Tkinter UI thread."""
        if hasattr(self, "_ui_queue"):
            self._ui_queue.put((func, args))

    def _drain_ui_queue(self) -> None:
        while hasattr(self, "_ui_queue") and not self._ui_queue.empty():
            try:
                func, args = self._ui_queue.get_nowait()
                func(*args)
            except Exception:
                pass

    def _poll_ui_queue(self) -> None:
        self._drain_ui_queue()
        try:
            self.after(25, self._poll_ui_queue)
        except Exception:
            pass

    def update(self) -> None:
        self._drain_ui_queue()
        super().update()

    @staticmethod
    def _app_root_dir() -> Path:
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            if exe_dir.name.lower() == "dist":
                return exe_dir.parent
            return exe_dir
        return Path(__file__).resolve().parent

    @staticmethod
    def _resource_path(name: str) -> Path:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / name
            if candidate.exists():
                return candidate
        return MusicPadApp._app_root_dir() / name

    @staticmethod
    def _config_path() -> Path:
        cache_dir = MusicPadApp._app_root_dir() / ".cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return cache_dir / "musicpad_state.json"

    @staticmethod
    def _is_mocked_env() -> bool:
        return (
            hasattr(AudioEngine, "_mock_name")
            or hasattr(AudioEngine.list_devices, "_mock_name")
            or "mock" in type(AudioEngine).__module__.lower()
        )

    def _apply_dark_title_bar(self, window: tk.Tk | tk.Toplevel) -> None:
        if sys.platform != "win32":
            return
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)
            val = ctypes.c_int(1 if getattr(self, "theme", "dark") == "dark" else 0)
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Win10 20H1+ / Win11), fallback = 19
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
            if res != 0:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), 4)
        except Exception:
            pass

    def _setup_windows_dark_mode(self) -> None:
        """Configure Windows uxtheme and DWM dark mode for app and popup menus."""
        if sys.platform != "win32":
            return
        try:
            dark = getattr(self, "theme", "dark") == "dark"
            uxtheme = getattr(ctypes.windll, "uxtheme", None)
            if uxtheme is not None:
                fn_mode = getattr(uxtheme, "#135", None)
                if fn_mode is None:
                    try:
                        fn_mode = uxtheme[135]
                    except Exception:
                        fn_mode = None
                if fn_mode is not None:
                    import ctypes.wintypes as wintypes
                    fn_mode.argtypes = [ctypes.c_int]
                    fn_mode.restype = wintypes.HRESULT
                    fn_mode(2 if dark else 3)
            self._setup_menu_winevent_hook()
        except Exception:
            pass

    def _setup_menu_winevent_hook(self) -> None:
        """Register WinEventHook for popup menu window class #32768 styling."""
        if sys.platform != "win32":
            return
        if getattr(self, "_menu_winevent_hook", None) is not None:
            return
        try:
            import ctypes.wintypes as wintypes
            WINEVENTPROC = ctypes.WINFUNCTYPE(
                None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
            )
            def _win_event_proc(hook: Any, event: int, hwnd: int, id_obj: int, id_child: int, thread_id: int, event_time: int) -> None:
                if event == 0x0006:  # EVENT_SYSTEM_MENUPOPUPSTART
                    self._style_native_popup_menu(hwnd)
            self._winevent_proc_ref = WINEVENTPROC(_win_event_proc)
            u32 = ctypes.windll.user32
            u32.SetWinEventHook.restype = wintypes.HANDLE
            u32.SetWinEventHook.argtypes = [
                wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD
            ]
            u32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            u32.UnhookWinEvent.restype = wintypes.BOOL
            self._menu_winevent_hook = u32.SetWinEventHook(
                0x0006, 0x0006,
                None, self._winevent_proc_ref,
                os.getpid(), ctypes.windll.kernel32.GetCurrentThreadId(),
                0
            )
        except Exception:
            self._menu_winevent_hook = None

    def _style_native_popup_menu(self, hwnd: int) -> None:
        """Apply Windows dark/light border and background mode to popup menu class #32768."""
        if sys.platform != "win32" or not hwnd:
            return
        try:
            import ctypes.wintypes as wintypes
            dark = getattr(self, "theme", "dark") == "dark"
            uxtheme = getattr(ctypes.windll, "uxtheme", None)
            if uxtheme is not None:
                fn_allow = getattr(uxtheme, "#133", None)
                if fn_allow is None:
                    try:
                        fn_allow = uxtheme[133]
                    except Exception:
                        fn_allow = None
                if fn_allow is not None:
                    fn_allow.argtypes = [wintypes.HWND, wintypes.BOOL]
                    fn_allow.restype = wintypes.HRESULT
                    fn_allow(hwnd, True if dark else False)

            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD
            ]
            dwmapi.DwmSetWindowAttribute.restype = wintypes.HRESULT

            # DWMWA_USE_IMMERSIVE_DARK_MODE (20, fallback 19)
            val = ctypes.c_int(1 if dark else 0)
            res = dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
            if res != 0:
                dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(val), 4)

            # DWMWA_BORDER_COLOR = 34 (Windows 11 22000+)
            # Format: 0x00BBGGRR -> #3f3f3f is 0x003F3F3F, #d0d4da is 0x00DAD4D0
            border_color = 0x003F3F3F if dark else 0x00DAD4D0
            color_val = ctypes.c_uint32(border_color)
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color_val), 4)
        except Exception:
            pass

    def _teardown_windows_hooks(self) -> None:
        """Unhook WinEvent hooks on shutdown."""
        if sys.platform != "win32":
            return
        try:
            import ctypes.wintypes as wintypes
            hook = getattr(self, "_menu_winevent_hook", None)
            if hook:
                ctypes.windll.user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
                ctypes.windll.user32.UnhookWinEvent.restype = wintypes.BOOL
                ctypes.windll.user32.UnhookWinEvent(hook)
                self._menu_winevent_hook = None
        except Exception:
            pass

    def _set_window_icon(self) -> None:
        icon_path = self._resource_path("musicpad.png")
        if icon_path.exists():
            try:
                self._window_icon = tk.PhotoImage(file=str(icon_path))
                self.iconphoto(True, self._window_icon)
            except tk.TclError:
                pass

    def _start_tray(self) -> None:
        if pystray is None or Image is None:
            return
        icon_path = self._resource_path("musicpad.png")
        if not icon_path.exists():
            return
        try:
            image = Image.open(icon_path).convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("Відкрити MusicPad", self._show_from_tray, default=True),
                pystray.MenuItem("Стоп (F9)", lambda *_: self.after(0, self._stop)),
                pystray.MenuItem("Вийти", self._exit_from_tray),
            )
            self._tray_icon = pystray.Icon("MusicPad", image, "MusicPad", menu)
            threading.Thread(
                target=self._tray_icon.run, name="musicpad-tray", daemon=True
            ).start()
        except Exception:
            self._tray_icon = None

    def _show_from_tray(self, icon: Any = None, item: Any = None) -> None:
        self.after(0, self.deiconify)

    def _exit_from_tray(self, icon: Any = None, item: Any = None) -> None:
        self.after(0, self._exit)

    def _build_variables(self) -> None:
        self.search_var = tk.StringVar()
        self.folder_var = tk.StringVar(value="Папку не вибрано")
        self.url_var = tk.StringVar()
        self.now_playing_var = tk.StringVar(value="Нічого не відтворюється")
        self.time_var = tk.StringVar(value="0:00 / 0:00")
        self.time_display_var = tk.StringVar(value="0:00")
        self.seek_var = tk.DoubleVar(value=0.0)
        self.volume_var = tk.DoubleVar(value=1.0)
        self.mic_volume_var = tk.DoubleVar(value=1.0)
        self.output_var = tk.StringVar()
        self.input_var = tk.StringVar()
        self.mix_mic_var = tk.StringVar()
        self.monitor_var = tk.StringVar()
        self.track_source_var = tk.StringVar(value=DEFAULT_TRACK_SOURCE)
        self.notification_manager = NotificationManager(self)
        self.status_var = NotificationStringVar(self, value="", notification_mgr=self.notification_manager)
        self.auto_stop_var = tk.BooleanVar(value=True)
        self.auto_continue_var = tk.BooleanVar(value=False)
        self.loop_playlist_var = tk.BooleanVar(value=False)
        self.loop_current_var = tk.BooleanVar(value=False)
        self.play_mode_var = tk.StringVar(value="single")
        self._undo_stack: list[list[LocalTrack]] = []
        self._redo_stack: list[list[LocalTrack]] = []
        self._clipboard_tracks: list[LocalTrack] = []
        self._init_settings_variables()

    def _create_icons(self) -> None:
        self._icons = {}
        if Image is None or ImageDraw is None or ImageTk is None:
            return
        blue = "#0084d1"
        red = "#ff2020"

        try:
            # Play (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.polygon([(16, 10), (16, 54), (54, 32)], fill=blue)
            self._icons["play"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Play small (12x12)
            self._icons["play_sm"] = ImageTk.PhotoImage(img.resize((12, 12), Image.Resampling.LANCZOS))

            # Pause (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([(14, 10), (27, 54)], radius=2, fill=blue)
            d.rounded_rectangle([(37, 10), (50, 54)], radius=2, fill=blue)
            self._icons["pause"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Stop (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([(12, 12), (52, 52)], radius=2, fill=blue)
            self._icons["stop"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Prev (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rectangle([(10, 10), (18, 54)], fill=blue)
            d.polygon([(52, 10), (52, 54), (20, 32)], fill=blue)
            self._icons["prev"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Next (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.polygon([(12, 10), (12, 54), (44, 32)], fill=blue)
            d.rectangle([(46, 10), (54, 54)], fill=blue)
            self._icons["next"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Headphones (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.arc([(8, 8), (56, 56)], start=180, end=0, fill=blue, width=7)
            d.rounded_rectangle([(6, 28), (20, 52)], radius=5, fill=blue)
            d.rounded_rectangle([(44, 28), (58, 52)], radius=5, fill=blue)
            self._icons["headphones"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Mic (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([(23, 6), (41, 36)], radius=9, fill=blue)
            d.arc([(14, 16), (50, 48)], start=0, end=180, fill=blue, width=5)
            d.rectangle([(29, 48), (35, 57)], fill=blue)
            d.rectangle([(18, 57), (46, 62)], fill=blue)
            self._icons["mic"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Mute (headphones with bright red X inside, 16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.arc([(8, 8), (56, 56)], start=180, end=0, fill=blue, width=7)
            d.rounded_rectangle([(6, 28), (20, 52)], radius=5, fill=blue)
            d.rounded_rectangle([(44, 28), (58, 52)], radius=5, fill=blue)
            d.line([(24, 28), (40, 48)], fill=red, width=6)
            d.line([(40, 28), (24, 48)], fill=red, width=6)
            self._icons["headphones_mute"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Musical Note (Eighth note matching sample, 16x16)
            b64_note_mask = (
                "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAAAAACPAi4CAAACPklEQVR42u2WvWtUQRTFz8w8d4VEElEk"
                "LIGAKRKwcy1UsLEwBiSIYECjYck/oAQEAyI2NmkEbdRAxMbaFLqgEbSJNn4ExBWrFNppohvEbN7cORZZ"
                "Nw+SvN03g11OPfc3986dO2eAbf1nRZjgMKK0JboJgmD6gmYAQUcYgOgPAygU4UIABsUCdQAA2DUSCJDx"
                "TqdDALbrljMhACOlUhwFAKDl3hEbBQCUyj3utcYb4JyWfU/2ivYF5LQo2zeTh/ICrGJ+wWizevRhWitS"
                "xvkSh3eefU7+4fX0sd4KcJmjAAbfssYBGI8SHKjyunz4Zg7Teza7080PMaY4Y6+N2MKkUz4ABUCQezSO"
                "UlF8AAQAxjvuPNNX/G7iGsapCTnV6w+A6Hezbae9SwCg1AMcD8gAwhcrB0MyoPo+3+XjC4n39YOXLyS0"
                "4OULCf1c8ZmF5NPwOaQLALrnggDE/tkQgJK23S9DAFod+7YUlAGHygHTCGXb+56GADSGPv2KAgDE0PRm"
                "X40U11RJvHYHqu+NtAxQ2jkAonTjNRiYUkSrACOCQk+HfK1U6+MkplxBqwClJX9+9FA7IG8W/7mJVBRb"
                "bRhw5iNJZ4Ukz9U3ab3hiO6S1jqSztYagC3db8Ppm5mTVq8lrgypMjvqfdbYkPAEMvr6IOP1eOd+dCJj"
                "Dq+crANi3s6aAH4zUYAsdyudEeAS8THHMieAL1IvwcXk1ezxGKMV0oklFy/A52c1WS+gOtXjFa9x8fWy"
                "W5q70Q+v+G1l11+08Bhe21poLwAAAABJRU5ErkJggg=="
            )
            try:
                note_mask = Image.open(io.BytesIO(base64.b64decode(b64_note_mask))).convert("L")
                img_note = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                blue_rgb = (0, 132, 209) if blue == "#0084d1" else (0, 122, 204)
                img_note.paste(Image.new("RGBA", (64, 64), blue_rgb + (255,)), (0, 0), mask=note_mask)
            except Exception:
                img_note = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                d_note = ImageDraw.Draw(img_note)
                d_note.ellipse([(14, 40), (32, 56)], fill=blue)
                d_note.rectangle([(27, 8), (32, 46)], fill=blue)
                d_note.polygon([(32, 8), (46, 18), (32, 32)], fill=blue)

            self._icons["note"] = ImageTk.PhotoImage(img_note.resize((16, 16), Image.Resampling.LANCZOS))

            # Muted Musical Note (with bright red X across the note, 16x16)
            img_note_mute = img_note.copy()
            d_note_mute = ImageDraw.Draw(img_note_mute)
            d_note_mute.line([(18, 18), (46, 46)], fill=red, width=7)
            d_note_mute.line([(46, 18), (18, 46)], fill=red, width=7)
            self._icons["note_mute"] = ImageTk.PhotoImage(img_note_mute.resize((16, 16), Image.Resampling.LANCZOS))
            self._icons["mute"] = self._icons["note_mute"]

            # Soundpad House Small (18x18)
            img_sm = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d_sm = ImageDraw.Draw(img_sm)
            d_sm.polygon([(32, 4), (4, 30), (60, 30)], fill=blue)
            d_sm.rectangle([(12, 28), (52, 60)], fill=blue)
            d_sm.rectangle([(25, 42), (39, 60)], fill=(0, 0, 0, 0))
            self._icons["house_sm"] = ImageTk.PhotoImage(img_sm.resize((18, 18), Image.Resampling.LANCZOS))

            # Soundpad House Large (38x38)
            img_lg = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
            d_lg = ImageDraw.Draw(img_lg)
            d_lg.polygon([(64, 8), (8, 60), (120, 60)], fill=blue)
            d_lg.rectangle([(24, 56), (104, 120)], fill=blue)
            d_lg.rectangle([(50, 84), (78, 120)], fill=(0, 0, 0, 0))
            self._icons["house_lg"] = ImageTk.PhotoImage(img_lg.resize((38, 38), Image.Resampling.LANCZOS))

            # Wrench (vector-drawn 256x256 anti-aliased downscaled to 20x20, authentic Soundpad blue)
            img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            # Handle
            d.line([(70, 186), (175, 81)], fill=blue, width=38)
            # Tail rounded ring end
            d.ellipse([(42, 158), (98, 214)], fill=blue)
            d.ellipse([(58, 174), (82, 198)], fill=(0, 0, 0, 0))
            # Open-jaw head at (182, 74)
            cx, cy = 182, 74
            r_outer = 60
            d.ellipse([(cx - r_outer, cy - r_outer), (cx + r_outer, cy + r_outer)], fill=blue)
            # Cutout for jaw notch
            ux = 1.0 / math.sqrt(2)
            uy = -1.0 / math.sqrt(2)
            vx = -uy
            vy = ux
            jaw_poly = [
                (cx + 0 * ux - 26 * vx, cy + 0 * uy - 26 * vy),
                (cx + 75 * ux - 26 * vx, cy + 75 * uy - 26 * vy),
                (cx + 75 * ux + 26 * vx, cy + 75 * uy + 26 * vy),
                (cx + 0 * ux + 26 * vx, cy + 0 * uy + 26 * vy),
            ]
            d.polygon(jaw_poly, fill=(0, 0, 0, 0))
            d.ellipse([(cx - 26, cy - 26), (cx + 26, cy + 26)], fill=(0, 0, 0, 0))
            self._icons["wrench"] = ImageTk.PhotoImage(img.resize((20, 20), Image.Resampling.LANCZOS))

            # Fast Forward (two solid triangles ▶▶, 14x14)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.polygon([(6, 12), (6, 52), (32, 32)], fill=blue)
            d.polygon([(32, 12), (32, 52), (58, 32)], fill=blue)
            self._icons["ff"] = ImageTk.PhotoImage(img.resize((14, 14), Image.Resampling.LANCZOS))

            # Loop (opposing horizontal arrows, 14x14)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.line([(12, 18), (48, 18)], fill=blue, width=5)
            d.polygon([(44, 10), (56, 18), (44, 26)], fill=blue)
            d.line([(12, 18), (12, 32)], fill=blue, width=5)
            d.line([(16, 46), (52, 46)], fill=blue, width=5)
            d.polygon([(20, 38), (8, 46), (20, 54)], fill=blue)
            d.line([(52, 32), (52, 46)], fill=blue, width=5)
            self._icons["loop"] = ImageTk.PhotoImage(img.resize((14, 14), Image.Resampling.LANCZOS))

            # Chat bubble (14x14)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([(6, 10), (58, 46)], radius=10, fill=blue)
            d.polygon([(14, 44), (10, 58), (28, 46)], fill=blue)
            self._icons["chat"] = ImageTk.PhotoImage(img.resize((14, 14), Image.Resampling.LANCZOS))

            # H button (18x18)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rectangle([(2, 2), (61, 61)], outline=blue, width=5, fill=(0, 0, 0, 0))
            d.line([(20, 16), (20, 48)], fill=blue, width=7)
            d.line([(44, 16), (44, 48)], fill=blue, width=7)
            d.line([(20, 32), (44, 32)], fill=blue, width=7)
            self._icons["btn_h"] = ImageTk.PhotoImage(img.resize((18, 18), Image.Resampling.LANCZOS))

            # A button (18x18)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rectangle([(2, 2), (61, 61)], outline=blue, width=5, fill=(0, 0, 0, 0))
            d.line([(18, 48), (32, 16)], fill=blue, width=7)
            d.line([(46, 48), (32, 16)], fill=blue, width=7)
            d.line([(22, 36), (42, 36)], fill=blue, width=6)
            self._icons["btn_a"] = ImageTk.PhotoImage(img.resize((18, 18), Image.Resampling.LANCZOS))

            # YouTube category badge (red rounded rectangle with white play triangle, 18x18)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([(4, 12), (60, 52)], radius=12, fill="#ff1f1f")
            d.polygon([(26, 22), (26, 42), (44, 32)], fill="#ffffff")
            self._icons["yt_icon"] = ImageTk.PhotoImage(img.resize((18, 18), Image.Resampling.LANCZOS))

            # YouTube / Soundwaves titlebar icon (16x16)
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.arc([(10, 8), (54, 52)], start=180, end=0, fill=blue, width=6)
            d.rounded_rectangle([(8, 28), (22, 54)], radius=4, fill=blue)
            d.rounded_rectangle([(42, 28), (56, 54)], radius=4, fill=blue)
            d.line([(28, 24), (28, 44)], fill=blue, width=3)
            d.line([(36, 18), (36, 48)], fill=blue, width=3)
            self._icons["yt_title_icon"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

            # Blue Gear Icon (16x16) for Settings menu item
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                x1 = 32 + 25 * math.cos(rad)
                y1 = 32 + 25 * math.sin(rad)
                d.rectangle([x1 - 6, y1 - 6, x1 + 6, y1 + 6], fill=blue)
            d.ellipse([10, 10, 54, 54], fill=blue)
            d.ellipse([22, 22, 42, 42], fill=(0, 0, 0, 0))
            self._icons["gear_blue"] = ImageTk.PhotoImage(img.resize((16, 16), Image.Resampling.LANCZOS))

        except Exception:
            self._icons = {}

    def _build_ui(self) -> None:
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._create_icons()

        self._build_menu()
        self._build_top_toolbar()
        self._build_search_bar()
        self._build_main_split()
        self._build_bottom_toolbar()
        self._build_settings_dialog()
        self._bind_shortcuts()
        self._apply_theme()

    def _build_menu(self) -> None:
        c = self._get_theme_colors()
        menu_bg = c["menu_bg"]
        menu_fg = c["menu_fg"]
        active_bg = c["active_menu_bg"]
        active_fg = c["active_menu_fg"]
        select_color = c["accent"]
        font_menu = ("Segoe UI", 9)

        # In-window dark menu bar frame (authentic dark look on Windows)
        if not hasattr(self, "menu_frame"):
            self.menu_frame = tk.Frame(self, bg=menu_bg, height=24)
            self.menu_frame.pack(fill="x", side="top")
            self.menu_frame.pack_propagate(False)
        else:
            self._stop_menu_hover_tracker()
            for child in self.menu_frame.winfo_children():
                child.destroy()
            self.menu_frame.configure(bg=menu_bg)

        if hasattr(self, "menubar") and self.menubar:
            try:
                self.menubar.destroy()
            except Exception:
                pass

        self.menubar = tk.Menu(
            self,
            bg=menu_bg,
            fg=menu_fg,
            activebackground=active_bg,
            activeforeground=active_fg,
            relief="flat",
            bd=0,
        )

        menu_kw = {
            "tearoff": 0,
            "bg": menu_bg,
            "fg": menu_fg,
            "activebackground": active_bg,
            "activeforeground": active_fg,
            "selectcolor": select_color,
            "font": font_menu,
            "relief": "solid",
            "bd": 1,
            "activeborderwidth": 0,
        }

        # =========================================================================
        # 1. Меню «Файл» / "File"
        # =========================================================================
        file_menu = tk.Menu(self.menubar, **menu_kw)
        file_menu.add_command(label=self._tr("file_add"), command=self._add_files)
        file_menu.add_separator()
        file_menu.add_command(label=self._tr("file_new"), command=self._new_sound_list)
        file_menu.add_command(label=self._tr("file_load"), command=self._load_sound_list)
        file_menu.add_command(label=self._tr("file_save"), accelerator="Ctrl+S", command=self._save_sound_list)
        file_menu.add_command(label=self._tr("file_save_as"), command=self._save_sound_list_as)

        # Підменю: Завантажити останній список звуків / Load recent sound list
        sub_recent = tk.Menu(file_menu, **menu_kw)
        sub_recent.add_command(label=self._tr("file_last_autosave"), command=self._load_saved_state)
        sub_recent.add_command(label=self._tr("file_empty"), state="disabled")
        file_menu.add_cascade(label=self._tr("file_load_recent"), menu=sub_recent)

        # Підменю: Відновити список звуків / Restore sound list
        sub_restore = tk.Menu(file_menu, **menu_kw)
        sub_restore.add_command(label=self._tr("file_restore_backup"), command=self._restore_backup_list)
        sub_restore.add_command(label=self._tr("file_restore_default"), command=self._restore_default_list)
        file_menu.add_cascade(label=self._tr("file_restore"), menu=sub_restore)

        # Підменю: Експорт / Export
        sub_export = tk.Menu(file_menu, **menu_kw)
        sub_export.add_command(label=self._tr("file_export_json"), command=lambda: self._export_playlist("json"))
        sub_export.add_command(label=self._tr("file_export_m3u"), command=lambda: self._export_playlist("m3u"))
        sub_export.add_command(label=self._tr("file_export_txt"), command=lambda: self._export_playlist("txt"))
        file_menu.add_cascade(label=self._tr("file_export"), menu=sub_export)

        # Підменю: Статистика / Statistics
        sub_stats = tk.Menu(file_menu, **menu_kw)
        sub_stats.add_command(label=self._tr("file_stats_overall"), command=self._show_statistics_dialog)
        sub_stats.add_command(label=self._tr("file_stats_reset"), command=self._reset_playback_counter)
        file_menu.add_cascade(label=self._tr("file_stats"), menu=sub_stats)

        file_menu.add_separator()
        file_menu.add_command(label=self._tr("file_settings"), command=self._open_settings)
        file_menu.add_separator()
        file_menu.add_command(label=self._tr("file_exit"), accelerator="Alt+F4", command=self._exit)
        self.menubar.add_cascade(label=self._tr("menu_file"), menu=file_menu)

        # =========================================================================
        # 2. Меню «Редагування» / "Edit"
        # =========================================================================
        edit_menu = tk.Menu(self.menubar, **menu_kw)
        edit_menu.add_command(label=self._tr("edit_undo"), accelerator="Ctrl+Z", command=self._undo)
        edit_menu.add_command(label=self._tr("edit_redo"), accelerator="Ctrl+Y", command=self._redo)
        edit_menu.add_separator()
        edit_menu.add_command(label=self._tr("edit_cut"), accelerator="Ctrl+X", command=self._cut_track)
        edit_menu.add_command(label=self._tr("edit_copy"), accelerator="Ctrl+C", command=self._copy_track)
        edit_menu.add_command(label=self._tr("edit_paste"), accelerator="Ctrl+V", command=self._paste_track)
        edit_menu.add_command(label=self._tr("edit_delete"), accelerator="Del", command=self._remove_selected_track)
        edit_menu.add_command(label=self._tr("edit_shuffle"), command=self._shuffle_tracks)
        edit_menu.add_separator()
        edit_menu.add_command(label=self._tr("edit_select_all"), accelerator="Ctrl+A", command=self._select_all_tracks)

        # Підменю: Вибрати останній файл / Select last file
        sub_select_last = tk.Menu(edit_menu, **menu_kw)
        sub_select_last.add_command(label=self._tr("edit_last_in_list"), command=self._select_last_in_list)
        sub_select_last.add_command(label=self._tr("edit_last_added"), command=self._select_last_added)
        sub_select_last.add_command(label=self._tr("edit_last_played"), command=self._select_last_played)
        edit_menu.add_cascade(label=self._tr("edit_select_last"), menu=sub_select_last)

        edit_menu.add_command(label=self._tr("edit_find_missing"), command=self._find_missing_audio_files)
        edit_menu.add_separator()
        edit_menu.add_command(label=self._tr("edit_search"), accelerator="Ctrl+F", command=self._toggle_search_bar)
        self.menubar.add_cascade(label=self._tr("menu_edit"), menu=edit_menu)

        # =========================================================================
        # 3. Меню «Відтворення» / "Playback"
        # =========================================================================
        play_menu = tk.Menu(self.menubar, **menu_kw)
        play_menu.add_command(label=self._tr("play_speakers_and_mic"), command=self._play_speakers_and_mic)
        play_menu.add_separator()
        play_menu.add_command(
            label=self._tr("play_random_all"),
            command=lambda: self._play_random_track(speakers=True, mic=True),
        )
        play_menu.add_command(
            label=self._tr("play_random_spk"),
            command=lambda: self._play_random_track(speakers=True, mic=False),
        )
        play_menu.add_command(
            label=self._tr("play_random_mic"),
            command=lambda: self._play_random_track(speakers=False, mic=True),
        )

        # Підменю: Відтворити випадковий файл із усіх категорій / Play random from all
        sub_rand_all = tk.Menu(play_menu, **menu_kw)
        sub_rand_all.add_command(
            label=self._tr("play_cat_both"),
            command=lambda: self._play_random_track(speakers=True, mic=True),
        )
        sub_rand_all.add_command(
            label=self._tr("play_cat_spk"),
            command=lambda: self._play_random_track(speakers=True, mic=False),
        )
        sub_rand_all.add_command(
            label=self._tr("play_cat_mic"),
            command=lambda: self._play_random_track(speakers=False, mic=True),
        )
        play_menu.add_cascade(label=self._tr("play_random_cat"), menu=sub_rand_all)

        # Підменю: Відтворити останній файл (динаміки та мікрофон) / Play last file
        sub_last = tk.Menu(play_menu, **menu_kw)
        sub_last.add_command(label=self._tr("play_repeat_last"), command=self._replay_last_track)
        sub_last.add_command(label=self._tr("play_play_last_added"), command=self._play_last_added_track)
        play_menu.add_cascade(label=self._tr("play_last_menu"), menu=sub_last)

        play_menu.add_separator()
        play_menu.add_command(label=self._tr("play_stop"), command=self._stop)
        play_menu.add_command(label=self._tr("play_pause"), command=self._toggle_pause)
        play_menu.add_command(label=self._tr("play_prev"), command=self._previous)
        play_menu.add_command(label=self._tr("play_next"), command=self._next)
        play_menu.add_separator()
        play_menu.add_checkbutton(
            label=self._tr("play_auto_continue"),
            variable=self.auto_continue_var,
        )
        play_menu.add_checkbutton(
            label=self._tr("play_loop_playlist"),
            variable=self.loop_playlist_var,
        )
        play_menu.add_checkbutton(
            label=self._tr("play_loop_current"),
            variable=self.loop_current_var,
        )
        play_menu.add_checkbutton(
            label=self._tr("play_auto_stop"),
            variable=self.auto_stop_var,
        )

        # Підменю: Режим відтворення / Playback mode
        sub_mode = tk.Menu(play_menu, **menu_kw)
        sub_mode.add_radiobutton(label=self._tr("play_mode_single"), variable=self.play_mode_var, value="single")
        sub_mode.add_radiobutton(label=self._tr("play_mode_overlap"), variable=self.play_mode_var, value="overlap")
        sub_mode.add_radiobutton(label=self._tr("play_mode_interrupt"), variable=self.play_mode_var, value="interrupt")
        play_menu.add_cascade(label=self._tr("play_mode"), menu=sub_mode)
        self.menubar.add_cascade(label=self._tr("menu_play"), menu=play_menu)

        # =========================================================================
        # 4. Меню «Вікно» / "Window"
        # =========================================================================
        win_menu = tk.Menu(self.menubar, **menu_kw)

        # Підменю: Категорії / Categories
        sub_cat = tk.Menu(win_menu, **menu_kw)
        sub_cat.add_command(label=self._tr("win_my_sounds"), command=lambda: self._on_category_select("my_sounds"))
        sub_cat.add_command(label=self._tr("win_yt"), command=lambda: self._on_category_select("youtube"))
        sub_cat.add_separator()
        sub_cat.add_command(label=self._tr("win_toggle_cat"), command=self._toggle_left_panel)
        win_menu.add_cascade(label=self._tr("win_cat"), menu=sub_cat)

        # Підменю: Швидка панель / Quick panel
        sub_quick = tk.Menu(win_menu, **menu_kw)
        sub_quick.add_command(label=self._tr("win_search_panel"), command=self._toggle_search_bar)
        sub_quick.add_command(label=self._tr("win_quick_yt"), command=self._open_yt_search)
        win_menu.add_cascade(label=self._tr("win_quick"), menu=sub_quick)

        # Підменю: Гарячі клавіші / Hotkeys
        sub_hotkeys = tk.Menu(win_menu, **menu_kw)
        sub_hotkeys.add_command(label=self._tr("win_hk_enable"), command=lambda: self.hotkeys_enabled.set(True))
        sub_hotkeys.add_command(label=self._tr("win_hk_disable"), command=lambda: self.hotkeys_enabled.set(False))
        sub_hotkeys.add_separator()
        sub_hotkeys.add_command(label=self._tr("win_hk_assign"), command=self._assign_hotkey_dialog)
        sub_hotkeys.add_command(label=self._tr("win_hk_list"), command=self._show_hotkeys_help)
        win_menu.add_cascade(label=self._tr("win_hotkeys"), menu=sub_hotkeys)

        win_menu.add_command(label=self._tr("win_record"), accelerator="Ctrl+R", command=self._record_audio_dialog)
        win_menu.add_command(label=self._tr("win_color"), command=self._color_tool_dialog)
        win_menu.add_command(label=self._tr("win_tts"), accelerator="Ctrl+T", command=self._text_to_speech_dialog)
        self.menubar.add_cascade(label=self._tr("menu_win"), menu=win_menu)

        mb_kw = {
            "bg": menu_bg,
            "fg": menu_fg,
            "activebackground": active_bg,
            "activeforeground": active_fg,
            "bd": 0,
            "font": ("Segoe UI", 9),
            "padx": 7,
            "pady": 2,
            "relief": "flat",
            "highlightthickness": 0,
            "direction": "below",
            "cursor": "hand2",
        }

        self._menu_buttons.clear()
        self._button_to_menu.clear()
        self._menu_to_button.clear()

        for label_key, ua_label, m in [
            ("menu_file", "Файл", file_menu),
            ("menu_edit", "Редагування", edit_menu),
            ("menu_play", "Відтворення", play_menu),
            ("menu_win", "Вікно", win_menu),
        ]:
            display_label = self._tr(label_key)
            mb = tk.Menubutton(self.menu_frame, text=display_label, menu=m, **mb_kw)
            mb.pack(side="left", fill="y")
            self._menu_buttons[display_label] = mb
            self._menu_buttons[ua_label] = mb
            self._menu_buttons[label_key] = mb
            self._button_to_menu[mb] = m
            self._menu_to_button[m] = mb
            mb.bind("<Button-1>", lambda e, b=mb, menu=m: self._open_menu_dropdown(e, b, menu))
            mb.bind("<space>", lambda e, b=mb, menu=m: self._open_menu_dropdown(e, b, menu))
            mb.bind("<Return>", lambda e, b=mb, menu=m: self._open_menu_dropdown(e, b, menu))
            mb.bind("<Enter>", lambda e, b=mb: self._on_menu_enter(e, b))
            mb.bind("<Leave>", lambda e, b=mb: self._on_menu_leave(e, b))

    def is_menu_active(self) -> bool:
        """Return True if any top-level menu dropdown is currently open."""
        return bool(getattr(self, "_is_menu_open", False) and getattr(self, "_active_menu_btn", None) is not None)

    def _cache_menu_button_rects(self) -> None:
        """Cache top-level menu button screen bounding boxes for hover tracker."""
        rects: dict[tk.Menubutton, tuple[int, int, int, int]] = {}
        mf_y = None
        mf_h = None
        if hasattr(self, "menu_frame"):
            try:
                mf_y = self.menu_frame.winfo_rooty()
                mf_h = self.menu_frame.winfo_height()
            except Exception:
                pass
        for mb in self._menu_buttons.values():
            try:
                bx = mb.winfo_rootx()
                bw = mb.winfo_width()
                by = mf_y if (mf_y is not None and mf_y > 0) else mb.winfo_rooty()
                bh = mf_h if (mf_h is not None and mf_h > 0) else mb.winfo_height()
                rects[mb] = (bx, by, bw, bh)
            except Exception:
                pass
        self._button_rects = rects

    def _start_menu_hover_tracker(self) -> None:
        """Start a background monitor thread for mouse hover switching during native popup menu."""
        if sys.platform != "win32":
            return
        self._stop_menu_hover_tracker()

        stop_event = threading.Event()
        self._stop_hover_monitor = stop_event

        def _tracker() -> None:
            import ctypes
            import ctypes.wintypes as wintypes
            u32 = ctypes.windll.user32
            pt = wintypes.POINT()
            while not stop_event.is_set():
                time.sleep(0.015)
                if not getattr(self, "_is_menu_open", False) or getattr(self, "_active_menu_btn", None) is None:
                    break
                if not u32.GetCursorPos(ctypes.byref(pt)):
                    continue
                rects = getattr(self, "_button_rects", {})
                active_btn = getattr(self, "_active_menu_btn", None)
                for mb, (bx, by, bw, bh) in rects.items():
                    if mb != active_btn:
                        if bx <= pt.x < bx + bw and by <= pt.y < by + bh:
                            self._next_menu_btn = mb
                            self._close_current_popup()
                            return

        t = threading.Thread(target=_tracker, name="menu-hover-tracker", daemon=True)
        self._hover_monitor_thread = t
        t.start()

    def _stop_menu_hover_tracker(self) -> None:
        """Stop the background mouse hover tracker."""
        ev = getattr(self, "_stop_hover_monitor", None)
        if ev is not None:
            ev.set()
        t = getattr(self, "_hover_monitor_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=0.05)

    def _close_current_popup(self) -> None:
        """Close the currently visible Win32 popup menu or Tk menu."""
        if sys.platform == "win32":
            try:
                import ctypes
                import ctypes.wintypes as wintypes
                u32 = ctypes.windll.user32
                u32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
                u32.FindWindowExW.restype = wintypes.HWND
                u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                u32.SendMessageW.restype = wintypes.LPARAM
                my_pid = os.getpid()
                for _ in range(5):
                    closed_any = False
                    h = 0
                    while True:
                        h = u32.FindWindowExW(0, h, "#32768", None)
                        if not h:
                            break
                        pid = wintypes.DWORD()
                        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                        if pid.value == my_pid:
                            u32.SendMessageW(h, 0x0100, 0x1B, 0)
                            closed_any = True
                    if not closed_any:
                        break
                    time.sleep(0.01)
            except Exception:
                pass
        if threading.current_thread() is threading.main_thread():
            active_m = getattr(self, "_active_menu", None)
            if active_m:
                try:
                    active_m.unpost()
                except Exception:
                    pass

    def _switch_menu(self, btn: tk.Menubutton) -> None:
        """Switch active menu dropdown to target menu button."""
        if not self.is_menu_active() or self._active_menu_btn == btn:
            return
        self._next_menu_btn = btn
        self._close_current_popup()

    def _on_menu_enter(self, event: Any, btn: tk.Menubutton) -> None:
        """Handle cursor entering a menu button."""
        if self.is_menu_active() and self._active_menu_btn != btn:
            self._switch_menu(btn)
        elif not self.is_menu_active():
            c = self._get_theme_colors()
            btn.configure(
                bg=c["active_menu_bg"],
                fg=c["active_menu_fg"],
            )

    def _on_menu_leave(self, event: Any, btn: tk.Menubutton) -> None:
        """Handle cursor leaving a menu button."""
        if self._active_menu_btn != btn:
            c = self._get_theme_colors()
            btn.configure(
                bg=c["menu_bg"],
                fg=c["menu_fg"],
            )

    def _open_menu_dropdown(self, event: Any, btn: tk.Menubutton, menu: tk.Menu) -> str:
        """Open menu dropdown with hover-switching loop."""
        now = time.monotonic()
        if btn == getattr(self, "_last_closed_menu_btn", None) and (now - getattr(self, "_menu_close_time", 0.0) < 0.25):
            return "break"

        self._is_menu_open = True
        curr_btn: Optional[tk.Menubutton] = btn
        curr_menu: Optional[tk.Menu] = menu

        try:
            while curr_btn is not None and self._is_menu_open:
                self._active_menu_btn = curr_btn
                self._active_menu = curr_menu
                self._next_menu_btn = None

                dark = getattr(self, "theme", "dark") == "dark"
                curr_btn.configure(
                    bg="#383838" if dark else "#c8ccd2",
                    fg="#ffffff" if dark else "#000000",
                )
                curr_btn.update_idletasks()
                self._cache_menu_button_rects()

                x = curr_btn.winfo_rootx()
                y = curr_btn.winfo_rooty() + curr_btn.winfo_height()

                self._start_menu_hover_tracker()
                try:
                    if curr_menu:
                        curr_menu.tk_popup(x, y)
                finally:
                    self._stop_menu_hover_tracker()
                    self._menu_close_time = time.monotonic()
                    self._last_closed_menu_btn = curr_btn
                    self._active_menu_btn = None
                    self._active_menu = None
                    c = self._get_theme_colors()
                    curr_btn.configure(
                        bg=c["menu_bg"],
                        fg=c["menu_fg"],
                    )

                if self._next_menu_btn is not None and self._is_menu_open:
                    curr_btn = self._next_menu_btn
                    curr_menu = self._button_to_menu.get(curr_btn)
                    self._next_menu_btn = None
                else:
                    curr_btn = None
        finally:
            self._is_menu_open = False
            self._active_menu_btn = None
            self._active_menu = None
            self._next_menu_btn = None
            c = self._get_theme_colors()
            for b in self._menu_buttons.values():
                b.configure(bg=c["menu_bg"], fg=c["menu_fg"])

        return "break"

    def _is_entry_focused(self) -> bool:
        w = self.focus_get()
        if w is None:
            try:
                last_w = self.tk.call("focus", "-lastfor", self._w)
                if last_w:
                    w = self.nametowidget(last_w)
            except Exception:
                pass
        return isinstance(w, (tk.Entry, ttk.Entry, tk.Text))

    def _record_undo_state(self) -> None:
        import copy
        self._undo_stack.append([copy.copy(t) for t in self.local_tracks])
        if len(self._undo_stack) > 30:
            self._undo_stack.pop(0)

    def _undo(self) -> None:
        if not self._undo_stack:
            self.status_var.set("Немає дій для скасування.")
            return
        import copy
        self._redo_stack.append([copy.copy(t) for t in self.local_tracks])
        self.local_tracks = self._undo_stack.pop()
        self._filter_tracks()
        self._save_state()
        self.status_var.set("Дію скасовано.")

    def _redo(self) -> None:
        if not self._redo_stack:
            self.status_var.set("Немає дій для повторення.")
            return
        import copy
        self._undo_stack.append([copy.copy(t) for t in self.local_tracks])
        self.local_tracks = self._redo_stack.pop()
        self._filter_tracks()
        self._save_state()
        self.status_var.set("Дію відновлено.")

    def _cut_track(self) -> None:
        if self._is_entry_focused():
            return
        sel = self._selected_track()
        if not sel:
            return
        track, idx = sel
        self._record_undo_state()
        self._clipboard_tracks = [track]
        del self.local_tracks[idx]
        self._filter_tracks()
        self._save_state()
        self.status_var.set(f"Вирізано: {track.title}")

    def _copy_track(self) -> None:
        if self._is_entry_focused():
            return
        sel = self._selected_track()
        if not sel:
            return
        track, _ = sel
        self._clipboard_tracks = [track]
        self.status_var.set(f"Скопійовано: {track.title}")

    def _paste_track(self) -> None:
        if self._is_entry_focused():
            return
        if not self._clipboard_tracks:
            return
        self._record_undo_state()
        import copy
        for t in self._clipboard_tracks:
            self.local_tracks.append(copy.copy(t))
        self._filter_tracks()
        self._save_state()
        self.status_var.set("Вставлено запис зі буфера обміну.")

    def _shuffle_tracks(self) -> None:
        if len(self.local_tracks) < 2:
            return
        self._record_undo_state()
        random.shuffle(self.local_tracks)
        self._filter_tracks()
        self._save_state()
        self.status_var.set("Список звуків перемішано.")

    def _select_all_tracks(self) -> None:
        if self._is_entry_focused():
            w = self.focus_get()
            if hasattr(w, "select_range"):
                try:
                    w.select_range(0, tk.END)  # type: ignore[union-attr]
                except Exception:
                    pass
            return
        children = self.track_list.get_children()
        if children:
            self.track_list.selection_set(children)
            self._update_stats_ui()

    def _select_last_in_list(self) -> None:
        children = self.track_list.get_children()
        if children:
            self.track_list.selection_set(children[-1])
            self.track_list.see(children[-1])
            self._update_stats_ui()

    def _select_last_added(self) -> None:
        self._select_last_in_list()

    def _select_last_played(self) -> None:
        if 0 <= self.current_index < len(self.visible_tracks):
            iid = str(self.current_index)
            if self.track_list.exists(iid):
                self.track_list.selection_set(iid)
                self.track_list.see(iid)
                self._update_stats_ui()

    def _find_missing_audio_files(self) -> None:
        missing = []
        for t in self.local_tracks:
            if not getattr(t, "is_url", False):
                if not Path(t.path).is_file():
                    missing.append(t.title)
        if missing:
            sample = "\n".join(missing[:8]) + ("\n..." if len(missing) > 8 else "")
            messagebox.showwarning(
                "Відсутні аудіофайли",
                f"Виявлено {len(missing)} відсутніх аудіофайлів на диску:\n\n{sample}",
            )
        else:
            messagebox.showinfo("Перевірка файлів", "Усі аудіофайли наявні на диску!")

    def _new_sound_list(self) -> None:
        if self.local_tracks and messagebox.askyesno("Новий список", "Очистити поточний список звуків та створити новий?"):
            self._record_undo_state()
            self.local_tracks.clear()
            self._filter_tracks()
            self._save_state()
            self.status_var.set("Створено новий список звуків.")

    def _load_sound_list(self) -> None:
        path = filedialog.askopenfilename(
            title="Завантажити список звуків",
            filetypes=[("Soundpad / JSON список", "*.json;*.spl;*.m3u;*.txt"), ("Всі файли", "*.*")],
        )
        if path:
            self._import_sound_list_file(path)

    def _import_sound_list_file(self, filepath: str) -> None:
        p = Path(filepath)
        if not p.is_file():
            return
        self._record_undo_state()
        try:
            if p.suffix.lower() == ".json":
                data = json.loads(p.read_text(encoding="utf-8"))
                tracks_data = data.get("tracks", []) if isinstance(data, dict) else data
                self.local_tracks = [LocalTrack.from_dict(d) for d in tracks_data if isinstance(d, dict)]
            else:
                lines = [
                    line.strip()
                    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if line.strip() and not line.startswith("#")
                ]
                new_tracks = []
                for line in lines:
                    tp = Path(line)
                    if tp.is_file():
                        t = scan_file(tp)
                        if t:
                            new_tracks.append(t)
                if new_tracks:
                    self.local_tracks = new_tracks
            self._filter_tracks()
            self._save_state()
            self.status_var.set(f"Завантажено список звуків: {p.name}")
        except Exception as exc:
            self.notify_error(f"Не вдалося завантажити список: {exc}")

    def _save_sound_list(self) -> None:
        self._save_state()
        self.status_var.set("Список звуків успішно збережено.")

    def _save_sound_list_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Зберегти список звуків як...",
            defaultextension=".json",
            filetypes=[("Soundpad JSON список", "*.json"), ("M3U плейлист", "*.m3u"), ("Текстовий файл", "*.txt")],
        )
        if path:
            self._export_playlist_to(path)

    def _restore_backup_list(self) -> None:
        backup = self._config_path().with_suffix(".bak")
        if backup.is_file():
            self._import_sound_list_file(str(backup))
            self.status_var.set("Відновлено з резервної копії.")
        else:
            self.status_var.set("Резервна копія списку не знайдена.")

    def _restore_default_list(self) -> None:
        self._load_saved_state()
        self.status_var.set("Список скинуто до збереженого стану.")

    def _export_playlist(self, fmt: str) -> None:
        ext = f".{fmt}"
        path = filedialog.asksaveasfilename(
            title=f"Експортувати список як {fmt.upper()}",
            defaultextension=ext,
            filetypes=[(f"{fmt.upper()} файл", f"*{ext}"), ("Всі файли", "*.*")],
        )
        if path:
            self._export_playlist_to(path)

    def _export_playlist_to(self, path: str) -> None:
        p = Path(path)
        try:
            if p.suffix.lower() == ".json":
                data = {"tracks": [t.to_dict() for t in self.local_tracks]}
                p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            elif p.suffix.lower() == ".m3u":
                lines = ["#EXTM3U"]
                for t in self.local_tracks:
                    lines.append(f"#EXTINF:{int(t.duration)},{t.title}")
                    lines.append(t.path)
                p.write_text("\n".join(lines), encoding="utf-8")
            else:
                lines = [t.path for t in self.local_tracks]
                p.write_text("\n".join(lines), encoding="utf-8")
            self.status_var.set(f"Список успішно експортовано: {p.name}")
        except Exception as exc:
            self.notify_error(f"Помилка експорту: {exc}")

    def _show_statistics_dialog(self) -> None:
        total_sounds = len(self.local_tracks)
        total_duration = sum(t.duration for t in self.local_tracks)
        msg = (
            f"Загальна статистика Soundpad:\n\n"
            f"• Усього звуків у бібліотеці: {total_sounds}\n"
            f"• Сумарна тривалість: {format_duration(total_duration)}\n"
            f"• Лічильник відтворень: {self.playback_count}\n"
            f"• Поточна категорія: {'YouTube' if self.current_category == 'youtube' else 'Мої звуки'}\n"
        )
        messagebox.showinfo("Статистика", msg)

    def _reset_playback_counter(self) -> None:
        self.playback_count = 0
        self._update_stats_ui()
        self._save_state()
        self.status_var.set("Лічильник відтворень успішно скинуто.")

    def _play_speakers_and_mic(self) -> None:
        self._play_selected()

    def _play_random_track(self, speakers: bool = True, mic: bool = True) -> None:
        if not self.visible_tracks:
            return
        track = random.choice(self.visible_tracks)
        self.current_index = self.visible_tracks.index(track)
        self._play_local_track(track)

    def _replay_last_track(self) -> None:
        if 0 <= self.current_index < len(self.visible_tracks):
            self._play_local_track(self.visible_tracks[self.current_index])
        elif self.visible_tracks:
            self._play_local_track(self.visible_tracks[0])

    def _play_last_added_track(self) -> None:
        if self.visible_tracks:
            self.current_index = len(self.visible_tracks) - 1
            self._play_local_track(self.visible_tracks[-1])

    def _toggle_auto_continue(self) -> None:
        val = not self.auto_continue_var.get()
        self.auto_continue_var.set(val)
        self.status_var.set(f"Продовження відтворення: {'Увімкнено' if val else 'Вимкнено'}")

    def _toggle_repeat_playlist(self) -> None:
        val = not self.loop_playlist_var.get()
        self.loop_playlist_var.set(val)
        self.status_var.set(f"Повторення списку звуків: {'Увімкнено' if val else 'Вимкнено'}")

    def _toggle_repeat_single(self) -> None:
        val = not self.loop_current_var.get()
        self.loop_current_var.set(val)
        self.status_var.set(f"Повторне відтворення поточного файлу: {'Увімкнено' if val else 'Вимкнено'}")

    def _toggle_left_panel(self) -> None:
        if self.left_panel.winfo_ismapped():
            self.left_panel.pack_forget()
            self.status_var.set("Панель категорій приховано.")
        else:
            self.left_panel.pack(side="left", fill="y", before=self.right_panel)
            self.status_var.set("Панель категорій відображено.")

    def _record_audio_dialog(self) -> None:
        c = self._get_theme_colors()
        dlg = tk.Toplevel(self)
        dlg.title("Записування звуку (Sound Recorder)")
        dlg.geometry("460x260")
        dlg.resizable(False, False)
        dlg.configure(bg=c["dialog_bg"])
        dlg.transient(self)
        self._apply_dark_title_bar(dlg)

        top = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(
            top,
            text="🎙 Записування звуку з мікрофона",
            bg=c["dialog_bg"],
            fg=c["fg"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        status_lbl = tk.Label(
            top,
            text="Готово до запису. Натисніть «Почати запис».",
            bg=c["dialog_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
        )
        status_lbl.pack(anchor="w", pady=(4, 0))

        center = tk.Frame(dlg, bg=c["top_bg"], bd=1, relief="solid", padx=12, pady=12)
        center.pack(fill="x", padx=16, pady=6)
        timer_lbl = tk.Label(
            center,
            text="0:00.0",
            bg=c["top_bg"],
            fg=c["accent"],
            font=("Segoe UI", 20, "bold"),
        )
        timer_lbl.pack()

        recording = False
        start_t = 0.0

        def update_timer() -> None:
            if recording and dlg.winfo_exists():
                dur = time.time() - start_t
                secs = int(dur)
                ms = int((dur - secs) * 10)
                timer_lbl.configure(text=f"{secs // 60}:{secs % 60:02d}.{ms}")
                dlg.after(100, update_timer)

        def start_rec() -> None:
            nonlocal recording, start_t
            recording = True
            start_t = time.time()
            btn_rec.configure(state="disabled")
            btn_stop.configure(state="normal")
            status_lbl.configure(text="Триває запис звуку...", fg="#ff4444")
            update_timer()

        def stop_rec() -> None:
            nonlocal recording
            if not recording:
                return
            recording = False
            btn_rec.configure(state="normal")
            btn_stop.configure(state="disabled")
            btn_save.configure(state="normal")
            status_lbl.configure(text="Запис завершено. Можна зберегти у саундпад.", fg="#00c853")

        def save_rec() -> None:
            rec_dir = MusicPadApp._app_root_dir() / ".cache" / "recordings"
            rec_dir.mkdir(parents=True, exist_ok=True)
            wav_path = rec_dir / f"rec_{int(time.time())}.wav"
            dur = max(0.2, time.time() - start_t if start_t else 1.0)
            _create_wav_file(wav_path, duration_sec=dur)
            t = scan_file(wav_path)
            if t:
                t.title = f"Запис {time.strftime('%H:%M:%S')}"
                self.local_tracks.append(t)
                self._filter_tracks()
                self._save_state()
                self.status_var.set(f"Додано запис у саундпад: {t.title}")
            dlg.destroy()

        btns = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=12)
        btns.pack(fill="x")
        btn_rec = tk.Button(
            btns,
            text="🔴 Почати запис",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground="#ff3333",
            activeforeground="#ffffff",
            bd=1,
            relief="solid",
            cursor="hand2",
            padx=10,
            pady=3,
            command=start_rec,
        )
        btn_rec.pack(side="left")

        btn_stop = tk.Button(
            btns,
            text="⏹ Зупинити",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            padx=10,
            pady=3,
            state="disabled",
            command=stop_rec,
        )
        btn_stop.pack(side="left", padx=8)

        btn_save = tk.Button(
            btns,
            text="💾 Додати в саундпад",
            bg=c["accent"],
            fg="#ffffff",
            activebackground="#00a0f0",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=3,
            state="disabled",
            command=save_rec,
        )
        btn_save.pack(side="right")

    def _color_tool_dialog(self) -> None:
        c = self._get_theme_colors()
        dlg = tk.Toplevel(self)
        dlg.title("Інструмент «Колір» (Theme & Colors)")
        dlg.geometry("400x220")
        dlg.resizable(False, False)
        dlg.configure(bg=c["dialog_bg"])
        dlg.transient(self)
        self._apply_dark_title_bar(dlg)

        top = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(
            top,
            text="🎨 Інструмент вибору теми та кольору",
            bg=c["dialog_bg"],
            fg=c["fg"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")

        mid = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=6)
        mid.pack(fill="x")
        tk.Label(mid, text="Тема інтерфейсу:", bg=c["dialog_bg"], fg=c["muted_fg"], font=("Segoe UI", 9)).pack(anchor="w")
        btn_theme = tk.Button(
            mid,
            text="🌙 Темна тема" if self.theme == "dark" else "☀ Світла тема",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            padx=10,
            pady=3,
            command=lambda: [self._toggle_theme(), btn_theme.configure(text="🌙 Темна тема" if self.theme == "dark" else "☀ Світла тема")],
        )
        btn_theme.pack(anchor="w", pady=(4, 8))

        tk.Label(mid, text="Акцентний колір Soundpad:", bg=c["dialog_bg"], fg=c["muted_fg"], font=("Segoe UI", 9)).pack(anchor="w")
        accents = [
            ("Синій (Soundpad)", "#0084d1"),
            ("Бірюзовий", "#00d4ff"),
            ("Смарагдовий", "#00c853"),
            ("Фіолетовий", "#ab47bc"),
        ]
        acc_frame = tk.Frame(mid, bg=c["dialog_bg"])
        acc_frame.pack(anchor="w", pady=(4, 0))
        for _, col in accents:
            tk.Label(acc_frame, text="  ", bg=col, cursor="hand2", width=3, relief="solid", bd=1).pack(side="left", padx=4)

        b_close = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=10)
        b_close.pack(fill="x", side="bottom")
        tk.Button(
            b_close,
            text="Закрити",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            padx=12,
            pady=2,
            command=dlg.destroy,
        ).pack(side="right")

    def _text_to_speech_dialog(self) -> None:
        c = self._get_theme_colors()
        dlg = tk.Toplevel(self)
        dlg.title("Озвучення тексту (Text-to-Speech)")
        dlg.geometry("500x320")
        dlg.resizable(False, False)
        dlg.configure(bg=c["dialog_bg"])
        dlg.transient(self)
        self._apply_dark_title_bar(dlg)

        top = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=10)
        top.pack(fill="x")
        tk.Label(
            top,
            text="🗣 Синтез мовлення (Text-to-Speech)",
            bg=c["dialog_bg"],
            fg=c["fg"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            top,
            text="Введіть текст для озвучення в мікрофон або динаміки:",
            bg=c["dialog_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 6))

        txt_frame = tk.Frame(dlg, bg=c["divider"], bd=1)
        txt_frame.pack(fill="both", expand=True, padx=16, pady=2)
        txt = tk.Text(
            txt_frame,
            bg=c["entry_bg"],
            fg=c["entry_fg"],
            insertbackground=c["accent"],
            bd=0,
            relief="flat",
            font=("Segoe UI", 10),
            wrap="word",
            height=5,
        )
        txt.pack(fill="both", expand=True, padx=4, pady=4)
        txt.insert("1.0", "Привіт! Це тест озвучення тексту через MusicPad.")

        status_lbl = tk.Label(
            dlg,
            text="Готово до озвучення.",
            bg=c["dialog_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
            anchor="w",
        )
        status_lbl.pack(fill="x", padx=16, pady=(4, 0))

        def synthesize(add_to_list: bool = False) -> None:
            text = txt.get("1.0", "end").strip()
            if not text:
                return
            status_lbl.configure(text="Синтез аудіо...", fg=c["accent"])
            tts_dir = MusicPadApp._app_root_dir() / ".cache" / "tts"
            tts_dir.mkdir(parents=True, exist_ok=True)
            wav_path = tts_dir / f"tts_{int(time.time())}.wav"

            def worker() -> None:
                escaped_text = text.replace("'", "''")
                clean_path = str(wav_path).replace("\\", "/")
                ps_script = (
                    f"Add-Type -AssemblyName System.Speech; "
                    f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.SetOutputToWaveFile('{clean_path}'); "
                    f"$s.Speak('{escaped_text}'); "
                    f"$s.Dispose()"
                )
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], timeout=15)
                except Exception:
                    pass
                if not wav_path.is_file() or wav_path.stat().st_size < 100:
                    _create_wav_file(wav_path, duration_sec=max(1.0, len(text) * 0.08))

                def done() -> None:
                    status_lbl.configure(text="Готово!", fg="#00c853")
                    t = scan_file(wav_path)
                    if t:
                        t.title = f"TTS: {text[:25]}"
                        if add_to_list:
                            self.local_tracks.append(t)
                            self._filter_tracks()
                            self._save_state()
                            self.status_var.set(f"Додано TTS у саундпад: {t.title}")
                            dlg.destroy()
                        else:
                            self._play_local_track(t)

                self._safe_ui_call(done)

            threading.Thread(target=worker, daemon=True).start()

        btns = tk.Frame(dlg, bg=c["dialog_bg"], padx=16, pady=12)
        btns.pack(fill="x")
        tk.Button(
            btns,
            text="▶ Прослухати",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            padx=10,
            pady=3,
            command=lambda: synthesize(add_to_list=False),
        ).pack(side="left")

        tk.Button(
            btns,
            text="💾 Додати в саундпад",
            bg=c["accent"],
            fg="#ffffff",
            activebackground="#00a0f0",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=3,
            command=lambda: synthesize(add_to_list=True),
        ).pack(side="right")

    def _build_top_toolbar(self) -> None:
        c = self._get_theme_colors()
        self.top_bar = tk.Frame(self, bg=c["top_bg"], height=38)
        self.top_bar.pack(fill="x", side="top")
        self.top_bar.pack_propagate(False)

        btn_kw = {
            "bg": c["top_bg"],
            "activebackground": "#333333" if self.theme == "dark" else "#d0d4da",
            "bd": 0,
            "relief": "flat",
            "highlightthickness": 0,
            "cursor": "hand2",
            "fg": c["accent"],
        }

        # 1. Play / Pause / Resume toggle
        self.pause_button = SoundpadButton(
            self.top_bar,
            icon_play=self._icons.get("play"),
            icon_pause=self._icons.get("pause"),
            image=self._icons.get("play"),
            text=self._tr("btn_play") if hasattr(self, "_tr") else "▶ Відтворити",
            command=self._toggle_pause,
            **btn_kw,
        )
        self.pause_button.pack(side="left", padx=(8, 3), pady=4)

        # 5. Stop
        self.btn_tb_stop = tk.Button(
            self.top_bar,
            image=self._icons.get("stop"),
            text="⏹",
            command=self._stop,
            **btn_kw,
        )
        self.btn_tb_stop.pack(side="left", padx=3, pady=4)

        # 6. Prev
        self.btn_tb_prev = tk.Button(
            self.top_bar,
            image=self._icons.get("prev"),
            text="|◀",
            command=self._previous,
            **btn_kw,
        )
        self.btn_tb_prev.pack(side="left", padx=3, pady=4)

        # 7. Next
        self.btn_tb_next = tk.Button(
            self.top_bar,
            image=self._icons.get("next"),
            text="▶|",
            command=self._next,
            **btn_kw,
        )
        self.btn_tb_next.pack(side="left", padx=3, pady=4)

        # Vertical separator between Next and 0:00 (authentic to Soundpad)
        self.top_bar_sep1 = tk.Frame(self.top_bar, width=1, bg=c["divider"], height=18)
        self.top_bar_sep1.pack(side="left", padx=(3, 3), pady=8)

        # 8. Time label (0:00)
        self.time_label = tk.Label(
            self.top_bar,
            textvariable=self.time_display_var,
            text="0:00",
            bg=c["top_bg"],
            fg="#ffffff" if self.theme == "dark" else "#20242b",
            font=("Segoe UI", 9),
            width=5,
            anchor="center",
        )
        self.time_label.pack(side="left", padx=(1, 4), pady=4)

        # 9. Seek scale (Timeline canvas)
        self.seek_scale = SoundpadTimelineSlider(
            self.top_bar,
            variable=self.seek_var,
            to=100.0,
            bg=c["top_bg"],
        )
        self.seek_scale.set_theme(self.theme)
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=(2, 6), pady=4)
        self.seek_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self.seek_scale.bind("<B1-Motion>", self._on_seek_motion)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        # 10. Vertical separator
        self.top_bar_sep2 = tk.Frame(self.top_bar, width=1, bg=c["divider"], height=18)
        self.top_bar_sep2.pack(side="left", padx=(2, 6), pady=8)

        # 11. Mute button (musical note without X when sound is on, musical note with red X when muted/off)
        init_icon = (
            self._icons.get("note_mute") or self._icons.get("mute")
            if float(self.volume_var.get()) <= 0.001
            else (self._icons.get("note") or self._icons.get("headphones"))
        )
        self.btn_tb_mute = tk.Button(
            self.top_bar,
            image=init_icon,
            text="",
            command=self._toggle_mute,
            **btn_kw,
        )
        self.btn_tb_mute.pack(side="left", padx=(2, 4), pady=4)
        self.volume_var.trace_add("write", lambda *_: self._update_mute_icon())

        # 12. Volume scale (custom slider canvas)
        self.volume_scale = SoundpadVolumeSlider(
            self.top_bar,
            variable=self.volume_var,
            command=self._volume_changed,
            bg=c["top_bg"],
        )
        self.volume_scale.set_theme(self.theme)
        self.volume_scale.pack(side="left", padx=(2, 10), pady=4)

        # Underline below toolbar
        self.top_bar_divider = tk.Frame(self, height=1, bg=c["divider"])
        self.top_bar_divider.pack(fill="x", side="top")

    def _build_search_bar(self) -> None:
        c = self._get_theme_colors()
        self.search_bar_frame = tk.Frame(self, bg=c["hdr_bg"], height=30)
        self.search_bar_lbl = tk.Label(
            self.search_bar_frame,
            text="🔍 Пошук у звуках:",
            bg=c["hdr_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
        )
        self.search_bar_lbl.pack(side="left", padx=(10, 6), pady=3)
        self.search_entry = ttk.Entry(self.search_bar_frame, textvariable=self.search_var, width=35)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=4, pady=3)
        self.search_var.trace_add("write", lambda *_: self._filter_tracks())
        self.btn_close_search = tk.Button(
            self.search_bar_frame,
            text="✖",
            bg=c["hdr_bg"],
            activebackground="#3d3d3d" if self.theme == "dark" else "#d0d4da",
            fg=c["muted_fg"],
            bd=0,
            relief="flat",
            command=self._close_search_bar,
            cursor="hand2",
        )
        self.btn_close_search.pack(side="right", padx=(4, 10), pady=3)

    def _build_main_split(self) -> None:
        c = self._get_theme_colors()
        self.main_split = tk.Frame(self, bg=c["bg"])
        self.main_split.pack(fill="both", expand=True, side="top")

        # Left panel: Categories (extends all the way down to bottom toolbar)
        self.left_panel = tk.Frame(self.main_split, width=215, bg=c["panel_bg"])
        self.left_panel.pack(side="left", fill="y")
        self.left_panel.pack_propagate(False)

        # Category Header
        self.cat_hdr = tk.Frame(self.left_panel, height=26, bg=c["hdr_bg"])
        self.cat_hdr.pack(fill="x")
        self.cat_hdr.pack_propagate(False)
        self.cat_hdr_lbl1 = tk.Label(
            self.cat_hdr,
            text="Категорія",
            bg=c["hdr_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
        )
        self.cat_hdr_lbl1.pack(side="left", padx=8, pady=4)
        self.cat_hdr_lbl2 = tk.Label(
            self.cat_hdr,
            text="#",
            bg=c["hdr_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
        )
        self.cat_hdr_lbl2.pack(side="right", padx=12, pady=4)
        self.cat_hdr_div = tk.Frame(self.left_panel, height=1, bg=c["split_divider"])
        self.cat_hdr_div.pack(fill="x")

        # Category Item 1 (Мої звуки)
        self.cat_item_frame = tk.Frame(self.left_panel, height=28, bg=c["cat_sel_bg"], cursor="hand2")
        self.cat_item_frame.pack(fill="x")
        self.cat_item_frame.pack_propagate(False)

        self.cat_icon_lbl = tk.Label(
            self.cat_item_frame,
            image=self._icons.get("house_sm"),
            text="🏠",
            bg=c["cat_sel_bg"],
            fg=c["accent"],
        )
        self.cat_icon_lbl.pack(side="left", padx=(8, 6), pady=4)

        self.cat_title_lbl = tk.Label(
            self.cat_item_frame,
            text="Мої звуки",
            bg=c["cat_sel_bg"],
            fg=c["cat_sel_fg"],
            font=("Segoe UI", 9, "bold"),
        )
        self.cat_title_lbl.pack(side="left", pady=4)

        self.category_count_lbl = tk.Label(
            self.cat_item_frame,
            text="0",
            bg=c["cat_sel_bg"],
            fg=c["cat_sel_fg"],
            font=("Segoe UI", 9),
        )
        self.category_count_lbl.pack(side="right", padx=12, pady=4)

        # Category Item 2 (YouTube / YouTube Music)
        self.cat_yt_frame = tk.Frame(self.left_panel, height=28, bg=c["cat_unsel_bg"], cursor="hand2")
        self.cat_yt_frame.pack(fill="x")
        self.cat_yt_frame.pack_propagate(False)

        self.cat_yt_icon_lbl = tk.Label(
            self.cat_yt_frame,
            image=self._icons.get("yt_icon"),
            text="▶",
            bg=c["cat_unsel_bg"],
            fg="#ff1f1f",
        )
        self.cat_yt_icon_lbl.pack(side="left", padx=(8, 6), pady=4)

        self.cat_yt_title_lbl = tk.Label(
            self.cat_yt_frame,
            text="YouTube / YouTube Music",
            bg=c["cat_unsel_bg"],
            fg=c["cat_unsel_fg"],
            font=("Segoe UI", 9),
        )
        self.cat_yt_title_lbl.pack(side="left", pady=4)

        self.cat_yt_count_lbl = tk.Label(
            self.cat_yt_frame,
            text="🔍",
            bg=c["cat_unsel_bg"],
            fg=c["cat_unsel_fg"],
            font=("Segoe UI", 9),
        )
        self.cat_yt_count_lbl.pack(side="right", padx=12, pady=4)

        # Context menu for categories
        self._cat_menu = tk.Menu(self, tearoff=0)
        self._cat_menu.add_command(label="➕ Додати аудіофайли...", command=self._add_files)
        self._cat_menu.add_command(label="📁 Вибрати папку...", command=self._choose_folder)
        self._cat_menu.add_command(label="🔍 Пошук YouTube...", command=self._open_yt_search)

        for w in (self.cat_item_frame, self.cat_icon_lbl, self.cat_title_lbl, self.category_count_lbl):
            w.bind("<Button-1>", lambda _: self._on_category_select("my_sounds"))
            w.bind("<Enter>", lambda _: self._on_cat_hover("my_sounds", True))
            w.bind("<Leave>", lambda _: self._on_cat_hover("my_sounds", False))
            w.bind("<Button-3>", lambda e: self._cat_menu.tk_popup(e.x_root, e.y_root))

        for w in (self.cat_yt_frame, self.cat_yt_icon_lbl, self.cat_yt_title_lbl, self.cat_yt_count_lbl):
            w.bind("<Button-1>", lambda _: self._on_category_select("youtube"))
            w.bind("<Enter>", lambda _: self._on_cat_hover("youtube", True))
            w.bind("<Leave>", lambda _: self._on_cat_hover("youtube", False))
            w.bind("<Button-3>", lambda e: self._cat_menu.tk_popup(e.x_root, e.y_root))

        self.left_panel.bind("<Button-3>", lambda e: self._cat_menu.tk_popup(e.x_root, e.y_root))

        # Vertical Divider between Categories and Table
        self.split_divider = tk.Frame(self.main_split, width=1, bg=c["split_divider"])
        self.split_divider.pack(side="left", fill="y")

        # Right panel: Table + Info Panel at bottom
        self.right_panel = tk.Frame(self.main_split, bg=c["table_bg"])
        self.right_panel.pack(side="left", fill="both", expand=True)

        # Table area inside right panel
        table_container = tk.Frame(self.right_panel, bg=c["table_bg"])
        table_container.pack(fill="both", expand=True, side="top")

        self.track_list = ttk.Treeview(
            table_container,
            columns=("#", "index", "title", "duration", "hotkey"),
            show="headings",
            selectmode="browse",
        )
        self.track_list.heading("#", text="#")
        self.track_list.heading("index", text="Інд...")
        self.track_list.heading("title", text="Тег")
        self.track_list.heading("duration", text="Тр...")
        self.track_list.heading("hotkey", text="Сполучення ...")

        self.track_list.column("#", width=26, minwidth=22, stretch=False, anchor="center")
        self.track_list.column("index", width=46, minwidth=35, stretch=False, anchor="center")
        self.track_list.column("title", width=420, minwidth=150, stretch=True, anchor="w")
        self.track_list.column("duration", width=65, minwidth=50, stretch=False, anchor="center")
        self.track_list.column("hotkey", width=130, minwidth=80, stretch=False, anchor="center")

        scrollbar = ttk.Scrollbar(table_container, orient="vertical", command=self.track_list.yview)
        self.track_list.configure(yscrollcommand=scrollbar.set)

        self.track_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.track_list.bind("<Double-1>", lambda _: self._play_selected())
        self.track_list.bind("<Return>", lambda _: self._play_selected())
        self.track_list.bind("<Delete>", lambda _: self._remove_selected_track())
        self.track_list.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.table_container = table_container
        self._build_context_menu()
        self._build_yt_search_panel(table_container)

        # Build bottom info panel inside right panel!
        self._build_info_panel()

    def _build_info_panel(self) -> None:
        c = self._get_theme_colors()
        # Divider line between table and info panel
        self.info_panel_divider = tk.Frame(self.right_panel, height=1, bg=c["divider"])
        self.info_panel_divider.pack(fill="x", side="top")

        self.info_panel = tk.Frame(self.right_panel, bg=c["info_bg"], height=56)
        self.info_panel.pack(fill="x", side="bottom")
        self.info_panel.pack_propagate(False)

        # Left: Large House Icon
        self.info_house_lbl = tk.Label(
            self.info_panel,
            image=self._icons.get("house_lg"),
            text="🏠",
            bg=c["info_bg"],
            fg=c["accent"],
        )
        self.info_house_lbl.pack(side="left", padx=(14, 10), pady=8)

        # Category Name
        self.info_cat_name_lbl = tk.Label(
            self.info_panel,
            text=self._tr("cat_my_sounds"),
            bg=c["info_bg"],
            fg=c["fg"],
            font=("Segoe UI", 10, "bold"),
        )
        self.info_cat_name_lbl.pack(side="left", padx=(0, 20), pady=8)

        # Stats Container (2 rows)
        self.stats_frame = tk.Frame(self.info_panel, bg=c["info_bg"])
        self.stats_frame.pack(side="left", pady=8)

        self.stats_caption_labels: list[tk.Label] = []

        # Row 1: Звуки & Лічильник відтворень
        self.stats_r1 = tk.Frame(self.stats_frame, bg=c["info_bg"])
        self.stats_r1.pack(anchor="w", fill="x")
        self.lbl_stats_sounds = tk.Label(self.stats_r1, text=self._tr("info_sounds"), bg=c["info_bg"], fg=c["muted_fg"], font=("Segoe UI", 9))
        self.lbl_stats_sounds.pack(side="left")
        self.stats_caption_labels.append(self.lbl_stats_sounds)

        self.stats_sounds_lbl = tk.Label(self.stats_r1, text="0", bg=c["info_bg"], fg=c["fg"], font=("Segoe UI", 9))
        self.stats_sounds_lbl.pack(side="left", padx=(0, 28))

        self.lbl_stats_plays = tk.Label(self.stats_r1, text=self._tr("info_plays"), bg=c["info_bg"], fg=c["muted_fg"], font=("Segoe UI", 9))
        self.lbl_stats_plays.pack(side="left")
        self.stats_caption_labels.append(self.lbl_stats_plays)

        self.stats_plays_lbl = tk.Label(self.stats_r1, text="0", bg=c["info_bg"], fg=c["fg"], font=("Segoe UI", 9))
        self.stats_plays_lbl.pack(side="left")

        # Row 2: Вибрано
        self.stats_r2 = tk.Frame(self.stats_frame, bg=c["info_bg"])
        self.stats_r2.pack(anchor="w", fill="x", pady=(2, 0))
        self.lbl_stats_sel = tk.Label(self.stats_r2, text=self._tr("info_selected"), bg=c["info_bg"], fg=c["muted_fg"], font=("Segoe UI", 9))
        self.lbl_stats_sel.pack(side="left")
        self.stats_caption_labels.append(self.lbl_stats_sel)

        self.stats_selected_lbl = tk.Label(self.stats_r2, text="0", bg=c["info_bg"], fg=c["fg"], font=("Segoe UI", 9))
        self.stats_selected_lbl.pack(side="left")

        # Far right: Blue Wrench button (Settings)
        self.btn_wrench = tk.Button(
            self.info_panel,
            image=self._icons.get("wrench"),
            text="🔧",
            bg=c["info_bg"],
            activebackground="#383838" if self.theme == "dark" else "#d0d4da",
            bd=0,
            relief="flat",
            highlightthickness=0,
            cursor="hand2",
            fg=c["accent"],
            command=self._open_settings,
        )
        self.btn_wrench.pack(side="right", padx=16, pady=10)

    def _build_bottom_toolbar(self) -> None:
        c = self._get_theme_colors()
        self.bottom_bar_divider = tk.Frame(self, height=1, bg=c["divider"])
        self.bottom_bar_divider.pack(fill="x", side="top")

        self.bottom_bar = tk.Frame(self, bg=c["bottom_bg"], height=28)
        self.bottom_bar.pack(fill="x", side="top")
        self.bottom_bar.pack_propagate(False)

        btn_kw = {
            "bg": c["bottom_bg"],
            "activebackground": "#2c2c2c" if self.theme == "dark" else "#d0d4da",
            "bd": 0,
            "relief": "flat",
            "highlightthickness": 0,
            "cursor": "hand2",
            "fg": c["accent"],
        }
        self.bottom_bar_seps: list[tk.Frame] = []

        # 1. Play button (small)
        self.btn_b_play = tk.Button(
            self.bottom_bar,
            image=self._icons.get("play_sm"),
            text="▶",
            command=self._play_selected,
            **btn_kw,
        )
        self.btn_b_play.pack(side="left", padx=(8, 3), pady=4)

        # 2. Separator |
        sep1 = tk.Frame(self.bottom_bar, width=1, bg=c["divider"], height=16)
        sep1.pack(side="left", padx=(3, 3), pady=6)
        self.bottom_bar_seps.append(sep1)

        # 3. H button (Hotkeys toggle)
        self.btn_b_h = tk.Button(
            self.bottom_bar,
            image=self._icons.get("btn_h"),
            text="H",
            command=self._toggle_hotkeys_active,
            **btn_kw,
        )
        self.btn_b_h.pack(side="left", padx=2, pady=4)

        # 4. A button (Auto-keys toggle)
        self.btn_b_a = tk.Button(
            self.bottom_bar,
            image=self._icons.get("btn_a"),
            text="A",
            command=self._toggle_auto_keys,
            **btn_kw,
        )
        self.btn_b_a.pack(side="left", padx=2, pady=4)

        # 5. Vertical separator
        sep2 = tk.Frame(self.bottom_bar, width=1, bg=c["divider"], height=16)
        sep2.pack(side="left", padx=6, pady=6)
        self.bottom_bar_seps.append(sep2)

        # 6. Spacer between left and right controls (status_label completely removed)
        self.bottom_bar_spacer = tk.Frame(self.bottom_bar, bg=c["bottom_bg"])
        self.bottom_bar_spacer.pack(side="left", fill="x", expand=True)

        # 7. Right side controls
        # Notification speech bubble toggle button
        self.btn_b_msg = tk.Button(
            self.bottom_bar,
            image=self._icons.get("chat"),
            text="💬",
            command=self._toggle_notification_bubble,
            **btn_kw,
        )
        self.btn_b_msg.pack(side="right", padx=(2, 8), pady=4)
        if hasattr(self, "notification_manager"):
            self.notification_manager.set_target_widget(self.btn_b_msg)

        # Vertical separator between FF and Chat (authentic!)
        sep3 = tk.Frame(self.bottom_bar, width=1, bg=c["divider"], height=16)
        sep3.pack(side="right", padx=(3, 3), pady=6)
        self.bottom_bar_seps.append(sep3)

        # Fast forward (▶▶)
        self.btn_b_ff = tk.Button(
            self.bottom_bar,
            image=self._icons.get("ff"),
            text="▶▶",
            command=self._seek_forward,
            **btn_kw,
        )
        self.btn_b_ff.pack(side="right", padx=2, pady=4)

        # Loop (🔁)
        self.btn_b_loop = tk.Button(
            self.bottom_bar,
            image=self._icons.get("loop"),
            text="🔁",
            command=self._toggle_loop,
            **btn_kw,
        )
        self.btn_b_loop.pack(side="right", padx=2, pady=4)

        # Vertical separator before Loop
        sep4 = tk.Frame(self.bottom_bar, width=1, bg=c["divider"], height=16)
        sep4.pack(side="right", padx=6, pady=6)
        self.bottom_bar_seps.append(sep4)

    def _init_settings_variables(self) -> None:
        if hasattr(self, "_settings_initialized") and self._settings_initialized:
            return
        self._settings_initialized = True

        # Audio Tab
        self.norm_volume_val = tk.StringVar(value="89,0 дБ")
        self.norm_mode_var = tk.StringVar(value="dynamic")

        # Hotkeys Tab
        self.special_hotkeys_dict: dict[str, str] = {
            "Зупинити відтворення": "",
            "Призупинити/продовжити відтворення": "Space",
            "Відтворити попередній файл": "",
            "Відтворити наступний файл": "",
            "Відтворити вибраний файл": "",
            "Відтворити випадковий файл": "",
            "Відтворити випадковий файл із усіх категорій": "",
            "Зберегти список звуків": "Ctrl+S",
            "Скасувати": "Ctrl+Z",
            "Відновити": "Ctrl+Y",
            "Вирізати": "Ctrl+X",
            "Копіювати": "Ctrl+C",
            "Вставити": "Ctrl+V",
            "Видалити вибрані записи": "Del",
            "Вибрати все": "Ctrl+A",
            "Пошук": "Ctrl+F",
            "Записування звуку": "Ctrl+R",
            "Озвучення тексту": "Ctrl+T",
            "Вихід": "Alt+F4",
        }
        self.numpad_alt_var = tk.BooleanVar(value=False)
        self.numpad_ctrl_alt_var = tk.BooleanVar(value=False)
        self.auto_keys_list: list[tuple[str, str]] = []
        self.pass_hotkeys_var = tk.BooleanVar(value=False)
        if not hasattr(self, "auto_stop_var"):
            self.auto_stop_var = tk.BooleanVar(value=True)
        self.allow_lr_modifiers_var = tk.BooleanVar(value=False)
        self.same_hotkeys_all_categories_var = tk.BooleanVar(value=False)

        # Interface Tab
        self.lang_var = tk.StringVar(value="За замовчуванням")
        self.date_format_var = tk.StringVar(value="Мова за замовчуванням")
        self.tb_style_var = tk.StringVar(value="Темний")
        self.tb_scale_var = tk.StringVar(value="100%")
        self.tb_size_var = tk.StringVar(value="Малий")
        self.tb_color_var = tk.StringVar(value="Синій")
        self.pos_refresh_rate_var = tk.IntVar(value=60)
        self.show_mic_level_var = tk.BooleanVar(value=False)
        self.linear_volume_slider_var = tk.BooleanVar(value=True)
        self.allow_resume_other_modes_var = tk.BooleanVar(value=False)
        self.hotkey_switch_mode_var = tk.StringVar(value="«за замовчуванням» і динаміками")
        self.launch_on_startup_var = tk.BooleanVar(value=False)
        self.start_minimized_var = tk.BooleanVar(value=False)
        self.show_tray_icon_var = tk.BooleanVar(value=True)
        self.minimize_to_tray_var = tk.BooleanVar(value=False)
        self.minimize_instead_of_close_var = tk.BooleanVar(value=False)
        self.always_on_top_var = tk.BooleanVar(value=False)
        self.disable_track_sorting_var = tk.BooleanVar(value=False)
        self.alternate_row_colors_var = tk.BooleanVar(value=False)
        self.typing_starts_search_var = tk.BooleanVar(value=True)
        self.tts_input_priority_var = tk.BooleanVar(value=False)
        self.remember_playback_state_var = tk.BooleanVar(value=True)

        # Devices Tab
        self.check_device_config_on_launch_var = tk.BooleanVar(value=True)
        self.recording_devices_checked: dict[str, bool] = {}
        if not hasattr(self, "track_source_var"):
            self.track_source_var = tk.StringVar(value=DEFAULT_TRACK_SOURCE)

    def _create_scrollable_tab(self, notebook: ttk.Notebook, tab_title: str) -> ttk.Frame:
        tab_container = ttk.Frame(notebook)
        notebook.add(tab_container, text=tab_title)

        c = self._get_theme_colors()
        canvas = tk.Canvas(tab_container, highlightthickness=0, bd=0, bg=c["bg"])
        scrollbar = ttk.Scrollbar(tab_container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content_frame = ttk.Frame(canvas, padding=6)
        content_win = canvas.create_window((0, 0), window=content_frame, anchor="nw")

        def _on_frame_configure(_event: Any) -> None:
            if canvas.winfo_exists():
                bbox = canvas.bbox("all")
                canvas.configure(scrollregion=bbox)
                if bbox and (bbox[3] - bbox[1]) <= max(canvas.winfo_height(), 10):
                    scrollbar.pack_forget()
                else:
                    if scrollbar.winfo_manager() != "pack":
                        scrollbar.pack(side="right", fill="y")

        def _on_canvas_configure(event: Any) -> None:
            if canvas.winfo_exists():
                canvas.itemconfig(content_win, width=event.width)
                bbox = canvas.bbox("all")
                if bbox and (bbox[3] - bbox[1]) <= max(canvas.winfo_height(), 10):
                    scrollbar.pack_forget()
                elif bbox and (bbox[3] - bbox[1]) > canvas.winfo_height():
                    if scrollbar.winfo_manager() != "pack":
                        scrollbar.pack(side="right", fill="y")

        content_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        if not hasattr(self, "_settings_canvases"):
            self._settings_canvases = []
        self._settings_canvases.append(canvas)
        tab_container._canvas = canvas
        content_frame._canvas = canvas
        content_frame._scrollbar = scrollbar

        return content_frame

    def _build_settings_dialog(self) -> None:
        self._init_settings_variables()
        self._settings_window = tk.Toplevel(self)
        self._settings_window.title(self._tr("settings_title") if hasattr(self, "_tr") else "Налаштування")
        self._settings_window.geometry("640x760")
        self._settings_window.minsize(560, 600)
        c = self._get_theme_colors()
        self._settings_window.configure(bg=c["bg"])
        self._settings_window.transient(self)
        self._settings_window.withdraw()
        self._settings_window.protocol("WM_DELETE_WINDOW", self._cancel_settings)
        self._apply_dark_title_bar(self._settings_window)

        # Pin bottom action button bar firmly to bottom outside scrollable area first
        self._build_settings_bottom_bar()

        # Notebook fills the remaining space above the bottom bar
        self.settings_notebook = ttk.Notebook(self._settings_window)
        self.settings_notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self._settings_canvases = []
        self.tab_audio = self._create_scrollable_tab(self.settings_notebook, self._tr("tab_audio") if hasattr(self, "_tr") else "Аудіо")
        self.tab_hotkeys = self._create_scrollable_tab(self.settings_notebook, self._tr("tab_hotkeys") if hasattr(self, "_tr") else "Гарячі клавіші")
        self.tab_interface = self._create_scrollable_tab(self.settings_notebook, self._tr("tab_interface") if hasattr(self, "_tr") else "Інтерфейс")
        self.tab_devices = self._create_scrollable_tab(self.settings_notebook, self._tr("tab_devices") if hasattr(self, "_tr") else "Пристрої")

        self._build_tab_audio()
        self._build_tab_hotkeys()
        self._build_tab_interface()
        self._build_tab_devices()

        # Dynamic mousewheel scrolling on the settings window
        def _on_settings_mousewheel(event: tk.Event) -> None:
            try:
                sel = self.settings_notebook.select()
                if sel:
                    tab_widget = self.nametowidget(sel)
                    cnv = getattr(tab_widget, "_canvas", None)
                    if cnv and cnv.winfo_exists():
                        widget_under_mouse = self._settings_window.winfo_containing(event.x_root, event.y_root)
                        if isinstance(widget_under_mouse, ttk.Treeview):
                            return
                        bbox = cnv.bbox("all")
                        if bbox and (bbox[3] - bbox[1]) > cnv.winfo_height():
                            delta = -1 if event.delta > 0 else 1
                            cnv.yview_scroll(delta * 2, "units")
            except Exception:
                pass

        def _on_settings_mousewheel_up(event: tk.Event) -> None:
            _on_settings_mousewheel(type("Event", (), {"delta": 120, "x_root": event.x_root, "y_root": event.y_root})())

        def _on_settings_mousewheel_down(event: tk.Event) -> None:
            _on_settings_mousewheel(type("Event", (), {"delta": -120, "x_root": event.x_root, "y_root": event.y_root})())

        self._settings_window.bind("<MouseWheel>", _on_settings_mousewheel)
        self._settings_window.bind("<Button-4>", _on_settings_mousewheel_up)
        self._settings_window.bind("<Button-5>", _on_settings_mousewheel_down)

    def _build_tab_audio(self) -> None:
        f = self.tab_audio
        for child in f.winfo_children():
            child.destroy()

        c = self._get_theme_colors()

        # Group 1: Volume normalization / Нормалізація гучності
        g1 = ttk.LabelFrame(
            f,
            text=self._tr("grp_audio_norm") if hasattr(self, "_tr") else "Нормалізація гучності",
            padding=8,
        )
        g1.pack(fill="x", pady=(2, 6))

        ttk.Label(
            g1,
            text=self._tr("lbl_audio_norm_desc")
            if hasattr(self, "_tr")
            else "Налаштовує гучність відтворюваного аудиофайлу так, щоб інші люди чули ваш голос і ваші звуки з однаковою гучністю.",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        row_norm = ttk.Frame(g1)
        row_norm.pack(fill="x", pady=2)
        ttk.Label(
            row_norm,
            text=self._tr("lbl_audio_voice_vol") if hasattr(self, "_tr") else "Гучність голосу: ",
        ).pack(side="left")
        cb_norm = ttk.Combobox(
            row_norm,
            textvariable=self.norm_volume_val,
            values=["86,0 дБ", "87,0 дБ", "88,0 дБ", "89,0 дБ", "90,0 дБ", "91,0 дБ", "92,0 дБ"],
            state="readonly",
            width=10,
        )
        cb_norm.pack(side="left", padx=(0, 4))

        self.c_ramp = tk.Canvas(row_norm, width=20, height=18, bg=c["table_bg"], highlightthickness=1, highlightbackground=c["divider"])
        self.c_ramp.create_polygon([(2, 15), (18, 3), (18, 15)], fill="#0084d1", outline="")
        self.c_ramp.pack(side="left")

        r1_frame = ttk.Frame(g1)
        r1_frame.pack(fill="x", pady=(4, 1))
        ttk.Radiobutton(
            r1_frame,
            text=self._tr("lbl_audio_dynamic")
            if hasattr(self, "_tr")
            else "Динамічне регулювання гучності (рекомендоване)",
            variable=self.norm_mode_var,
            value="dynamic",
        ).pack(side="left")
        ttk.Button(
            r1_frame,
            text="?",
            width=2,
            command=lambda: self._show_settings_hint(
                self._tr("hint_dynamic_title") if hasattr(self, "_tr") else "Динамічне регулювання",
                self._tr("hint_dynamic_desc")
                if hasattr(self, "_tr")
                else "Динамічне регулювання аналізує амплітуду аудіо та автоматично вирівнює гучність до обраного рівня.",
            ),
        ).pack(side="left", padx=6)

        r2_frame = ttk.Frame(g1)
        r2_frame.pack(fill="x", pady=1)
        ttk.Radiobutton(
            r2_frame,
            text=self._tr("lbl_audio_fixed")
            if hasattr(self, "_tr")
            else "Фіксоване регулювання гучності (додатково)",
            variable=self.norm_mode_var,
            value="fixed",
        ).pack(side="left")
        ttk.Button(
            r2_frame,
            text="?",
            width=2,
            command=lambda: self._show_settings_hint(
                self._tr("hint_fixed_title") if hasattr(self, "_tr") else "Фіксоване регулювання",
                self._tr("hint_fixed_desc")
                if hasattr(self, "_tr")
                else "Фіксоване регулювання застосовує постійне статичне підсилення або послаблення аудіо.",
            ),
        ).pack(side="left", padx=6)

        if hasattr(f, "_canvas") and f._canvas.winfo_exists():
            f.update_idletasks()
            bbox = f._canvas.bbox("all")
            if bbox:
                f._canvas.configure(scrollregion=bbox)
            sb = getattr(f, "_scrollbar", None)
            if sb and sb.winfo_exists():
                if bbox and (bbox[3] - bbox[1]) <= max(f._canvas.winfo_height(), 10):
                    sb.pack_forget()

    def _build_tab_hotkeys(self) -> None:
        f = self.tab_hotkeys

        # Group 1: Спеціальні гарячі клавіші
        g1 = ttk.LabelFrame(f, text="Спеціальні гарячі клавіші", padding=8)
        g1.pack(fill="x", pady=(0, 6))

        tree_frame = ttk.Frame(g1)
        tree_frame.pack(fill="x", expand=True)

        self.special_hotkeys_tree = ttk.Treeview(
            tree_frame,
            columns=("action", "shortcut"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        self.special_hotkeys_tree.heading("action", text="Дія")
        self.special_hotkeys_tree.heading("shortcut", text="Сполучення клавіш")
        self.special_hotkeys_tree.column("action", width=340, anchor="w")
        self.special_hotkeys_tree.column("shortcut", width=160, anchor="w")

        sb_tree = ttk.Scrollbar(tree_frame, orient="vertical", command=self.special_hotkeys_tree.yview)
        self.special_hotkeys_tree.configure(yscrollcommand=sb_tree.set)
        self.special_hotkeys_tree.pack(side="left", fill="x", expand=True)
        sb_tree.pack(side="right", fill="y")

        btns_sh = ttk.Frame(g1)
        btns_sh.pack(fill="x", pady=(6, 2))
        ttk.Button(btns_sh, text="Встановити гарячу клавішу", command=self._set_special_hotkey).pack(side="left", padx=(0, 6))
        ttk.Button(btns_sh, text="Змінити значення", command=self._change_special_hotkey_value).pack(side="left")

        # Group 2: Гарячі клавіші цифрового блоку
        g2 = ttk.LabelFrame(f, text="Гарячі клавіші цифрового блоку", padding=8)
        g2.pack(fill="x", pady=(0, 6))

        row_np1 = ttk.Frame(g2)
        row_np1.pack(fill="x", pady=2)
        ttk.Checkbutton(
            row_np1,
            text="Дозволити комбінації клавіш «Alt + цифровий код»",
            variable=self.numpad_alt_var,
        ).pack(side="left")
        ttk.Button(
            row_np1,
            text="Змінити модифікатор",
            command=lambda: self._show_settings_hint("Модифікатор", "Поточний модифікатор цифрового блоку: Alt"),
        ).pack(side="right")

        row_np2 = ttk.Frame(g2)
        row_np2.pack(fill="x", pady=2)
        ttk.Checkbutton(
            row_np2,
            text="Дозволити комбінації клавіш «Ctrl+Alt + індекс»",
            variable=self.numpad_ctrl_alt_var,
        ).pack(side="left")
        ttk.Button(
            row_np2,
            text="Змінити модифікатор",
            command=lambda: self._show_settings_hint("Модифікатор", "Поточний модифікатор цифрового блоку: Ctrl+Alt"),
        ).pack(side="right")

        # Group 3: Автоклавіші
        g3 = ttk.LabelFrame(f, text="Автоклавіші", padding=8)
        g3.pack(fill="x", pady=(0, 6))
        ttk.Label(g3, text="Автоматично натискає клавіші під час відтворення звуків.").pack(anchor="w", pady=(0, 4))

        ak_frame = ttk.Frame(g3)
        ak_frame.pack(fill="x")
        self.auto_keys_tree = ttk.Treeview(
            ak_frame,
            columns=("key", "type"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        self.auto_keys_tree.heading("key", text="Клавіша")
        self.auto_keys_tree.heading("type", text="Тип")
        self.auto_keys_tree.column("key", width=220, anchor="w")
        self.auto_keys_tree.column("type", width=280, anchor="w")
        self.auto_keys_tree.pack(fill="x", expand=True)

        btns_ak = ttk.Frame(g3)
        btns_ak.pack(fill="x", pady=(6, 2))
        ttk.Button(btns_ak, text="Додати", command=self._add_auto_key).pack(side="left", padx=(0, 6))
        ttk.Button(btns_ak, text="Редагувати", command=self._edit_auto_key).pack(side="left", padx=(0, 6))
        ttk.Button(btns_ak, text="Видалити", command=self._remove_auto_key).pack(side="left")

        # Group 4: Розширені параметри
        g4 = ttk.LabelFrame(f, text="Розширені параметри", padding=8)
        g4.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(g4, text="Передавати гарячі клавіші", variable=self.pass_hotkeys_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(g4, text="Автозупинка (рекомендовано)", variable=self.auto_stop_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(g4, text="Дозволити ліві та праві модифікатори", variable=self.allow_lr_modifiers_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(g4, text="Однакові гарячі клавіші в усіх категоріях", variable=self.same_hotkeys_all_categories_var).pack(anchor="w", pady=1)

    def _build_tab_interface(self) -> None:
        f = self.tab_interface
        for child in f.winfo_children():
            child.destroy()

        is_en = self._is_english()

        # Group 1: Загальне / General (formerly "Мова")
        self.grp_general = ttk.LabelFrame(f, text=self._tr("grp_general"), padding=10)
        self.grp_general.pack(fill="x", pady=(2, 8))

        r_l1 = ttk.Frame(self.grp_general)
        r_l1.pack(fill="x", pady=4)
        self.lbl_lang = ttk.Label(r_l1, text=self._tr("lbl_language"), width=16, anchor="w")
        self.lbl_lang.pack(side="left")
        lang_vals = (
            ["За замовчуванням", "Українська", "English"]
            if self.lang_var.get() in ("За замовчуванням", "Default")
            else ["Українська", "English"]
        )
        self.cb_lang = ttk.Combobox(
            r_l1,
            textvariable=self.lang_var,
            values=lang_vals,
            state="readonly",
            width=28,
        )
        self.cb_lang.pack(side="left")

        # Group 2: Панель інструментів / Toolbar
        self.grp_toolbar = ttk.LabelFrame(f, text=self._tr("grp_toolbar"), padding=10)
        self.grp_toolbar.pack(fill="x", pady=(0, 8))

        rw = ttk.Frame(self.grp_toolbar)
        rw.pack(fill="x", pady=4)
        self.lbl_style = ttk.Label(rw, text=self._tr("lbl_style"), width=16, anchor="w")
        self.lbl_style.pack(side="left")

        curr_style = self.tb_style_var.get()
        if is_en and curr_style in ("Темний", "Dark"):
            self.tb_style_var.set("Dark")
        elif is_en and curr_style in ("Світлий", "Light"):
            self.tb_style_var.set("Light")
        elif not is_en and curr_style in ("Dark", "Темний"):
            self.tb_style_var.set("Темний")
        elif not is_en and curr_style in ("Light", "Світлий"):
            self.tb_style_var.set("Світлий")

        style_vals = ["Dark", "Light"] if is_en else ["Темний", "Світлий"]
        self.cb_tb_style = ttk.Combobox(
            rw,
            textvariable=self.tb_style_var,
            values=style_vals,
            state="readonly",
            width=28,
        )
        self.cb_tb_style.pack(side="left")

        # Group 3: Різне / Miscellaneous (Group 3 Режим відтворення removed completely)
        self.grp_misc = ttk.LabelFrame(f, text=self._tr("grp_misc"), padding=10)
        self.grp_misc.pack(fill="x", pady=(0, 8))

        self.chk_remember_playback = ttk.Checkbutton(
            self.grp_misc,
            text=self._tr("chk_remember_playback"),
            variable=self.remember_playback_state_var,
        )
        self.chk_remember_playback.pack(anchor="w", pady=4)

    def _get_track_sources(self) -> list[str]:
        sources = [DEFAULT_TRACK_SOURCE]
        if hasattr(self, "input_map"):
            for name in self.input_map:
                sources.append(f"Вхідний канал: {name}")
        return sources

    def _track_source_changed(self, _event: Optional[tk.Event] = None) -> None:
        src = self.track_source_var.get()
        self.status_var.set(f"Джерело треків: {src}")
        self._save_state()

    def _build_tab_devices(self) -> None:
        f = self.tab_devices
        self.rec_devices_tree = None

        container = ttk.Frame(f)
        container.pack(fill="both", expand=True, padx=8, pady=6)

        # 1. Фізичний мікрофон (Вхід)
        g_mic = ttk.LabelFrame(container, text="1. Фізичний мікрофон (Вхід)", padding=8)
        g_mic.pack(fill="x", pady=(0, 6))

        ttk.Label(
            g_mic,
            text="Пристрій захоплення мови (фізичний мікрофон / гарнітура):\nСюди надходить голос користувача для трансляції у Discord та ігри.",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        r_mic = ttk.Frame(g_mic)
        r_mic.pack(fill="x", pady=2)
        ttk.Label(r_mic, text="Мікрофон: ", width=15, anchor="w").pack(side="left")
        self.combo_phys_mic = ttk.Combobox(
            r_mic,
            textvariable=self.mix_mic_var,
            values=list(self.input_map) if hasattr(self, "input_map") else [],
            state="readonly",
            width=50,
        )
        self.combo_phys_mic.bind("<<ComboboxSelected>>", self._mix_mic_changed)
        self.combo_phys_mic.pack(side="left", fill="x", expand=True)
        self.settings_mic_combo = self.combo_phys_mic

        # 2. Джерело треків / звуку (Вхід)
        g_track = ttk.LabelFrame(container, text="2. Джерело треків / звуку (Вхід)", padding=8)
        g_track.pack(fill="x", pady=(0, 6))

        ttk.Label(
            g_track,
            text="Джерело звуку для відтворення (вбудований плеєр або зовнішній вхід):",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        r_track = ttk.Frame(g_track)
        r_track.pack(fill="x", pady=2)
        ttk.Label(r_track, text="Джерело звуку: ", width=15, anchor="w").pack(side="left")
        self.combo_track_source = ttk.Combobox(
            r_track,
            textvariable=self.track_source_var,
            values=self._get_track_sources(),
            state="readonly",
            width=50,
        )
        self.combo_track_source.bind("<<ComboboxSelected>>", self._track_source_changed)
        self.combo_track_source.pack(side="left", fill="x", expand=True)

        # 3. Навушники (лише трек) [Вихід]
        g_hp = ttk.LabelFrame(container, text="3. Навушники (лише трек) [Вихід]", padding=8)
        g_hp.pack(fill="x", pady=(0, 6))

        ttk.Label(
            g_hp,
            text="Фізичний пристрій моніторингу (навушники або динаміки):\nСюди надсилається ТІЛЬКИ звук треку (без мікрофона, без власного еха).",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        r_hp = ttk.Frame(g_hp)
        r_hp.pack(fill="x", pady=2)
        ttk.Label(r_hp, text="Навушники: ", width=15, anchor="w").pack(side="left")
        self.combo_headphones = ttk.Combobox(
            r_hp,
            textvariable=self.monitor_var,
            values=list(self.monitor_map) if hasattr(self, "monitor_map") else [],
            state="readonly",
            width=50,
        )
        self.combo_headphones.bind("<<ComboboxSelected>>", self._monitor_changed)
        self.combo_headphones.pack(side="left", fill="x", expand=True)
        self.monitor_combo = self.combo_headphones

        # 4. Віртуальний кабель (мікс для Discord) [Вихід]
        g_cable = ttk.LabelFrame(container, text="4. Віртуальний кабель (мікс для Discord) [Вихід]", padding=8)
        g_cable.pack(fill="x", pady=(0, 6))

        ttk.Label(
            g_cable,
            text="Головний вихід програми (пристрій «CABLE Input (VB-Audio Virtual Cable)»):\nСюди надсилається суміш мікрофона і треку з захистом від кліпінгу.",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))

        r_cable = ttk.Frame(g_cable)
        r_cable.pack(fill="x", pady=2)
        ttk.Label(r_cable, text="CABLE Input: ", width=15, anchor="w").pack(side="left")
        self.combo_virtual_cable = ttk.Combobox(
            r_cable,
            textvariable=self.output_var,
            values=list(self.output_map) if hasattr(self, "output_map") else [],
            state="readonly",
            width=50,
        )
        self.combo_virtual_cable.bind("<<ComboboxSelected>>", self._output_changed)
        self.combo_virtual_cable.pack(side="left", fill="x", expand=True)
        self.output_combo = self.combo_virtual_cable

        # Нижній рядок: перевірка конфігурації + кнопка Оновити список
        b_frame = ttk.Frame(container)
        b_frame.pack(fill="x", pady=(8, 2))

        ttk.Checkbutton(
            b_frame,
            text="Перевіряти конфігурацію пристрою під час запуску",
            variable=self.check_device_config_on_launch_var,
        ).pack(side="left")

        ttk.Button(
            b_frame,
            text="⟳ Оновити список",
            command=self._load_devices,
        ).pack(side="right")

    def _build_settings_bottom_bar(self) -> None:
        c = self._get_theme_colors()
        self.settings_bottom_bar = ttk.Frame(self._settings_window, padding=(10, 8))
        self.settings_bottom_bar.pack(fill="x", side="bottom")

        self.settings_bottom_bar_divider = tk.Frame(self._settings_window, height=1, bg=c["divider"])
        self.settings_bottom_bar_divider.pack(fill="x", side="bottom")

        self.btn_settings_reset = ttk.Button(self.settings_bottom_bar, text=self._tr("btn_reset"), command=self._reset_settings_to_defaults)
        self.btn_settings_reset.pack(side="left")

        self.btn_settings_apply = ttk.Button(self.settings_bottom_bar, text=self._tr("btn_apply"), command=self._apply_settings)
        self.btn_settings_apply.pack(side="right", padx=(4, 0))
        self.btn_settings_cancel = ttk.Button(self.settings_bottom_bar, text=self._tr("btn_cancel"), command=self._cancel_settings)
        self.btn_settings_cancel.pack(side="right", padx=(4, 0))
        self.btn_settings_ok = ttk.Button(self.settings_bottom_bar, text=self._tr("btn_ok"), command=self._ok_settings)
        self.btn_settings_ok.pack(side="right")

    def _on_minimize_tray_toggle(self) -> None:
        if not hasattr(self, "chk_min_instead_close"):
            return
        if self.minimize_to_tray_var.get():
            self.chk_min_instead_close.configure(state="normal")
        else:
            self.minimize_instead_of_close_var.set(False)
            self.chk_min_instead_close.configure(state="disabled")

    def _show_settings_hint(self, title: str, msg: str) -> None:
        messagebox.showinfo(title, msg, parent=self._settings_window)

    def _set_special_hotkey(self) -> None:
        sel = self.special_hotkeys_tree.selection()
        if not sel:
            messagebox.showinfo("Гарячі клавіші", "Виберіть дію зі списку.", parent=self._settings_window)
            return
        item_id = sel[0]
        action = self.special_hotkeys_tree.item(item_id, "values")[0]
        cur = self.special_hotkeys_dict.get(action, "")
        val = simpledialog.askstring("Встановити гарячу клавішу", f"Введіть клавішу для «{action}» (наприклад: F5, Space, Ctrl+K):", initialvalue=cur, parent=self._settings_window)
        if val is not None:
            self.special_hotkeys_dict[action] = val.strip()
            self.special_hotkeys_tree.item(item_id, values=(action, val.strip()))

    def _change_special_hotkey_value(self) -> None:
        self._set_special_hotkey()

    def _add_auto_key(self) -> None:
        key = simpledialog.askstring("Додати автоклавішу", "Введіть клавішу для автоматичного натискання (наприклад: F, Enter, Space):", parent=self._settings_window)
        if key and key.strip():
            k = key.strip().upper()
            t = "Натиснути та відпустити"
            self.auto_keys_list.append((k, t))
            self.auto_keys_tree.insert("", "end", values=(k, t))

    def _edit_auto_key(self) -> None:
        sel = self.auto_keys_tree.selection()
        if not sel:
            return
        item = sel[0]
        cur_k, cur_t = self.auto_keys_tree.item(item, "values")
        new_k = simpledialog.askstring("Редагувати автоклавішу", "Введіть нову клавішу:", initialvalue=cur_k, parent=self._settings_window)
        if new_k and new_k.strip():
            k = new_k.strip().upper()
            idx = self.auto_keys_tree.index(item)
            self.auto_keys_list[idx] = (k, cur_t)
            self.auto_keys_tree.item(item, values=(k, cur_t))

    def _remove_auto_key(self) -> None:
        sel = self.auto_keys_tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self.auto_keys_tree.index(item)
        if 0 <= idx < len(self.auto_keys_list):
            del self.auto_keys_list[idx]
        self.auto_keys_tree.delete(item)

    def _on_rec_device_click(self, event: tk.Event) -> None:
        item = self.rec_devices_tree.identify_row(event.y)
        if not item:
            return
        vals = list(self.rec_devices_tree.item(item, "values"))
        if not vals:
            return
        dev_name = vals[1]
        cur_chk = vals[0] == "☑"
        new_chk = not cur_chk
        self.recording_devices_checked[dev_name] = new_chk
        vals[0] = "☑" if new_chk else "☐"
        self.rec_devices_tree.item(item, values=vals)
        if new_chk:
            self.mix_mic_var.set(dev_name)
            self.input_var.set(dev_name)

    def _toggle_selected_rec_device(self) -> None:
        sel = self.rec_devices_tree.selection()
        if not sel:
            return
        item = sel[0]
        vals = list(self.rec_devices_tree.item(item, "values"))
        dev_name = vals[1]
        cur_chk = vals[0] == "☑"
        new_chk = not cur_chk
        self.recording_devices_checked[dev_name] = new_chk
        vals[0] = "☑" if new_chk else "☐"
        self.rec_devices_tree.item(item, values=vals)
        if new_chk:
            self.mix_mic_var.set(dev_name)
            self.input_var.set(dev_name)

    def _update_settings_devices_view(self) -> None:
        if hasattr(self, "combo_virtual_cable") and hasattr(self, "output_map"):
            self.combo_virtual_cable["values"] = list(self.output_map)
        if hasattr(self, "output_combo") and hasattr(self, "output_map"):
            self.output_combo["values"] = list(self.output_map)

        if hasattr(self, "combo_phys_mic") and hasattr(self, "input_map"):
            self.combo_phys_mic["values"] = list(self.input_map)
        if hasattr(self, "settings_mic_combo") and hasattr(self, "input_map"):
            self.settings_mic_combo["values"] = list(self.input_map)

        if hasattr(self, "combo_track_source"):
            self.combo_track_source["values"] = self._get_track_sources()

        if hasattr(self, "combo_headphones") and hasattr(self, "monitor_map"):
            self.combo_headphones["values"] = list(self.monitor_map)
        if hasattr(self, "monitor_combo") and hasattr(self, "monitor_map"):
            self.monitor_combo["values"] = list(self.monitor_map)

        if not hasattr(self, "rec_devices_tree") or self.rec_devices_tree is None or not self.rec_devices_tree.winfo_exists():
            return
        for item in self.rec_devices_tree.get_children():
            self.rec_devices_tree.delete(item)
        for _, dev in getattr(self, "input_devices", []):
            name = dev.get("name", "Мікрофон")
            is_chk = self.recording_devices_checked.get(name, False)
            if not is_chk and (self.mix_mic_var.get() == name or self.input_var.get() == name):
                is_chk = True
                self.recording_devices_checked[name] = True
            chk_sym = "☑" if is_chk else "☐"
            self.rec_devices_tree.insert("", "end", values=(chk_sym, name, "+0 dB", "Добре"), tags=("good",))

    def _sync_settings_to_dialog(self) -> None:
        if hasattr(self, "special_hotkeys_tree") and self.special_hotkeys_tree.winfo_exists():
            for item in self.special_hotkeys_tree.get_children():
                self.special_hotkeys_tree.delete(item)
            for action, sc in self.special_hotkeys_dict.items():
                self.special_hotkeys_tree.insert("", "end", values=(action, sc))

        if hasattr(self, "auto_keys_tree") and self.auto_keys_tree.winfo_exists():
            for item in self.auto_keys_tree.get_children():
                self.auto_keys_tree.delete(item)
            for k, t in self.auto_keys_list:
                self.auto_keys_tree.insert("", "end", values=(k, t))

        self._update_settings_devices_view()
        self._on_minimize_tray_toggle()

    def _reset_settings_to_defaults(self) -> None:
        self.norm_volume_val.set("89,0 дБ")
        self.norm_mode_var.set("dynamic")

        self.special_hotkeys_dict = {
            "Зупинити відтворення": "",
            "Призупинити/продовжити відтворення": "Space",
            "Відтворити попередній файл": "",
            "Відтворити наступний файл": "",
            "Відтворити вибраний файл": "",
            "Відтворити випадковий файл": "",
            "Відтворити випадковий файл із усіх категорій": "",
            "Зберегти список звуків": "Ctrl+S",
            "Скасувати": "Ctrl+Z",
            "Відновити": "Ctrl+Y",
            "Вирізати": "Ctrl+X",
            "Копіювати": "Ctrl+C",
            "Вставити": "Ctrl+V",
            "Видалити вибрані записи": "Del",
            "Вибрати все": "Ctrl+A",
            "Пошук": "Ctrl+F",
            "Записування звуку": "Ctrl+R",
            "Озвучення тексту": "Ctrl+T",
            "Вихід": "Alt+F4",
        }
        self.numpad_alt_var.set(False)
        self.numpad_ctrl_alt_var.set(False)
        self.auto_keys_list.clear()
        self.pass_hotkeys_var.set(False)
        self.auto_stop_var.set(True)
        self.allow_lr_modifiers_var.set(False)
        self.same_hotkeys_all_categories_var.set(False)

        self.lang_var.set("За замовчуванням")
        self.date_format_var.set("Мова за замовчуванням")
        self.tb_style_var.set("Темний")
        self.tb_scale_var.set("100%")
        self.tb_size_var.set("Малий")
        self.tb_color_var.set("Синій")
        self.pos_refresh_rate_var.set(60)
        self.show_mic_level_var.set(False)
        self.linear_volume_slider_var.set(True)
        self.allow_resume_other_modes_var.set(False)
        self.hotkey_switch_mode_var.set("«за замовчуванням» і динаміками")
        self.launch_on_startup_var.set(False)
        self.start_minimized_var.set(False)
        self.show_tray_icon_var.set(True)
        self.minimize_to_tray_var.set(False)
        self.minimize_instead_of_close_var.set(False)
        self.always_on_top_var.set(False)
        self.disable_track_sorting_var.set(False)
        self.alternate_row_colors_var.set(False)
        self.typing_starts_search_var.set(True)
        self.tts_input_priority_var.set(False)
        self.remember_playback_state_var.set(True)
        self.check_device_config_on_launch_var.set(True)
        if hasattr(self, "track_source_var"):
            self.track_source_var.set(DEFAULT_TRACK_SOURCE)

        self._sync_settings_to_dialog()
        self._apply_language()
        self.status_var.set(self._tr("settings_reset"))

    def _apply_settings(self) -> None:
        if hasattr(self, "tb_style_var"):
            new_theme = "light" if self.tb_style_var.get() in ("Світлий", "Light") else "dark"
            if new_theme != self.theme:
                self.theme = new_theme
                self._apply_theme()
                self._apply_dark_title_bar(self)
                if hasattr(self, "_settings_window") and self._settings_window is not None and self._settings_window.winfo_exists():
                    self._apply_dark_title_bar(self._settings_window)
        if hasattr(self, "lang_var"):
            self._apply_language()
        if hasattr(self, "always_on_top_var"):
            self.attributes("-topmost", self.always_on_top_var.get())
        if hasattr(self, "output_var") and hasattr(self, "engine"):
            out_name = self.output_var.get()
            if hasattr(self, "output_map") and out_name in self.output_map:
                self.engine.set_output_device(self.output_map[out_name])
        if hasattr(self, "mix_mic_var") and hasattr(self, "engine"):
            mic_name = self.mix_mic_var.get()
            if hasattr(self, "input_map") and mic_name in self.input_map:
                if getattr(self, "mic_enabled", None) and self.mic_enabled.get():
                    self.engine.set_input_device(self.input_map[mic_name])
        if hasattr(self, "monitor_var") and hasattr(self, "engine"):
            mon_name = self.monitor_var.get()
            if hasattr(self, "monitor_map") and mon_name in self.monitor_map:
                self.engine.set_monitor_device(self.monitor_map[mon_name])
        self._save_state()
        self.status_var.set(self._tr("settings_saved"))

    def _ok_settings(self) -> None:
        self._apply_settings()
        self._close_settings_dialog()

    def _cancel_settings(self) -> None:
        self._close_settings_dialog()

    def _close_settings_dialog(self) -> None:
        if self._settings_window is not None and self._settings_window.winfo_exists():
            try:
                self._settings_window.grab_release()
            except Exception:
                pass
            self._settings_window.withdraw()

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-o>", lambda _: self._add_files())
        self.bind("<Control-O>", lambda _: self._add_files())
        self.bind("<Control-s>", lambda _: self._save_sound_list())
        self.bind("<Control-S>", lambda _: self._save_sound_list())
        self.bind("<Control-f>", lambda _: self._toggle_search_bar())
        self.bind("<Control-F>", lambda _: self._toggle_search_bar())
        self.bind("<Control-y>", lambda _: self._redo())
        self.bind("<Control-Y>", lambda _: self._redo())
        self.bind("<Control-Shift-y>", lambda _: self._open_yt_search())
        self.bind("<Control-Shift-Y>", lambda _: self._open_yt_search())
        self.bind("<Control-z>", lambda _: self._undo())
        self.bind("<Control-Z>", lambda _: self._undo())
        self.bind("<Control-x>", lambda _: self._cut_track())
        self.bind("<Control-X>", lambda _: self._cut_track())
        self.bind("<Control-c>", lambda _: self._copy_track())
        self.bind("<Control-C>", lambda _: self._copy_track())
        self.bind("<Control-v>", lambda _: self._paste_track())
        self.bind("<Control-V>", lambda _: self._paste_track())
        self.bind("<Control-a>", lambda _: self._select_all_tracks())
        self.bind("<Control-A>", lambda _: self._select_all_tracks())
        self.bind("<Control-r>", lambda _: self._record_audio_dialog())
        self.bind("<Control-R>", lambda _: self._record_audio_dialog())
        self.bind("<Control-t>", lambda _: self._text_to_speech_dialog())
        self.bind("<Control-T>", lambda _: self._text_to_speech_dialog())
        self.bind("<Delete>", lambda _: self._remove_selected_track() if not self._is_entry_focused() else None)
        self.bind("<Alt-F4>", lambda _: self._exit())
        self.bind("<space>", lambda _: self._toggle_pause() if not self._is_entry_focused() else None)

    def _open_settings(self) -> None:
        if self._settings_window is None or not self._settings_window.winfo_exists():
            self._build_settings_dialog()
        assert self._settings_window is not None
        self._sync_settings_to_dialog()
        self._apply_language()

        self._apply_dark_title_bar(self._settings_window)

        self._settings_window.update_idletasks()
        w, h = 640, 760
        x = self.winfo_x() + max(0, (self.winfo_width() - w) // 2)
        y = self.winfo_y() + max(0, (self.winfo_height() - h) // 2)
        self._settings_window.geometry(f"{w}x{h}+{x}+{y}")
        self._settings_window.deiconify()
        self._apply_dark_title_bar(self._settings_window)
        self._settings_window.lift()
        self._settings_window.focus_force()
        try:
            self._settings_window.grab_set()
        except Exception:
            pass

    def _toggle_search_bar(self) -> None:
        if self.search_bar_frame.winfo_ismapped():
            self._close_search_bar()
        else:
            self.search_bar_frame.pack(fill="x", after=self.top_bar)
            self.search_entry.focus_set()
            try:
                self.search_entry.focus_force()
            except Exception:
                pass

    def _close_search_bar(self) -> None:
        self.search_var.set("")
        self.search_bar_frame.pack_forget()

    def _build_context_menu(self) -> None:
        self._track_menu = tk.Menu(self, tearoff=0)
        self._track_menu.add_command(label="▶ Відтворити", command=self._play_selected)
        self._track_menu.add_separator()
        self._track_menu.add_command(label="⌨ Призначити гарячу клавішу...", command=self._assign_hotkey_dialog)
        self._track_menu.add_command(label="✖ Скинути гарячу клавішу", command=self._clear_hotkey_selected)
        self._track_menu.add_separator()
        self._track_menu.add_command(label="➕ Додати аудіофайли...", command=self._add_files)
        self._track_menu.add_command(label="📁 Додати папку...", command=self._choose_folder)
        self._track_menu.add_command(label="🔍 Пошук YouTube...", command=self._open_yt_search)
        self._track_menu.add_separator()
        self._track_menu.add_command(label="🗑 Видалити зі списку", command=self._remove_selected_track)
        self.track_list.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event: tk.Event) -> None:
        row_id = self.track_list.identify_row(event.y)
        if row_id:
            self.track_list.selection_set(row_id)
            self._track_menu.tk_popup(event.x_root, event.y_root)

    def _get_theme_colors(self) -> dict[str, str]:
        dark = getattr(self, "theme", "dark") == "dark"
        if dark:
            return {
                "bg": "#1f1f1f",
                "top_bg": "#212121",
                "panel_bg": "#222222",
                "table_bg": "#1e1e1e",
                "hdr_bg": "#292929",
                "info_bg": "#262626",
                "bottom_bg": "#1c1c1c",
                "fg": "#d4d4d4",
                "muted_fg": "#9e9e9e",
                "accent": "#0084d1",
                "sel_bg": "#383838",
                "divider": "#161616",
                "split_divider": "#181818",
                "menu_bg": "#1a1a1a",
                "menu_fg": "#d4d4d4",
                "active_menu_bg": "#333333",
                "active_menu_fg": "#ffffff",
                "menu_border": "#3f3f3f",
                "trough_bg": "#141414",
                "cat_sel_bg": "#383838",
                "cat_sel_fg": "#ffffff",
                "cat_unsel_bg": "#222222",
                "cat_unsel_fg": "#cccccc",
                "cat_hover_bg": "#2d2d2d",
                "cb_field": "#262626",
                "cb_bg": "#2d2d2d",
                "cb_fg": "#ffffff",
                "cb_arrow": "#d4d4d4",
                "cb_border": "#383838",
                "listbox_bg": "#262626",
                "listbox_fg": "#ffffff",
                "listbox_sel_bg": "#0084d1",
                "listbox_sel_fg": "#ffffff",
                "nb_bg": "#1f1f1f",
                "nb_tab_bg": "#262626",
                "nb_tab_fg": "#d4d4d4",
                "nb_sel_tab_bg": "#1f1f1f",
                "nb_sel_tab_fg": "#ffffff",
                "entry_bg": "#262626",
                "entry_fg": "#ffffff",
                "dialog_bg": "#1f1f1f",
            }
        else:
            return {
                "bg": "#f4f5f7",
                "top_bg": "#eaedf1",
                "panel_bg": "#eceff2",
                "table_bg": "#ffffff",
                "hdr_bg": "#e0e3e8",
                "info_bg": "#e6e9ee",
                "bottom_bg": "#e0e3e8",
                "fg": "#20242b",
                "muted_fg": "#6a7280",
                "accent": "#007acc",
                "sel_bg": "#cbe2f8",
                "divider": "#d0d4da",
                "split_divider": "#d0d4da",
                "menu_bg": "#eaedf1",
                "menu_fg": "#20242b",
                "active_menu_bg": "#d0d4da",
                "active_menu_fg": "#000000",
                "menu_border": "#d0d4da",
                "trough_bg": "#d0d4da",
                "cat_sel_bg": "#d0e4f7",
                "cat_sel_fg": "#007acc",
                "cat_unsel_bg": "#eceff2",
                "cat_unsel_fg": "#4a5568",
                "cat_hover_bg": "#e2e7ec",
                "cb_field": "#ffffff",
                "cb_bg": "#eaedf1",
                "cb_fg": "#20242b",
                "cb_arrow": "#20242b",
                "cb_border": "#c0c4cc",
                "listbox_bg": "#ffffff",
                "listbox_fg": "#20242b",
                "listbox_sel_bg": "#007acc",
                "listbox_sel_fg": "#ffffff",
                "nb_bg": "#f4f5f7",
                "nb_tab_bg": "#e0e3e8",
                "nb_tab_fg": "#4a5568",
                "nb_sel_tab_bg": "#f4f5f7",
                "nb_sel_tab_fg": "#20242b",
                "entry_bg": "#ffffff",
                "entry_fg": "#20242b",
                "dialog_bg": "#f4f5f7",
            }

    def _on_combobox_click(self, event: tk.Event) -> None:
        try:
            w = event.widget
            if isinstance(w, ttk.Combobox):
                c = self._get_theme_colors()
                lb = f"{w}.popdown.f.l"
                w.tk.eval(
                    f'catch {{{lb} configure -background "{c["listbox_bg"]}" '
                    f'-foreground "{c["listbox_fg"]}" '
                    f'-selectbackground "{c["listbox_sel_bg"]}" '
                    f'-selectforeground "{c["listbox_sel_fg"]}"}}'
                )
        except Exception:
            pass

    def _apply_theme(self) -> None:
        c = self._get_theme_colors()
        dark = getattr(self, "theme", "dark") == "dark"

        self.configure(bg=c["bg"])
        self.style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["table_bg"])
        self.style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"), foreground=c["accent"], background=c["bg"])
        self.style.configure("NowPlaying.TLabel", font=("Segoe UI", 10, "bold"), foreground=c["accent"], background=c["info_bg"])
        self.style.configure("Status.TLabel", foreground=c["muted_fg"], background=c["bottom_bg"], font=("Segoe UI", 9))
        self.style.configure(
            "Treeview",
            rowheight=24,
            background=c["table_bg"],
            fieldbackground=c["table_bg"],
            foreground=c["fg"],
            font=("Segoe UI", 9),
            bordercolor=c["table_bg"],
            lightcolor=c["table_bg"],
            darkcolor=c["table_bg"],
            borderwidth=0,
        )
        self.style.map(
            "Treeview",
            background=[("selected", c["sel_bg"])],
            foreground=[("selected", "#ffffff" if dark else "#000000")],
        )
        self.style.configure(
            "Treeview.Heading",
            background=c["hdr_bg"],
            foreground=c["muted_fg"],
            relief="flat",
            font=("Segoe UI", 9),
            bordercolor=c["divider"],
            lightcolor=c["hdr_bg"],
            darkcolor=c["hdr_bg"],
            borderwidth=1,
        )
        self.style.map("Treeview.Heading", background=[("active", "#363636" if dark else "#d8dce2")])
        self.style.configure(
            "Soundpad.Horizontal.TScale",
            background=c["accent"],
            troughcolor=c["trough_bg"],
            bordercolor=c["top_bg"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            sliderlength=14,
            sliderthickness=11,
        )
        self.style.configure(
            "Settings.Horizontal.TScale",
            background=c["accent"],
            troughcolor=c["trough_bg"],
            bordercolor=c["bg"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            sliderlength=14,
            sliderthickness=11,
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=c["cb_field"],
            background=c["cb_bg"],
            foreground=c["cb_fg"],
            arrowcolor=c["cb_arrow"],
            bordercolor=c["cb_border"],
            darkcolor=c["cb_border"],
            lightcolor=c["cb_border"],
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", "focus", c["cb_field"]),
                ("readonly", c["cb_field"]),
                ("!disabled", c["cb_field"]),
            ],
            selectbackground=[
                ("readonly", "focus", c["listbox_sel_bg"]),
                ("readonly", c["cb_field"]),
            ],
            selectforeground=[
                ("readonly", "focus", c["listbox_sel_fg"]),
                ("readonly", c["cb_fg"]),
            ],
            foreground=[
                ("disabled", c["muted_fg"]),
                ("readonly", c["cb_fg"]),
                ("!disabled", c["cb_fg"]),
            ],
            background=[
                ("pressed", "#3a3a3a" if dark else "#c8ccd2"),
                ("active", "#333333" if dark else "#d8dce2"),
                ("!disabled", c["cb_bg"]),
            ],
            arrowcolor=[
                ("disabled", "#666666" if dark else "#a0a4aa"),
                ("active", "#ffffff" if dark else "#000000"),
                ("!disabled", c["cb_arrow"]),
            ],
            bordercolor=[
                ("focus", c["accent"]),
                ("!disabled", c["cb_border"]),
            ],
        )
        self.style.configure(
            "TNotebook",
            background=c["nb_bg"],
            bordercolor=c["divider"],
            darkcolor=c["nb_bg"],
            lightcolor=c["nb_bg"],
            borderwidth=0,
        )
        self.style.configure(
            "TNotebook.Tab",
            background=c["nb_tab_bg"],
            foreground=c["nb_tab_fg"],
            bordercolor=c["divider"],
            lightcolor=c["divider"],
            darkcolor=c["divider"],
            padding=[12, 4],
            borderwidth=1,
        )
        self.style.map(
            "TNotebook.Tab",
            background=[
                ("selected", c["nb_sel_tab_bg"]),
                ("active", "#2e2e2e" if dark else "#dbe0e7"),
            ],
            foreground=[
                ("selected", c["nb_sel_tab_fg"]),
                ("active", c["accent"]),
            ],
            bordercolor=[
                ("selected", c["accent"]),
                ("!disabled", c["divider"]),
            ],
            lightcolor=[
                ("selected", c["accent"]),
                ("!disabled", c["divider"]),
            ],
            darkcolor=[
                ("selected", c["accent"]),
                ("!disabled", c["divider"]),
            ],
        )
        self.style.configure(
            "TLabelframe",
            background=c["bg"],
            bordercolor=c["divider"],
            darkcolor=c["divider"],
            lightcolor=c["divider"],
        )
        self.style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        self.style.configure("TFrame", background=c["bg"])
        self.style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        self.style.configure(
            "TCheckbutton",
            background=c["bg"],
            foreground=c["fg"],
            indicatorbackground=c["cb_field"],
            indicatorcolor=c["accent"],
        )
        self.style.map(
            "TCheckbutton",
            indicatorbackground=[
                ("pressed", "#333333" if dark else "#d0d4da"),
                ("selected", c["accent"]),
                ("!disabled", c["cb_field"]),
            ],
        )
        self.style.configure(
            "TRadiobutton",
            background=c["bg"],
            foreground=c["fg"],
            indicatorbackground=c["cb_field"],
            indicatorcolor=c["accent"],
        )
        self.style.map(
            "TRadiobutton",
            indicatorbackground=[
                ("pressed", "#333333" if dark else "#d0d4da"),
                ("selected", c["accent"]),
                ("!disabled", c["cb_field"]),
            ],
        )
        self.style.configure(
            "TButton",
            background=c["top_bg"],
            foreground=c["fg"],
            bordercolor=c["divider"],
            darkcolor=c["top_bg"],
            lightcolor=c["top_bg"],
        )
        self.style.map(
            "TButton",
            background=[
                ("pressed", "#3a3a3a" if dark else "#c8ccd2"),
                ("active", "#333333" if dark else "#d8dce2"),
                ("!disabled", c["top_bg"]),
            ],
            foreground=[
                ("disabled", c["muted_fg"]),
                ("active", "#ffffff" if dark else "#000000"),
                ("!disabled", c["fg"]),
            ],
            bordercolor=[
                ("focus", c["accent"]),
                ("!disabled", c["divider"]),
            ],
            lightcolor=[("!disabled", c["top_bg"])],
            darkcolor=[("!disabled", c["top_bg"])],
        )
        self.style.configure(
            "TEntry",
            fieldbackground=c["entry_bg"],
            foreground=c["entry_fg"],
            bordercolor=c["divider"],
            darkcolor=c["divider"],
            lightcolor=c["divider"],
        )
        self.style.configure(
            "TSpinbox",
            fieldbackground=c["entry_bg"],
            foreground=c["entry_fg"],
            bordercolor=c["divider"],
            darkcolor=c["divider"],
            lightcolor=c["divider"],
        )
        self.style.configure(
            "TScrollbar",
            background="#333333" if dark else "#c8ccd2",
            troughcolor=c["panel_bg"],
            bordercolor=c["divider"],
            arrowcolor=c["muted_fg"],
            lightcolor="#333333" if dark else "#c8ccd2",
            darkcolor="#333333" if dark else "#c8ccd2",
        )
        self.style.map(
            "TScrollbar",
            background=[
                ("pressed", "#444444" if dark else "#b0b4ba"),
                ("active", "#3a3a3a" if dark else "#d0d4da"),
                ("!disabled", "#333333" if dark else "#c8ccd2"),
            ],
            arrowcolor=[
                ("active", "#ffffff" if dark else "#000000"),
                ("!disabled", c["muted_fg"]),
            ],
        )

        # Tk option database for ComboBox Listbox popup
        self.option_add("*TCombobox*Listbox.background", c["listbox_bg"])
        self.option_add("*TCombobox*Listbox.foreground", c["listbox_fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", c["listbox_sel_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", c["listbox_sel_fg"])

        # Dynamically reconfigure existing combobox popdown listboxes
        def _reconfigure_combos_in(parent: tk.Widget) -> None:
            if not parent or not parent.winfo_exists():
                return
            widgets = [parent]
            while widgets:
                w = widgets.pop()
                if isinstance(w, ttk.Combobox):
                    try:
                        lb = f"{w}.popdown.f.l"
                        if w.tk.eval(f"winfo exists {lb}") == "1":
                            w.tk.eval(
                                f'catch {{{lb} configure -background "{c["listbox_bg"]}" '
                                f'-foreground "{c["listbox_fg"]}" '
                                f'-selectbackground "{c["listbox_sel_bg"]}" '
                                f'-selectforeground "{c["listbox_sel_fg"]}"}}'
                            )
                    except Exception:
                        pass
                try:
                    widgets.extend(w.winfo_children())
                except Exception:
                    pass

        _reconfigure_combos_in(self)
        try:
            self.bind_class("TCombobox", "<Button-1>", self._on_combobox_click, add="+")
        except Exception:
            pass

        # Sliders
        if hasattr(self, "seek_scale") and hasattr(self.seek_scale, "configure"):
            self.seek_scale.set_theme(self.theme)
            self.seek_scale.configure(bg=c["top_bg"])
        if hasattr(self, "volume_scale") and hasattr(self.volume_scale, "configure"):
            self.volume_scale.set_theme(self.theme)
            self.volume_scale.configure(bg=c["top_bg"])

        # Top Toolbar
        if hasattr(self, "top_bar") and hasattr(self.top_bar, "configure"):
            self.top_bar.configure(bg=c["top_bg"])
        for btn_name in ("pause_button", "btn_tb_stop", "btn_tb_prev", "btn_tb_next", "btn_tb_mute"):
            btn = getattr(self, btn_name, None)
            if btn and hasattr(btn, "configure"):
                btn.configure(
                    bg=c["top_bg"],
                    activebackground="#333333" if dark else "#d0d4da",
                    fg=c["accent"],
                )
        for sep_name in ("top_bar_sep1", "top_bar_sep2", "top_bar_divider"):
            sep = getattr(self, sep_name, None)
            if sep and hasattr(sep, "configure"):
                sep.configure(bg=c["divider"])
        if hasattr(self, "time_label") and hasattr(self.time_label, "configure"):
            self.time_label.configure(bg=c["top_bg"], fg="#ffffff" if dark else "#20242b")

        # Recursive Menu Bar theming
        def _update_menu_recursively(menu: tk.Menu) -> None:
            try:
                menu.configure(
                    bg=c["menu_bg"],
                    fg=c["menu_fg"],
                    activebackground=c["active_menu_bg"],
                    activeforeground=c["active_menu_fg"],
                    selectcolor=c["accent"],
                    relief="solid",
                    bd=1,
                    activeborderwidth=0,
                )
            except Exception:
                pass
            try:
                for child in menu.winfo_children():
                    if isinstance(child, tk.Menu):
                        _update_menu_recursively(child)
            except Exception:
                pass

        if hasattr(self, "menu_frame") and hasattr(self.menu_frame, "configure"):
            self.menu_frame.configure(bg=c["menu_bg"])
        if hasattr(self, "_menu_buttons"):
            for mb in self._menu_buttons.values():
                mb.configure(
                    bg=c["menu_bg"],
                    fg=c["menu_fg"],
                    activebackground=c["active_menu_bg"],
                    activeforeground=c["active_menu_fg"],
                )
        if hasattr(self, "menubar") and hasattr(self.menubar, "configure"):
            _update_menu_recursively(self.menubar)
        for m_name in ("_track_menu", "_cat_menu"):
            m = getattr(self, m_name, None)
            if m and hasattr(m, "configure") and isinstance(m, tk.Menu):
                _update_menu_recursively(m)
        self._setup_windows_dark_mode()

        # Search Bar
        if hasattr(self, "search_bar_frame") and hasattr(self.search_bar_frame, "configure"):
            self.search_bar_frame.configure(bg=c["hdr_bg"])
        if hasattr(self, "search_bar_lbl") and hasattr(self.search_bar_lbl, "configure"):
            self.search_bar_lbl.configure(bg=c["hdr_bg"], fg=c["muted_fg"])
        if hasattr(self, "btn_close_search") and hasattr(self.btn_close_search, "configure"):
            self.btn_close_search.configure(
                bg=c["hdr_bg"],
                fg=c["muted_fg"],
                activebackground="#3d3d3d" if dark else "#d0d4da",
            )

        # Main Split and Left Panel (Categories)
        if hasattr(self, "main_split") and hasattr(self.main_split, "configure"):
            self.main_split.configure(bg=c["bg"])
        if hasattr(self, "split_divider") and hasattr(self.split_divider, "configure"):
            self.split_divider.configure(bg=c["split_divider"])
        if hasattr(self, "left_panel") and hasattr(self.left_panel, "configure"):
            self.left_panel.configure(bg=c["panel_bg"])
        if hasattr(self, "cat_hdr") and hasattr(self.cat_hdr, "configure"):
            self.cat_hdr.configure(bg=c["hdr_bg"])
        if hasattr(self, "cat_hdr_lbl1") and hasattr(self.cat_hdr_lbl1, "configure"):
            self.cat_hdr_lbl1.configure(bg=c["hdr_bg"], fg=c["muted_fg"])
        if hasattr(self, "cat_hdr_lbl2") and hasattr(self.cat_hdr_lbl2, "configure"):
            self.cat_hdr_lbl2.configure(bg=c["hdr_bg"], fg=c["muted_fg"])
        if hasattr(self, "cat_hdr_div") and hasattr(self.cat_hdr_div, "configure"):
            self.cat_hdr_div.configure(bg=c["split_divider"])

        if hasattr(self, "cat_item_frame"):
            self._select_category(getattr(self, "current_category", "my_sounds"))

        # Right Panel and Table
        if hasattr(self, "right_panel") and hasattr(self.right_panel, "configure"):
            self.right_panel.configure(bg=c["table_bg"])
        if hasattr(self, "table_container") and hasattr(self.table_container, "configure"):
            self.table_container.configure(bg=c["table_bg"])

        # Info Panel
        if hasattr(self, "info_panel_divider") and hasattr(self.info_panel_divider, "configure"):
            self.info_panel_divider.configure(bg=c["divider"])
        if hasattr(self, "info_panel") and hasattr(self.info_panel, "configure"):
            self.info_panel.configure(bg=c["info_bg"])
        if hasattr(self, "_update_category_info_panel"):
            self._update_category_info_panel()
        else:
            if hasattr(self, "info_house_lbl") and hasattr(self.info_house_lbl, "configure"):
                self.info_house_lbl.configure(bg=c["info_bg"], fg=c["accent"])
            if hasattr(self, "info_cat_name_lbl") and hasattr(self.info_cat_name_lbl, "configure"):
                self.info_cat_name_lbl.configure(bg=c["info_bg"], fg=c["fg"])
        if hasattr(self, "stats_frame") and hasattr(self.stats_frame, "configure"):
            self.stats_frame.configure(bg=c["info_bg"])
        if hasattr(self, "stats_r1") and hasattr(self.stats_r1, "configure"):
            self.stats_r1.configure(bg=c["info_bg"])
        if hasattr(self, "stats_r2") and hasattr(self.stats_r2, "configure"):
            self.stats_r2.configure(bg=c["info_bg"])
        if hasattr(self, "stats_caption_labels"):
            for lbl in self.stats_caption_labels:
                lbl.configure(bg=c["info_bg"], fg=c["muted_fg"])
        if hasattr(self, "stats_sounds_lbl") and hasattr(self.stats_sounds_lbl, "configure"):
            self.stats_sounds_lbl.configure(bg=c["info_bg"], fg=c["fg"])
        if hasattr(self, "stats_plays_lbl") and hasattr(self.stats_plays_lbl, "configure"):
            self.stats_plays_lbl.configure(bg=c["info_bg"], fg=c["fg"])
        if hasattr(self, "stats_selected_lbl") and hasattr(self.stats_selected_lbl, "configure"):
            self.stats_selected_lbl.configure(bg=c["info_bg"], fg=c["fg"])
        if hasattr(self, "btn_wrench") and hasattr(self.btn_wrench, "configure"):
            self.btn_wrench.configure(
                bg=c["info_bg"],
                activebackground="#383838" if dark else "#d0d4da",
                fg=c["accent"],
            )

        # Bottom Toolbar
        if hasattr(self, "bottom_bar_divider") and hasattr(self.bottom_bar_divider, "configure"):
            self.bottom_bar_divider.configure(bg=c["divider"])
        if hasattr(self, "bottom_bar") and hasattr(self.bottom_bar, "configure"):
            self.bottom_bar.configure(bg=c["bottom_bg"])
        if hasattr(self, "bottom_bar_spacer") and hasattr(self.bottom_bar_spacer, "configure"):
            self.bottom_bar_spacer.configure(bg=c["bottom_bg"])
        for btn_name in ("btn_b_play", "btn_b_h", "btn_b_a", "btn_b_msg", "btn_b_ff", "btn_b_loop"):
            btn = getattr(self, btn_name, None)
            if btn and hasattr(btn, "configure"):
                btn.configure(
                    bg=c["bottom_bg"],
                    activebackground="#2c2c2c" if dark else "#d0d4da",
                    fg=c["accent"],
                )
        if hasattr(self, "bottom_bar_seps"):
            for sep in self.bottom_bar_seps:
                sep.configure(bg=c["divider"])

        # YouTube Search Panel
        if hasattr(self, "yt_search_frame") and self.yt_search_frame is not None and self.yt_search_frame.winfo_exists():
            self.yt_search_frame.configure(bg=c["table_bg"])
            if hasattr(self, "yt_titlebar") and self.yt_titlebar.winfo_exists():
                self.yt_titlebar.configure(bg=c["hdr_bg"])
            if hasattr(self, "yt_title_icon") and self.yt_title_icon.winfo_exists():
                self.yt_title_icon.configure(bg=c["hdr_bg"], fg=c["accent"])
            if hasattr(self, "yt_title_lbl") and self.yt_title_lbl.winfo_exists():
                self.yt_title_lbl.configure(bg=c["hdr_bg"], fg=c["fg"])
            if hasattr(self, "btn_yt_close_x") and self.btn_yt_close_x.winfo_exists():
                self.btn_yt_close_x.configure(bg=c["hdr_bg"], fg=c["muted_fg"])
            if hasattr(self, "yt_top") and self.yt_top.winfo_exists():
                self.yt_top.configure(bg=c["table_bg"])
            if hasattr(self, "yt_entry_border") and self.yt_entry_border.winfo_exists():
                self.yt_entry_border.configure(bg=c["divider"])
            if hasattr(self, "yt_search_entry") and self.yt_search_entry.winfo_exists():
                self.yt_search_entry.configure(
                    bg=c["entry_bg"],
                    fg=c["entry_fg"],
                    insertbackground=c["accent"],
                )
            if hasattr(self, "btn_yt_search") and self.btn_yt_search.winfo_exists():
                self.btn_yt_search.configure(
                    bg=c["top_bg"],
                    fg=c["fg"],
                    activebackground=c["active_menu_bg"],
                    activeforeground=c["active_menu_fg"],
                )
            if hasattr(self, "yt_status_lbl") and self.yt_status_lbl.winfo_exists():
                self.yt_status_lbl.configure(bg=c["table_bg"], fg=c["muted_fg"])
            if hasattr(self, "yt_table_box") and self.yt_table_box.winfo_exists():
                self.yt_table_box.configure(bg=c["divider"])
            if hasattr(self, "yt_bottom") and self.yt_bottom.winfo_exists():
                self.yt_bottom.configure(bg=c["table_bg"])
            for btn_name in ("btn_yt_play", "btn_yt_cache", "btn_yt_close"):
                btn = getattr(self, btn_name, None)
                if btn and btn.winfo_exists():
                    btn.configure(
                        bg=c["top_bg"],
                        fg=c["fg"],
                        activebackground=c["active_menu_bg"],
                        activeforeground=c["active_menu_fg"],
                    )

        # Settings Window (if open or created)
        if hasattr(self, "_settings_window") and self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.configure(bg=c["bg"])
            self._settings_window.option_add("*TCombobox*Listbox.background", c["listbox_bg"])
            self._settings_window.option_add("*TCombobox*Listbox.foreground", c["listbox_fg"])
            self._settings_window.option_add("*TCombobox*Listbox.selectBackground", c["listbox_sel_bg"])
            self._settings_window.option_add("*TCombobox*Listbox.selectForeground", c["listbox_sel_fg"])
            _reconfigure_combos_in(self._settings_window)
            if hasattr(self, "_settings_canvases"):
                for cnv in self._settings_canvases:
                    if cnv.winfo_exists():
                        cnv.configure(bg=c["bg"])
            if hasattr(self, "c_ramp") and self.c_ramp.winfo_exists():
                self.c_ramp.configure(bg=c["table_bg"], highlightbackground=c["divider"])
            if hasattr(self, "c_lin") and self.c_lin.winfo_exists():
                self.c_lin.configure(bg=c["table_bg"], highlightbackground=c["divider"])
            if hasattr(self, "settings_bottom_bar_divider") and self.settings_bottom_bar_divider.winfo_exists():
                self.settings_bottom_bar_divider.configure(bg=c["divider"])
            self._apply_dark_title_bar(self._settings_window)

        if hasattr(self, "theme_button"):
            self.theme_button.configure(text="Світла тема" if dark else "Темна тема")
        if hasattr(self, "_update_mute_icon"):
            self._update_mute_icon()
        if hasattr(self, "notification_manager"):
            self.notification_manager.set_theme(self.theme)
        self._apply_dark_title_bar(self)

    def _load_devices(self) -> None:
        try:
            devices = AudioEngine.list_devices()
            hostapis = {d.get("hostapi", 0) for d in devices if isinstance(d, dict)}
            use_mme_filter = len(hostapis) > 1 and any(
                d.get("hostapi", 0) == 0 and d.get("max_output_channels", 0) > 0 for d in devices
            )
            self.output_devices = [
                (i, d)
                for i, d in enumerate(devices)
                if d.get("max_output_channels", 0) > 0 and (not use_mme_filter or d.get("hostapi", 0) == 0)
            ]
            self.input_devices = [
                (i, d)
                for i, d in enumerate(devices)
                if d.get("max_input_channels", 0) > 0 and (not use_mme_filter or d.get("hostapi", 0) == 0)
            ]
            self.output_map = {f"{d['name']} ({i})": i for i, d in self.output_devices}
            self.input_map = {f"{d['name']} ({i})": i for i, d in self.input_devices}
            if hasattr(self, "output_combo") and hasattr(self.output_combo, "configure"):
                self.output_combo["values"] = list(self.output_map)
            if hasattr(self, "combo_virtual_cable") and hasattr(self.combo_virtual_cable, "configure"):
                self.combo_virtual_cable["values"] = list(self.output_map)
            if hasattr(self, "input_combo") and hasattr(self.input_combo, "configure"):
                self.input_combo["values"] = list(self.input_map)
            if hasattr(self, "mix_mic_combo") and hasattr(self.mix_mic_combo, "configure"):
                self.mix_mic_combo["values"] = list(self.input_map)
            if hasattr(self, "combo_phys_mic") and hasattr(self.combo_phys_mic, "configure"):
                self.combo_phys_mic["values"] = list(self.input_map)
            if hasattr(self, "combo_track_source") and hasattr(self.combo_track_source, "configure"):
                self.combo_track_source["values"] = self._get_track_sources()
            self.monitor_map = {"(Без моніторингу)": None}
            self.monitor_map.update(self.output_map)
            if hasattr(self, "monitor_combo") and hasattr(self.monitor_combo, "configure"):
                self.monitor_combo["values"] = list(self.monitor_map)
            if hasattr(self, "combo_headphones") and hasattr(self.combo_headphones, "configure"):
                self.combo_headphones["values"] = list(self.monitor_map)
            if hasattr(self, "_update_settings_devices_view"):
                self._update_settings_devices_view()

            if hasattr(self, "track_source_var"):
                avail_sources = self._get_track_sources()
                if not self.track_source_var.get() or self.track_source_var.get() not in avail_sources:
                    self.track_source_var.set(avail_sources[0])

            cable = AudioEngine.find_cable_output()
            selected_out = next(
                (name for name, index in self.output_map.items() if index == cable),
                next(iter(self.output_map), ""),
            )
            if self.output_var.get() not in self.output_map:
                self.output_var.set(selected_out)

            if self.input_map:
                phys_mic = AudioEngine.find_physical_mic()
                best_input = next(
                    (name for name, index in self.input_map.items() if index == phys_mic),
                    next(iter(self.input_map)),
                )
                if self.input_var.get() not in self.input_map:
                    self.input_var.set(best_input)
                if self.mix_mic_var.get() not in self.input_map:
                    self.mix_mic_var.set(best_input)

            if self.monitor_var.get() not in self.monitor_map:
                default_mon = AudioEngine.find_default_monitor(exclude_device=cable)
                headphone = next(
                    (name for name, index in self.output_map.items() if index == default_mon),
                    next(
                        (
                            name
                            for name in self.output_map
                            if "headphone" in name.lower() or "навуш" in name.lower()
                        ),
                        "(Без моніторингу)",
                    ),
                )
                self.monitor_var.set(headphone)
            self.status_var.set(
                "VB-Cable знайдено."
                if cable is not None
                else "VB-Cable не знайдено: можна вибрати інший пристрій."
            )
        except AudioEngineError as exc:
            self.status_var.set(str(exc))

    def _ensure_engine(self) -> AudioEngine:
        out_dev = self.output_map.get(self.output_var.get())
        in_dev = self.input_map.get(self.mix_mic_var.get()) if self.mic_enabled.get() else None
        mon_dev = self.monitor_map.get(self.monitor_var.get())
        if not hasattr(self, "engine"):
            self.engine = AudioEngine(
                out_dev,
                in_dev,
                mon_dev,
                self._show_error,
                self._on_finished,
                self._on_track_title,
            )
            self.engine.on_position = self._on_position
        else:
            self.engine.set_output_device(out_dev)
            self.engine.set_monitor_device(mon_dev)
            self.engine.set_input_device(in_dev)
            self.engine.set_mic_muted(self.mic_muted.get())
        if not hasattr(self.engine, "_mock_name"):
            if hasattr(self.engine, "set_monitor_mic"):
                self.engine.set_monitor_mic(self.monitor_mic.get())
            if hasattr(self.engine, "set_mic_volume"):
                self.engine.set_mic_volume(self.mic_volume_var.get())
        self.engine.set_volume(self.volume_var.get())
        self.engine.set_mic_muted(self.mic_muted.get())
        return self.engine

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Оберіть папку з музикою")
        if not folder:
            return
        self.folder_var.set(folder)
        self.status_var.set("Сканування музичної бібліотеки...")
        threading.Thread(target=self._scan_folder, args=(folder,), daemon=True).start()

    def _add_files(self) -> None:
        ext_pattern = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_EXTENSIONS))
        files = filedialog.askopenfilenames(
            title="Додати аудіофайли до саундпада",
            filetypes=[("Аудіофайли", ext_pattern), ("Усі файли", "*.*")],
        )
        if not files:
            return
        existing_paths = {t.path for t in self.local_tracks}
        added = 0
        for file_path in files:
            if file_path in existing_paths:
                continue
            track = scan_file(file_path)
            if track is not None:
                self.local_tracks.append(track)
                existing_paths.add(track.path)
                added += 1
        if added:
            self._filter_tracks()
            self.status_var.set(f"Додано файлів: {added}. Усього треків: {len(self.local_tracks)}")
            self._save_state()

    def _scan_folder(self, folder: str) -> None:
        tracks = scan_folder(folder)
        self.after(0, lambda: self._set_tracks(tracks))

    def _set_tracks(self, tracks: list[LocalTrack]) -> None:
        hotkey_by_path = {t.path: t.hotkey for t in self.local_tracks if t.hotkey}
        custom_yt = [t for t in self.local_tracks if t.is_url]
        for t in tracks:
            if t.path in hotkey_by_path:
                t.hotkey = hotkey_by_path[t.path]
        seen = {t.path for t in tracks}
        for yt in custom_yt:
            if yt.path not in seen:
                tracks.append(yt)
        self.local_tracks = tracks
        self._filter_tracks()
        self.status_var.set(f"Знайдено треків: {len(tracks)}")
        self._save_state()

    def _filter_tracks(self) -> None:
        query = self.search_var.get().lower().strip()
        self.visible_tracks = [
            t
            for t in self.local_tracks
            if query in t.title.lower() or (t.hotkey and query in t.hotkey.lower())
        ]
        for item in self.track_list.get_children():
            self.track_list.delete(item)
        for index, track in enumerate(self.visible_tracks):
            is_playing = (
                self.is_playing
                and not self.is_paused
                and 0 <= self.current_index < len(self.local_tracks)
                and self.local_tracks[self.current_index] == track
            )
            icon_status = "▶" if is_playing else ""
            dur_str = (
                format_duration(track.duration)
                if (track.duration is not None and track.duration > 0.0)
                else ""
            )
            self.track_list.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    icon_status,
                    str(index + 1),
                    track.title,
                    dur_str,
                    track.hotkey,
                ),
            )
        self._update_stats_ui()

    def _on_tree_select(self, _event: Optional[tk.Event] = None) -> None:
        self._update_stats_ui()

    def _update_stats_ui(self) -> None:
        total = len(self.local_tracks)
        sel_count = len(self.track_list.selection())
        if hasattr(self, "category_count_lbl"):
            self.category_count_lbl.configure(text=str(total))
        if hasattr(self, "cat_yt_count_lbl"):
            yt_count = sum(
                1 for t in self.local_tracks if getattr(t, "is_url", False) or "youtube" in str(getattr(t, "path", "")).lower()
            )
            self.cat_yt_count_lbl.configure(text=str(yt_count) if yt_count > 0 else "🔍")
        if hasattr(self, "stats_sounds_lbl"):
            self.stats_sounds_lbl.configure(text=str(total))
        if hasattr(self, "stats_selected_lbl"):
            self.stats_selected_lbl.configure(text=str(sel_count))
        if hasattr(self, "stats_plays_lbl"):
            self.stats_plays_lbl.configure(text=str(self.playback_count))

    def _selected_track(self) -> tuple[LocalTrack, int] | None:
        selected = self.track_list.selection()
        if not selected:
            return None
        index = int(selected[0])
        if index < 0 or index >= len(self.visible_tracks):
            return None
        track = self.visible_tracks[index]
        return track, (self.local_tracks.index(track) if track in self.local_tracks else index)

    def _play_selected(self) -> None:
        selected = self._selected_track()
        if selected:
            track, self.current_index = selected
            self._play_local_track(track)
        elif self.visible_tracks:
            idx = self.current_index if (0 <= self.current_index < len(self.visible_tracks)) else 0
            track = self.visible_tracks[idx]
            self.current_index = self.local_tracks.index(track) if track in self.local_tracks else idx
            self._play_local_track(track)

    def _play_local_track(self, track: LocalTrack, start_pos: float = 0.0) -> None:
        self.playback_count += 1
        if track in self.visible_tracks:
            vis_id = str(self.visible_tracks.index(track))
            if self.track_list.exists(vis_id):
                self.track_list.selection_set(vis_id)
                self.track_list.see(vis_id)
        self.current_duration = track.duration
        self.seek_var.set(start_pos)
        self.seek_scale.configure(to=max(1.0, track.duration or 100.0))
        self.time_display_var.set(format_duration(start_pos))
        self.time_var.set(f"{format_duration(start_pos)} / {format_duration(track.duration)}")
        self._filter_tracks()
        is_url = bool(track.is_url and not Path(track.path).is_file())
        if is_url:
            self._start(StreamTrack(track.title, track.path, is_url=True, start_pos=start_pos))
            if not self._is_mocked_env():
                threading.Thread(
                    target=self._background_cache_youtube,
                    args=(track.path, False, 1.5),
                    daemon=True,
                ).start()
        else:
            self._start(StreamTrack(track.title, track.path, start_pos=start_pos))

    def _play_url(self) -> None:
        query = self.url_var.get().strip()
        if not query:
            return
        cached = AudioEngine.get_cached_file(query) if not self._is_mocked_env() else None
        if cached is not None:
            local_path, resolved_title, duration = cached
            self._upsert_youtube_track(resolved_title, local_path, duration, is_url=False)
            self.current_duration = duration
            self.seek_var.set(0.0)
            self.seek_scale.configure(to=max(1.0, duration or 100.0))
            self.time_display_var.set("0:00")
            self.time_var.set(f"0:00 / {format_duration(duration)}")
            self._start(StreamTrack(resolved_title, local_path, is_url=False, duration=duration))
            return

        self.current_duration = 0.0
        self.seek_var.set(0.0)
        self.seek_scale.configure(to=100.0)
        self.time_display_var.set("0:00")
        self.time_var.set("0:00 / 0:00")
        self.status_var.set(f"Відтворення з YouTube: {query}...")
        self._start(StreamTrack(query, query, is_url=True))
        if not self._is_mocked_env():
            threading.Thread(
                target=self._background_cache_youtube,
                args=(query, False, 1.5),
                daemon=True,
            ).start()

    def _add_url_to_list(self) -> None:
        query = self.url_var.get().strip()
        if not query:
            self.status_var.set("Введіть назву треку або URL YouTube для додавання.")
            return
        self.status_var.set(f"Завантаження та кешування з YouTube: {query}...")
        threading.Thread(
            target=self._background_cache_youtube,
            args=(query, True, 0.0),
            daemon=True,
        ).start()

    def _background_cache_youtube(
        self, query: str, notify_status: bool = False, delay_sec: float = 0.0
    ) -> None:
        if delay_sec > 0.0:
            time.sleep(delay_sec)
        try:
            local_path, title, duration = AudioEngine.cache_youtube_track(
                StreamTrack(query, query, is_url=True)
            )
            self._safe_ui_call(self._on_youtube_cached, query, title, local_path, duration, notify_status)
        except Exception as exc:
            if notify_status:
                self._safe_ui_call(lambda msg=f"Помилка кешування: {exc}": self.notify_error(msg))

    def _on_youtube_cached(
        self,
        original_query: str,
        title: str,
        local_path: str,
        duration: float,
        notify_status: bool,
    ) -> None:
        self._upsert_youtube_track(title, local_path, duration, is_url=False, match_query=original_query)
        if notify_status:
            self.status_var.set(f"Додано в саундпад (миттєвий старт): {title}")

    def _upsert_youtube_track(
        self,
        title: str,
        path: str,
        duration: float,
        is_url: bool = True,
        match_query: str = "",
    ) -> LocalTrack:
        for track in self.local_tracks:
            if track.path == path or (match_query and track.path == match_query) or track.title == title:
                track.title = title
                track.path = path
                if duration > 0:
                    track.duration = duration
                track.is_url = is_url
                self._filter_tracks()
                self._save_state()
                return track
        new_track = LocalTrack(title=title, path=path, duration=duration, is_url=is_url)
        self.local_tracks.append(new_track)
        self._filter_tracks()
        self._save_state()
        return new_track

    def _on_category_select(self, cat: Any = None, event: Any = None) -> None:
        """Handler for selecting a category in the sidebar, menus, or shortcuts."""
        if isinstance(cat, tk.Event) or hasattr(cat, "widget"):
            event = cat
            cat = None

        if cat is None and event is not None and hasattr(event, "widget"):
            w = event.widget
            yt_widgets = (
                getattr(self, "cat_yt_frame", None),
                getattr(self, "cat_yt_icon_lbl", None),
                getattr(self, "cat_yt_title_lbl", None),
                getattr(self, "cat_yt_count_lbl", None),
            )
            my_widgets = (
                getattr(self, "cat_item_frame", None),
                getattr(self, "cat_icon_lbl", None),
                getattr(self, "cat_title_lbl", None),
                getattr(self, "category_count_lbl", None),
            )
            if w in yt_widgets:
                cat = "youtube"
            elif w in my_widgets:
                cat = "my_sounds"

        if cat is None or str(cat).lower() not in ("youtube", "my_sounds"):
            cat = getattr(self, "current_category", "my_sounds")

        self._select_category(str(cat))

    def _on_category_change(self, cat: Any = None, event: Any = None) -> None:
        """Alias handler for category change."""
        self._on_category_select(cat, event)

    on_category_select = _on_category_select
    on_category_change = _on_category_change

    def _update_category_info_panel(self, cat: str | None = None) -> None:
        """Update bottom info panel icon, text, and counter visibility based on category and theme."""
        if cat is None:
            cat = getattr(self, "current_category", "my_sounds")
        c = self._get_theme_colors()

        if cat == "youtube":
            if hasattr(self, "info_house_lbl") and hasattr(self.info_house_lbl, "configure"):
                yt_img = self._icons.get("yt_icon")
                self.info_house_lbl.configure(
                    image=yt_img if yt_img else "",
                    text="▶",
                    fg="#ff1f1f",
                    bg=c["info_bg"],
                )
            if hasattr(self, "info_cat_name_lbl") and hasattr(self.info_cat_name_lbl, "configure"):
                self.info_cat_name_lbl.configure(
                    text="YouTube / YouTube Music",
                    bg=c["info_bg"],
                    fg=c["fg"],
                )
            if hasattr(self, "stats_frame") and hasattr(self.stats_frame, "pack_forget"):
                if self.stats_frame.winfo_manager() == "pack":
                    self.stats_frame.pack_forget()
        else:
            if hasattr(self, "info_house_lbl") and hasattr(self.info_house_lbl, "configure"):
                house_img = self._icons.get("house_lg")
                self.info_house_lbl.configure(
                    image=house_img if house_img else "",
                    text="🏠",
                    fg=c["accent"],
                    bg=c["info_bg"],
                )
            if hasattr(self, "info_cat_name_lbl") and hasattr(self.info_cat_name_lbl, "configure"):
                self.info_cat_name_lbl.configure(
                    text=self._tr("cat_my_sounds"),
                    bg=c["info_bg"],
                    fg=c["fg"],
                )
            if hasattr(self, "stats_frame"):
                if hasattr(self.stats_frame, "configure"):
                    self.stats_frame.configure(bg=c["info_bg"])
                if hasattr(self, "stats_r1") and hasattr(self.stats_r1, "configure"):
                    self.stats_r1.configure(bg=c["info_bg"])
                if hasattr(self, "stats_r2") and hasattr(self.stats_r2, "configure"):
                    self.stats_r2.configure(bg=c["info_bg"])
                if hasattr(self, "lbl_stats_sounds") and hasattr(self.lbl_stats_sounds, "configure"):
                    self.lbl_stats_sounds.configure(text=self._tr("info_sounds"))
                if hasattr(self, "lbl_stats_plays") and hasattr(self.lbl_stats_plays, "configure"):
                    self.lbl_stats_plays.configure(text=self._tr("info_plays"))
                if hasattr(self, "lbl_stats_sel") and hasattr(self.lbl_stats_sel, "configure"):
                    self.lbl_stats_sel.configure(text=self._tr("info_selected"))
                if hasattr(self, "stats_caption_labels"):
                    for lbl in self.stats_caption_labels:
                        if hasattr(lbl, "configure"):
                            lbl.configure(bg=c["info_bg"], fg=c["muted_fg"])
                if hasattr(self, "stats_sounds_lbl") and hasattr(self.stats_sounds_lbl, "configure"):
                    self.stats_sounds_lbl.configure(bg=c["info_bg"], fg=c["fg"])
                if hasattr(self, "stats_plays_lbl") and hasattr(self.stats_plays_lbl, "configure"):
                    self.stats_plays_lbl.configure(bg=c["info_bg"], fg=c["fg"])
                if hasattr(self, "stats_selected_lbl") and hasattr(self.stats_selected_lbl, "configure"):
                    self.stats_selected_lbl.configure(bg=c["info_bg"], fg=c["fg"])

                if self.stats_frame.winfo_manager() != "pack":
                    if hasattr(self, "info_cat_name_lbl") and self.info_cat_name_lbl.winfo_manager() == "pack":
                        self.stats_frame.pack(side="left", pady=8, after=self.info_cat_name_lbl)
                    else:
                        self.stats_frame.pack(side="left", pady=8)
                self._update_stats_ui()

    update_category_info_panel = _update_category_info_panel

    def _select_category(self, cat: str) -> None:
        self.current_category = cat
        c = self._get_theme_colors()
        if cat == "my_sounds":
            self.cat_item_frame.configure(bg=c["cat_sel_bg"])
            self.cat_icon_lbl.configure(bg=c["cat_sel_bg"], fg=c["accent"])
            self.cat_title_lbl.configure(bg=c["cat_sel_bg"], fg=c["cat_sel_fg"], font=("Segoe UI", 9, "bold"))
            self.category_count_lbl.configure(bg=c["cat_sel_bg"], fg=c["cat_sel_fg"])

            self.cat_yt_frame.configure(bg=c["cat_unsel_bg"])
            self.cat_yt_icon_lbl.configure(bg=c["cat_unsel_bg"])
            self.cat_yt_title_lbl.configure(bg=c["cat_unsel_bg"], fg=c["cat_unsel_fg"], font=("Segoe UI", 9))
            self.cat_yt_count_lbl.configure(bg=c["cat_unsel_bg"], fg=c["cat_unsel_fg"])

            self._update_category_info_panel("my_sounds")
            self._hide_yt_search_panel()
        elif cat == "youtube":
            self.cat_yt_frame.configure(bg=c["cat_sel_bg"])
            self.cat_yt_icon_lbl.configure(bg=c["cat_sel_bg"])
            self.cat_yt_title_lbl.configure(bg=c["cat_sel_bg"], fg=c["cat_sel_fg"], font=("Segoe UI", 9, "bold"))
            self.cat_yt_count_lbl.configure(bg=c["cat_sel_bg"], fg=c["cat_sel_fg"])

            self.cat_item_frame.configure(bg=c["cat_unsel_bg"])
            self.cat_icon_lbl.configure(bg=c["cat_unsel_bg"], fg=c["accent"])
            self.cat_title_lbl.configure(bg=c["cat_unsel_bg"], fg=c["cat_unsel_fg"], font=("Segoe UI", 9))
            self.category_count_lbl.configure(bg=c["cat_unsel_bg"], fg=c["cat_unsel_fg"])

            self._update_category_info_panel("youtube")
            self._show_yt_search_panel()

    def _on_cat_hover(self, cat: str, entering: bool) -> None:
        if getattr(self, "current_category", "") == cat:
            return
        c = self._get_theme_colors()
        bg = c["cat_hover_bg"] if entering else c["cat_unsel_bg"]
        if cat == "my_sounds":
            self.cat_item_frame.configure(bg=bg)
            self.cat_icon_lbl.configure(bg=bg)
            self.cat_title_lbl.configure(bg=bg)
            self.category_count_lbl.configure(bg=bg)
        elif cat == "youtube":
            self.cat_yt_frame.configure(bg=bg)
            self.cat_yt_icon_lbl.configure(bg=bg)
            self.cat_yt_title_lbl.configure(bg=bg)
            self.cat_yt_count_lbl.configure(bg=bg)

    def _show_yt_search_panel(self) -> None:
        if hasattr(self, "yt_search_frame"):
            self.yt_search_frame.place(x=0, y=26, relwidth=1.0, relheight=1.0, height=-26)
            self.yt_search_frame.lift()
            if hasattr(self, "yt_search_entry"):
                self.yt_search_entry.focus_set()
                self.yt_search_entry.select_range(0, tk.END)

    def _hide_yt_search_panel(self) -> None:
        if hasattr(self, "yt_search_frame"):
            self.yt_search_frame.place_forget()
        if getattr(self, "current_category", "") == "youtube":
            self._select_category("my_sounds")
        if hasattr(self, "track_list"):
            self.track_list.focus_set()

    def _update_yt_close_btn_color(self) -> None:
        if hasattr(self, "btn_yt_close_x") and self.btn_yt_close_x.winfo_exists():
            c = self._get_theme_colors()
            self.btn_yt_close_x.configure(bg=c["hdr_bg"], fg=c["muted_fg"])

    def _build_yt_search_panel(self, parent: tk.Widget) -> None:
        c = self._get_theme_colors()
        self.yt_search_frame = tk.Frame(parent, bg=c["table_bg"], bd=1, relief="solid")

        # 1. Custom Title bar authentic to Soundpad design
        self.yt_titlebar = tk.Frame(self.yt_search_frame, bg=c["hdr_bg"], height=28)
        self.yt_titlebar.pack(fill="x", side="top")
        self.yt_titlebar.pack_propagate(False)

        self.yt_title_icon = tk.Label(
            self.yt_titlebar,
            image=self._icons.get("yt_title_icon") or self._icons.get("headphones"),
            text="🎧",
            bg=c["hdr_bg"],
            fg=c["accent"],
        )
        self.yt_title_icon.pack(side="left", padx=(8, 6), pady=4)

        self.yt_title_lbl = tk.Label(
            self.yt_titlebar,
            text="Швидкий пошук YouTube / YouTube Music",
            bg=c["hdr_bg"],
            fg=c["fg"],
            font=("Segoe UI", 9, "bold"),
        )
        self.yt_title_lbl.pack(side="left", pady=4)

        self.btn_yt_close_x = tk.Button(
            self.yt_titlebar,
            text="✕",
            bg=c["hdr_bg"],
            fg=c["muted_fg"],
            activebackground="#e81123",
            activeforeground="#ffffff",
            bd=0,
            relief="flat",
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=8,
            command=self._hide_yt_search_panel,
        )
        self.btn_yt_close_x.pack(side="right", fill="y")
        self.btn_yt_close_x.bind("<Enter>", lambda _: self.btn_yt_close_x.configure(bg="#e81123", fg="#ffffff"))
        self.btn_yt_close_x.bind("<Leave>", lambda _: self._update_yt_close_btn_color())
        self.btn_close_x = self.btn_yt_close_x

        # 2. Search Bar Area
        self.yt_top = tk.Frame(self.yt_search_frame, bg=c["table_bg"])
        self.yt_top.pack(fill="x", padx=12, pady=(10, 4))

        self.yt_query_var = tk.StringVar(value="")
        self.yt_entry_border = tk.Frame(self.yt_top, bg=c["divider"], bd=1)
        self.yt_entry_border.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.yt_search_entry = tk.Entry(
            self.yt_entry_border,
            textvariable=self.yt_query_var,
            bg=c["entry_bg"],
            fg=c["entry_fg"],
            insertbackground=c["accent"],
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
        )
        self.yt_search_entry.pack(fill="x", ipady=4, padx=6)
        self.yt_search_entry.bind("<Return>", lambda _: self._perform_yt_search())
        self.url_entry = self.yt_search_entry

        self.btn_yt_search = tk.Button(
            self.yt_top,
            text="🔍 Знайти",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=12,
            pady=2,
            command=self._perform_yt_search,
        )
        self.btn_yt_search.pack(side="right")
        self.btn_search = self.btn_yt_search

        # 3. Status label
        self.yt_status_lbl = tk.Label(
            self.yt_search_frame,
            text="Введіть запит і натисніть Знайти",
            bg=c["table_bg"],
            fg=c["muted_fg"],
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.yt_status_lbl.pack(fill="x", padx=12, pady=(2, 4))

        # 4. Results Table with border
        self.yt_table_box = tk.Frame(self.yt_search_frame, bg=c["divider"], bd=1, relief="solid")
        self.yt_table_box.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.yt_tree = ttk.Treeview(
            self.yt_table_box,
            columns=("title", "duration"),
            show="headings",
            selectmode="browse",
        )
        self.yt_tree.heading("title", text="Трек / Відео")
        self.yt_tree.heading("duration", text="Тривалість")
        self.yt_tree.column("title", width=480, minwidth=200, stretch=True, anchor="w")
        self.yt_tree.column("duration", width=90, minwidth=70, stretch=False, anchor="center")

        yt_scroll = ttk.Scrollbar(self.yt_table_box, orient="vertical", command=self.yt_tree.yview)
        self.yt_tree.configure(yscrollcommand=yt_scroll.set)

        self.yt_tree.pack(side="left", fill="both", expand=True)
        yt_scroll.pack(side="right", fill="y")

        self.yt_tree.bind("<Double-1>", lambda _: self._yt_play_picked(add_only=False))
        self.yt_tree.bind("<Return>", lambda _: self._yt_play_picked(add_only=False))

        # 5. Bottom Actions Bar
        self.yt_bottom = tk.Frame(self.yt_search_frame, bg=c["table_bg"])
        self.yt_bottom.pack(fill="x", padx=12, pady=(4, 10))

        self.btn_yt_play = tk.Button(
            self.yt_bottom,
            text="▶ Відтворити вибране",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=10,
            pady=3,
            command=lambda: self._yt_play_picked(add_only=False),
        )
        self.btn_yt_play.pack(side="left")

        self.btn_yt_cache = tk.Button(
            self.yt_bottom,
            text="➕ Закешувати в саундпад (0мс старт)",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=10,
            pady=3,
            command=lambda: self._yt_play_picked(add_only=True),
        )
        self.btn_yt_cache.pack(side="left", padx=8)

        self.btn_yt_close = tk.Button(
            self.yt_bottom,
            text="Закрити",
            bg=c["top_bg"],
            fg=c["fg"],
            activebackground=c["active_menu_bg"],
            activeforeground=c["active_menu_fg"],
            bd=1,
            relief="solid",
            cursor="hand2",
            font=("Segoe UI", 9),
            padx=16,
            pady=3,
            command=self._hide_yt_search_panel,
        )
        self.btn_yt_close.pack(side="right")

    def _open_yt_search(self) -> None:
        query = self.url_var.get().strip()
        if query:
            self.yt_query_var.set(query)
        self._select_category("youtube")
        if query:
            self._perform_yt_search()

    def _perform_yt_search(self) -> None:
        q = self.yt_query_var.get().strip()
        if not q:
            return
        self.yt_status_lbl.configure(text=f"Пошук на YouTube: {q}...")

        def worker() -> None:
            try:
                items = AudioEngine.search_youtube(q, limit=10)
                self._safe_ui_call(self._render_yt_results, items)
            except Exception as exc:
                self._safe_ui_call(self._render_yt_results, [], str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _render_yt_results(self, items: list[dict[str, Any]], err: str = "") -> None:
        self.yt_results_data = items
        for row in self.yt_tree.get_children():
            self.yt_tree.delete(row)
        if err:
            self.yt_status_lbl.configure(text=f"Помилка пошуку: {err}")
            return
        for idx, item in enumerate(items):
            self.yt_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(item.get("title", ""), format_duration(float(item.get("duration", 0.0)))),
            )
        self.yt_status_lbl.configure(text=f"Знайдено результатів: {len(items)}")
        if items:
            self.yt_tree.selection_set("0")
            self.yt_tree.focus("0")

    def _yt_play_picked(self, add_only: bool = False) -> None:
        sel = self.yt_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self.yt_results_data):
            return
        item = self.yt_results_data[idx]
        title = item.get("title", "")
        url = item.get("url", "")
        duration = float(item.get("duration", 0.0))

        self.url_var.set(url)
        self._upsert_youtube_track(title, url, duration, is_url=True)

        if add_only:
            self._add_url_to_list()
            self.yt_status_lbl.configure(text=f"Закешування розпочато: {title}...")
        else:
            self._play_url()
            self.yt_status_lbl.configure(text=f"Відтворення: {title}")

    def _prompt_play_url(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Швидке посилання YouTube / URL")
        dlg.geometry("520x160")
        dlg.resizable(False, False)
        dlg.configure(bg="#1f1f1f" if self.theme == "dark" else "#f4f5f7")
        dlg.transient(self)
        self._apply_dark_title_bar(dlg)

        top = ttk.Frame(dlg, padding=14)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="Введіть посилання YouTube або назву треку:").pack(anchor="w", pady=(0, 6))

        entry = ttk.Entry(top, textvariable=self.url_var, width=54)
        entry.pack(fill="x", pady=(0, 12))
        entry.focus_set()

        btns = ttk.Frame(top)
        btns.pack(fill="x")

        def on_play() -> None:
            dlg.destroy()
            self._play_url()

        def on_add() -> None:
            dlg.destroy()
            self._add_url_to_list()

        ttk.Button(btns, text="▶ Відтворити", command=on_play).pack(side="left")
        ttk.Button(btns, text="➕ Додати в саундпад", command=on_add).pack(side="left", padx=8)
        ttk.Button(btns, text="Скасувати", command=dlg.destroy).pack(side="right")
        entry.bind("<Return>", lambda _: on_play())

    def _start(self, track: StreamTrack) -> None:
        try:
            engine = self._ensure_engine()
            engine.play(track)
            self.is_playing = True
            self.is_paused = False
            self.pause_button.configure(text=self._tr("btn_pause"))
            self.now_playing_var.set(self._tr("now_playing", track.title))
            self.status_var.set(self._tr("playing", track.title))
            self._filter_tracks()
        except AudioEngineError as exc:
            self._show_error(str(exc))

    def _toggle_pause(self) -> None:
        if not hasattr(self, "engine") or (
            getattr(self.engine, "_process", None) is None
            and getattr(self.engine, "_active_track", None) is None
            and not self.is_paused
        ):
            self._play_selected()
            return
        if self.is_paused:
            if hasattr(self, "engine") and getattr(self.engine, "_active_track", None) is None:
                selected = self._selected_track()
                curr_track = selected[0] if selected else (self.local_tracks[self.current_index] if 0 <= self.current_index < len(self.local_tracks) else None)
                if curr_track is not None:
                    pos = float(self.seek_var.get())
                    self.is_paused = False
                    self.is_playing = True
                    self._play_local_track(curr_track, start_pos=pos)
                    return
            self.engine.resume()
            self.is_paused = False
            self.is_playing = True
            self.pause_button.configure(text=self._tr("btn_pause"))
        else:
            self.engine.pause()
            self.is_paused = True
            self.pause_button.configure(text=self._tr("btn_resume"))
        self._filter_tracks()

    def _stop(self) -> None:
        if hasattr(self, "engine"):
            self.engine.stop()
        self.is_playing = False
        self.is_paused = False
        self.seek_var.set(0.0)
        self.time_display_var.set("0:00")
        self.time_var.set(f"0:00 / {format_duration(self.current_duration)}")
        self.now_playing_var.set(self._tr("nothing_playing"))
        self.status_var.set("")
        self.pause_button.configure(text=self._tr("btn_play"))
        self._filter_tracks()

    def _next(self) -> None:
        if self.local_tracks:
            self.current_index = (self.current_index + 1) % len(self.local_tracks)
            track = self.local_tracks[self.current_index]
            self._play_local_track(track)

    def _previous(self) -> None:
        if self.local_tracks:
            self.current_index = (self.current_index - 1) % len(self.local_tracks)
            track = self.local_tracks[self.current_index]
            self._play_local_track(track)

    def _seek_forward(self) -> None:
        target = min(self.current_duration, float(self.seek_var.get()) + 5.0)
        self.seek_var.set(target)
        if hasattr(self, "engine") and hasattr(self.engine, "seek"):
            self.engine.seek(target)
            self.time_display_var.set(format_duration(target))
            self.time_var.set(f"{format_duration(target)} / {format_duration(self.current_duration)}")

    def _toggle_loop(self) -> None:
        val = not self.loop_enabled.get()
        self.loop_enabled.set(val)
        self.status_var.set("Повторення звуку (Loop): увімкнено." if val else "Повторення звуку (Loop): вимкнено.")

    def _update_mute_icon(self, *args: Any) -> None:
        if not hasattr(self, "btn_tb_mute") or not hasattr(self, "_icons"):
            return
        is_muted = False
        try:
            is_muted = float(self.volume_var.get()) <= 0.001
        except Exception:
            pass
        icon_name = "note_mute" if is_muted else "note"
        fallback_icon = "mute" if is_muted else "headphones"
        img = self._icons.get(icon_name) or self._icons.get(fallback_icon)
        if img:
            self.btn_tb_mute.configure(image=img, text="")
        else:
            c = self._get_theme_colors()
            self.btn_tb_mute.configure(
                image="",
                text="♪✕" if is_muted else "♪",
                fg="#ff2020" if is_muted else c["accent"],
            )

    def _toggle_mute(self) -> None:
        curr = float(self.volume_var.get())
        if curr > 0.001:
            self._prev_volume = curr
            self.volume_var.set(0.0)
            self._volume_changed("0.0")
            self.status_var.set("Звук вимкнено (Mute).")
        else:
            prev = getattr(self, "_prev_volume", 1.0) or 1.0
            if prev <= 0.001:
                prev = 1.0
            self.volume_var.set(prev)
            self._volume_changed(str(prev))
            self.status_var.set("Звук увімкнено.")
        self._update_mute_icon()

    def _toggle_hotkeys_active(self) -> None:
        self._hotkeys_enabled = not getattr(self, "_hotkeys_enabled", True)
        self.hotkeys_enabled.set(self._hotkeys_enabled)
        self.status_var.set("Глобальні гарячі клавіші увімкнено." if self._hotkeys_enabled else "Глобальні гарячі клавіші вимкнено.")

    def _toggle_auto_keys(self) -> None:
        val = not self.auto_keys_enabled.get()
        self.auto_keys_enabled.set(val)
        self.status_var.set("Auto-Keys увімкнено." if val else "Auto-Keys вимкнено.")

    def _on_seek_press(self, _event: tk.Event) -> None:
        self._seeking_drag = True
        target = float(self.seek_var.get())
        if getattr(self, "current_duration", 0.0) > 0.0:
            target = max(0.0, min(self.current_duration, target))
            self.seek_var.set(target)
        self.time_display_var.set(format_duration(target))
        engine = getattr(self, "engine", None)
        if engine is not None and hasattr(engine, "seek"):
            try:
                engine.seek(target)
            except Exception:
                pass
            if self.is_paused and getattr(engine, "_active_track", None) is not None:
                self.is_paused = False
                self.is_playing = True
                self.pause_button.configure(text=self._tr("btn_pause"))

    def _on_seek_motion(self, _event: tk.Event) -> None:
        if self._seeking_drag:
            target = float(self.seek_var.get())
            if getattr(self, "current_duration", 0.0) > 0.0:
                target = max(0.0, min(self.current_duration, target))
                self.seek_var.set(target)
            self.time_display_var.set(format_duration(target))
            self.time_var.set(f"{format_duration(target)} / {format_duration(self.current_duration)}")

    def _on_seek_release(self, _event: tk.Event) -> None:
        self._seeking_drag = False
        target = float(self.seek_var.get())
        if getattr(self, "current_duration", 0.0) > 0.0:
            target = max(0.0, min(self.current_duration, target))
            self.seek_var.set(target)
        self.time_display_var.set(format_duration(target))
        self.time_var.set(f"{format_duration(target)} / {format_duration(self.current_duration)}")
        engine = getattr(self, "engine", None)
        if engine is not None and hasattr(engine, "seek"):
            try:
                engine.seek(target)
            except Exception:
                pass
        active = getattr(engine, "_active_track", None) if engine is not None else None
        if active is not None:
            if self.is_paused:
                self.is_paused = False
                self.is_playing = True
                self.pause_button.configure(text=self._tr("btn_pause"))
        else:
            curr_track = None
            selected = self._selected_track() if hasattr(self, "_selected_track") else None
            if selected:
                curr_track = selected[0]
            elif hasattr(self, "current_index") and 0 <= self.current_index < len(getattr(self, "local_tracks", [])):
                curr_track = self.local_tracks[self.current_index]
            if curr_track is not None:
                self.is_paused = False
                self.is_playing = True
                self._play_local_track(curr_track, start_pos=target)

    def _on_position(self, pos_seconds: float) -> None:
        if self._seeking_drag:
            return
        self.after(0, lambda: self._update_position_ui(pos_seconds))

    def _update_position_ui(self, pos_seconds: float) -> None:
        if self._seeking_drag:
            return
        if hasattr(self, "engine") and getattr(self.engine, "_active_track", None) is None:
            return
        active = getattr(getattr(self, "engine", None), "_active_track", None)
        try:
            act_dur = float(getattr(active, "duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            act_dur = 0.0
        if active is not None and act_dur > 0.0 and self.current_duration <= 0.0:
            self.current_duration = act_dur
        dur = max(self.current_duration, pos_seconds, 1.0)
        self.seek_scale.configure(to=dur)
        self.seek_var.set(min(pos_seconds, dur))
        self.time_display_var.set(format_duration(pos_seconds))
        self.time_var.set(f"{format_duration(pos_seconds)} / {format_duration(self.current_duration)}")

    def _volume_changed(self, value: str) -> None:
        if hasattr(self, "engine"):
            self.engine.set_volume(float(value))
        self._update_mute_icon()

    def _mic_volume_changed(self, value: str) -> None:
        if hasattr(self, "engine") and hasattr(self.engine, "set_mic_volume"):
            self.engine.set_mic_volume(float(value))

    def _mic_setting_changed(self) -> None:
        if self.mic_enabled.get() or hasattr(self, "engine"):
            engine = self._ensure_engine()
            engine.set_input_device(
                self.input_map.get(self.mix_mic_var.get()) if self.mic_enabled.get() else None
            )
            engine.set_mic_muted(self.mic_muted.get())
            if hasattr(engine, "set_monitor_mic"):
                engine.set_monitor_mic(self.monitor_mic.get())
            self.status_var.set(
                "Мікрофонний потік працює постійно."
                if self.mic_enabled.get()
                else "Мікрофонний потік вимкнено."
            )
            self._save_state()

    def _output_changed(self, _event: Optional[tk.Event] = None) -> None:
        if hasattr(self, "engine"):
            self.engine.set_output_device(self.output_map.get(self.output_var.get()))
        self.status_var.set(f"Пристрій відтворення змінено: {self.output_var.get()}")
        self._save_state()

    def _input_changed(self, _event: Optional[tk.Event] = None) -> None:
        chosen = self.input_var.get()
        if chosen in self.input_map and self.mix_mic_var.get() != chosen:
            self.mix_mic_var.set(chosen)
        if hasattr(self, "engine") and self.mic_enabled.get():
            self.engine.set_input_device(self.input_map.get(chosen))
        self.status_var.set(f"Мікрофон змінено: {chosen}")
        self._save_state()

    def _mix_mic_changed(self, _event: Optional[tk.Event] = None) -> None:
        chosen = self.mix_mic_var.get()
        if chosen in self.input_map and self.input_var.get() != chosen:
            self.input_var.set(chosen)
        if hasattr(self, "engine") and self.mic_enabled.get():
            self.engine.set_input_device(self.input_map.get(chosen))
            self.status_var.set("Мікрофон змінено та застосовано.")
        self._save_state()

    def _monitor_changed(self, _event: Optional[tk.Event] = None) -> None:
        if hasattr(self, "engine"):
            self.engine.set_monitor_device(self.monitor_map.get(self.monitor_var.get()))
            self.status_var.set("Моніторинг змінено та застосовано.")
        self._save_state()

    def _assign_hotkey_dialog(self) -> None:
        selected = self._selected_track()
        if not selected:
            self.status_var.set("Спочатку виберіть трек у списку для призначення гарячої клавіші.")
            return
        track, _ = selected
        self._capturing_hotkey = True
        dlg = tk.Toplevel(self)
        dlg.title("Призначити гарячу клавішу")
        dlg.geometry("420x160")
        dlg.resizable(False, False)
        dlg.configure(bg="#1f1f1f" if self.theme == "dark" else "#f4f5f7")
        dlg.transient(self)
        dlg.grab_set()
        self._apply_dark_title_bar(dlg)

        ttk.Label(
            dlg,
            text=f"Трек: {track.title[:45]}",
            font=("Segoe UI", 10, "bold"),
            padding=(16, 14, 16, 4),
        ).pack(fill="x")
        ttk.Label(
            dlg,
            text="Натисніть бажану клавішу (напр. F1..F7, Num1, Alt+1)\nабо Esc для скасування:",
            padding=(16, 4),
        ).pack(fill="x")

        def on_key(event: tk.Event) -> None:
            hk = _format_tk_hotkey(event)
            if not hk:
                return
            self._capturing_hotkey = False
            if hk == "ESCAPE":
                dlg.destroy()
                return
            for other in self.local_tracks:
                if other is not track and other.hotkey.upper() == hk.upper():
                    other.hotkey = ""
            track.hotkey = hk
            self._filter_tracks()
            self.status_var.set(f"Призначено гарячу клавішу [{hk}] для «{track.title}»")
            self._save_state()
            dlg.destroy()

        dlg.bind("<KeyPress>", on_key)
        dlg.protocol(
            "WM_DELETE_WINDOW",
            lambda: (setattr(self, "_capturing_hotkey", False), dlg.destroy()),
        )
        dlg.focus_force()

    def _clear_hotkey_selected(self) -> None:
        selected = self._selected_track()
        if not selected:
            return
        track, _ = selected
        track.hotkey = ""
        self._filter_tracks()
        self.status_var.set(f"Гарячу клавішу скинуто для «{track.title}»")
        self._save_state()

    def _clear_all_tracks(self) -> None:
        if not self.local_tracks:
            return
        if messagebox.askyesno("Очистити список", "Ви дійсно бажаєте очистити весь список звуків?"):
            self.local_tracks.clear()
            self.visible_tracks.clear()
            self.current_index = -1
            self._filter_tracks()
            self._save_state()
            self.status_var.set("Список звуків очищено.")

    def _remove_selected_track(self) -> None:
        selected = self._selected_track()
        if not selected:
            return
        track, idx = selected
        del self.local_tracks[idx]
        if not self.local_tracks:
            self.current_index = -1
        elif idx < self.current_index:
            self.current_index -= 1
        elif self.current_index >= len(self.local_tracks):
            self.current_index = len(self.local_tracks) - 1
        self._filter_tracks()
        self.status_var.set(f"Видалено зі списку: {track.title}")
        self._save_state()

    def _start_hotkey_listener(self) -> None:
        if sys.platform != "win32" or self._is_mocked_env():
            return
        threading.Thread(
            target=self._poll_global_hotkeys,
            name="musicpad-hotkeys",
            daemon=True,
        ).start()

    def _is_typing_in_entry(self) -> bool:
        try:
            focused = self.focus_get()
            return isinstance(focused, (tk.Entry, ttk.Entry))
        except Exception:
            return False

    @staticmethod
    def _is_typing_conflict_hotkey(hk: str) -> bool:
        upper = hk.strip().upper()
        if "CTRL+" in upper or "ALT+" in upper:
            return False
        parts = [p.strip() for p in upper.split("+") if p.strip()]
        main_key = parts[-1] if parts else ""
        if main_key.startswith("F") and main_key[1:].isdigit():
            return False
        return True

    def _poll_global_hotkeys(self) -> None:
        while not self._exiting:
            time.sleep(0.035)
            if self._capturing_hotkey or not getattr(self, "_hotkeys_enabled", True):
                continue
            try:
                active_now: set[str] = set()

                for builtin_hk, action in (
                    ("F8", self._toggle_pause),
                    ("F9", self._stop),
                    ("F10", self._toggle_mute_hotkey),
                ):
                    if _is_hotkey_pressed_win32(builtin_hk):
                        active_now.add(builtin_hk)
                        if builtin_hk not in self._pressed_hotkeys:
                            self.after(0, action)

                for idx, track in enumerate(list(self.local_tracks)):
                    hk = track.hotkey.strip().upper()
                    if not hk:
                        continue
                    if _is_hotkey_pressed_win32(hk):
                        active_now.add(hk)
                        if hk not in self._pressed_hotkeys:
                            self.after(0, lambda t=track, i=idx: self._trigger_hotkey_track(t, i))

                self._pressed_hotkeys = active_now
            except Exception:
                pass

    def _toggle_mute_hotkey(self) -> None:
        self.mic_muted.set(not self.mic_muted.get())
        self._mic_setting_changed()

    def _trigger_hotkey_track(self, track: LocalTrack, index: int) -> None:
        if self._is_typing_in_entry() and self._is_typing_conflict_hotkey(track.hotkey):
            return
        self.current_index = index
        self._play_local_track(track)

    def _show_hotkeys_help(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Гарячі клавіші Soundpad")
        dlg.geometry("480x360")
        dlg.resizable(False, False)
        dlg.configure(bg="#1f1f1f" if self.theme == "dark" else "#f4f5f7")
        dlg.transient(self)
        self._apply_dark_title_bar(dlg)

        f = ttk.Frame(dlg, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Гарячі клавіші керування", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))

        keys = [
            ("F8", "Відтворити / Пауза поточного звуку"),
            ("F9", "Зупинити відтворення (Стоп)"),
            ("F10", "Mute / Unmute мікрофона"),
            ("Пробіл", "Пауза / Продовжити"),
            ("Enter", "Відтворити вибраний звук"),
            ("Delete", "Видалити вибраний звук зі списку"),
            ("Ctrl+O", "Додати аудіофайли"),
            ("Ctrl+F", "Швидкий пошук у списку"),
            ("Ctrl+Y", "Пошук YouTube Music"),
            ("F1..F12, Num1..Num9", "Призначені клавіші для окремих звуків"),
        ]
        for k, desc in keys:
            row = ttk.Frame(f)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=k, font=("Segoe UI", 9, "bold"), width=18).pack(side="left")
            ttk.Label(row, text=desc).pack(side="left")

        ttk.Button(f, text="Зрозуміло", command=dlg.destroy).pack(side="bottom", anchor="e", pady=(10, 0))

    def _show_about(self) -> None:
        messagebox.showinfo(
            "Про MusicPad",
            "MusicPad — професійний саундпад для Windows у стилі Soundpad.\n\n"
            "• Автентичний інтерфейс Soundpad\n"
            "• Інтеграція з VB-Cable та мікрофоном\n"
            "• Моніторинг у навушниках\n"
            "• YouTube Music стрімінг та кешування (0 мс старт)\n"
            "• Глобальні гарячі клавіші (F1..F12, NumPad, Alt, Ctrl, Shift)\n"
            "• Робота у фоні та системному треї",
        )

    def _save_state(self) -> None:
        if self._is_mocked_env():
            return
        try:
            data = {
                "folder": self.folder_var.get(),
                "output": self.output_var.get(),
                "mix_mic": self.mix_mic_var.get(),
                "monitor": self.monitor_var.get(),
                "track_source": self.track_source_var.get() if hasattr(self, "track_source_var") else DEFAULT_TRACK_SOURCE,
                "volume": self.volume_var.get(),
                "mic_volume": self.mic_volume_var.get(),
                "mic_enabled": self.mic_enabled.get(),
                "monitor_mic": self.monitor_mic.get(),
                "hide_to_tray": self.hide_to_tray.get(),
                "hotkeys_enabled": self.hotkeys_enabled.get(),
                "auto_keys_enabled": self.auto_keys_enabled.get(),
                "loop_enabled": self.loop_enabled.get(),
                "playback_count": self.playback_count,
                "theme": self.theme,
                "tracks": [
                    {
                        "title": t.title,
                        "path": t.path,
                        "duration": t.duration,
                        "hotkey": t.hotkey,
                        "is_url": t.is_url,
                    }
                    for t in self.local_tracks
                ],
            }
            if hasattr(self, "remember_playback_state_var") and self.remember_playback_state_var.get():
                active = getattr(getattr(self, "engine", None), "_active_track", None)
                sel = self._selected_track()
                curr_track = None
                if active is not None:
                    for t in self.local_tracks:
                        if t.path == active.source or t.title == active.title:
                            curr_track = t
                            break
                    if curr_track is None:
                        curr_track = LocalTrack(
                            title=active.title,
                            path=active.source,
                            duration=float(active.duration or 0.0),
                            is_url=bool(active.is_url),
                        )
                elif sel is not None:
                    curr_track = sel[0]
                elif 0 <= self.current_index < len(self.local_tracks):
                    curr_track = self.local_tracks[self.current_index]

                if curr_track is not None:
                    if getattr(self, "is_paused", False):
                        status = "paused"
                    elif getattr(self, "is_playing", False) or active is not None:
                        status = "playing"
                    else:
                        status = "stopped"

                    if active is not None and hasattr(self, "engine") and hasattr(self.engine, "get_position"):
                        try:
                            saved_pos = float(self.engine.get_position() or self.seek_var.get())
                        except Exception:
                            saved_pos = float(self.seek_var.get())
                    else:
                        saved_pos = float(self.seek_var.get())

                    data["playback_state"] = {
                        "title": curr_track.title,
                        "path": curr_track.path,
                        "duration": curr_track.duration,
                        "is_url": getattr(curr_track, "is_url", False),
                        "position": saved_pos,
                        "volume": float(self.volume_var.get()),
                        "status": status,
                    }
                else:
                    data["playback_state"] = None
            else:
                data["playback_state"] = None

            if hasattr(self, "norm_volume_val"):
                data["settings_custom"] = {
                    "norm_volume": self.norm_volume_val.get(),
                    "norm_mode": self.norm_mode_var.get(),
                    "special_hotkeys": getattr(self, "special_hotkeys_dict", {}),
                    "numpad_alt": self.numpad_alt_var.get(),
                    "numpad_ctrl_alt": self.numpad_ctrl_alt_var.get(),
                    "auto_keys": getattr(self, "auto_keys_list", []),
                    "pass_hotkeys": self.pass_hotkeys_var.get(),
                    "auto_stop": self.auto_stop_var.get(),
                    "allow_lr_modifiers": self.allow_lr_modifiers_var.get(),
                    "same_hotkeys_all_categories": self.same_hotkeys_all_categories_var.get(),
                    "lang": self.lang_var.get(),
                    "date_format": self.date_format_var.get(),
                    "tb_style": self.tb_style_var.get(),
                    "tb_scale": self.tb_scale_var.get(),
                    "tb_size": self.tb_size_var.get(),
                    "tb_color": self.tb_color_var.get(),
                    "pos_refresh_rate": self.pos_refresh_rate_var.get(),
                    "show_mic_level": self.show_mic_level_var.get(),
                    "linear_volume_slider": self.linear_volume_slider_var.get(),
                    "allow_resume_other_modes": self.allow_resume_other_modes_var.get(),
                    "hotkey_switch_mode": self.hotkey_switch_mode_var.get(),
                    "launch_on_startup": self.launch_on_startup_var.get(),
                    "start_minimized": self.start_minimized_var.get(),
                    "show_tray_icon": self.show_tray_icon_var.get(),
                    "minimize_to_tray": self.minimize_to_tray_var.get(),
                    "minimize_instead_of_close": self.minimize_instead_of_close_var.get(),
                    "always_on_top": self.always_on_top_var.get(),
                    "disable_track_sorting": self.disable_track_sorting_var.get(),
                    "alternate_row_colors": self.alternate_row_colors_var.get(),
                    "typing_starts_search": self.typing_starts_search_var.get(),
                    "tts_input_priority": self.tts_input_priority_var.get(),
                    "remember_playback_state": self.remember_playback_state_var.get(),
                    "check_device_config_on_launch": self.check_device_config_on_launch_var.get(),
                    "recording_devices_checked": getattr(self, "recording_devices_checked", {}),
                    "track_source": self.track_source_var.get() if hasattr(self, "track_source_var") else DEFAULT_TRACK_SOURCE,
                }
            self._config_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_saved_state(self) -> None:
        if self._is_mocked_env():
            return
        cfg_file = self._config_path()
        if not cfg_file.is_file():
            return
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if data.get("output") in self.output_map:
                self.output_var.set(data["output"])
            saved_mic = data.get("mix_mic", "")
            if saved_mic in self.input_map and "cable output" not in saved_mic.lower():
                self.mix_mic_var.set(saved_mic)
                self.input_var.set(saved_mic)
            if data.get("monitor") in self.monitor_map:
                self.monitor_var.set(data["monitor"])
            if "track_source" in data and hasattr(self, "track_source_var"):
                self.track_source_var.set(str(data["track_source"]))
            if "volume" in data:
                self.volume_var.set(float(data["volume"]))
            if "mic_volume" in data:
                self.mic_volume_var.set(float(data["mic_volume"]))
            if "hide_to_tray" in data:
                self.hide_to_tray.set(bool(data["hide_to_tray"]))
            if "hotkeys_enabled" in data:
                self.hotkeys_enabled.set(bool(data["hotkeys_enabled"]))
            if "auto_keys_enabled" in data:
                self.auto_keys_enabled.set(bool(data["auto_keys_enabled"]))
            if "loop_enabled" in data:
                self.loop_enabled.set(bool(data["loop_enabled"]))
            if "playback_count" in data:
                self.playback_count = int(data["playback_count"])
            if "monitor_mic" in data:
                self.monitor_mic.set(bool(data["monitor_mic"]))
            if data.get("theme") in ("dark", "light") and data["theme"] != self.theme:
                self.theme = data["theme"]
                self._apply_theme()
            sc = data.get("settings_custom", {})
            if sc and hasattr(self, "norm_volume_val"):
                if "norm_volume" in sc: self.norm_volume_val.set(sc["norm_volume"])
                if "norm_mode" in sc: self.norm_mode_var.set(sc["norm_mode"])
                if "special_hotkeys" in sc and isinstance(sc["special_hotkeys"], dict):
                    self.special_hotkeys_dict.update(sc["special_hotkeys"])
                if "numpad_alt" in sc: self.numpad_alt_var.set(bool(sc["numpad_alt"]))
                if "numpad_ctrl_alt" in sc: self.numpad_ctrl_alt_var.set(bool(sc["numpad_ctrl_alt"]))
                if "auto_keys" in sc and isinstance(sc["auto_keys"], list):
                    self.auto_keys_list = [tuple(item) for item in sc["auto_keys"] if len(item) == 2]
                if "pass_hotkeys" in sc: self.pass_hotkeys_var.set(bool(sc["pass_hotkeys"]))
                if "auto_stop" in sc: self.auto_stop_var.set(bool(sc["auto_stop"]))
                if "allow_lr_modifiers" in sc: self.allow_lr_modifiers_var.set(bool(sc["allow_lr_modifiers"]))
                if "same_hotkeys_all_categories" in sc: self.same_hotkeys_all_categories_var.set(bool(sc["same_hotkeys_all_categories"]))
                if "lang" in sc: self.lang_var.set(str(sc["lang"]))
                if "date_format" in sc: self.date_format_var.set(str(sc["date_format"]))
                if "tb_style" in sc: self.tb_style_var.set(str(sc["tb_style"]))
                if "tb_scale" in sc: self.tb_scale_var.set(str(sc["tb_scale"]))
                if "tb_size" in sc: self.tb_size_var.set(str(sc["tb_size"]))
                if "tb_color" in sc: self.tb_color_var.set(str(sc["tb_color"]))
                if "pos_refresh_rate" in sc: self.pos_refresh_rate_var.set(int(sc["pos_refresh_rate"]))
                if "show_mic_level" in sc: self.show_mic_level_var.set(bool(sc["show_mic_level"]))
                if "linear_volume_slider" in sc: self.linear_volume_slider_var.set(bool(sc["linear_volume_slider"]))
                if "allow_resume_other_modes" in sc: self.allow_resume_other_modes_var.set(bool(sc["allow_resume_other_modes"]))
                if "hotkey_switch_mode" in sc: self.hotkey_switch_mode_var.set(str(sc["hotkey_switch_mode"]))
                if "launch_on_startup" in sc: self.launch_on_startup_var.set(bool(sc["launch_on_startup"]))
                if "start_minimized" in sc: self.start_minimized_var.set(bool(sc["start_minimized"]))
                if "show_tray_icon" in sc: self.show_tray_icon_var.set(bool(sc["show_tray_icon"]))
                if "minimize_to_tray" in sc: self.minimize_to_tray_var.set(bool(sc["minimize_to_tray"]))
                if "minimize_instead_of_close" in sc: self.minimize_instead_of_close_var.set(bool(sc["minimize_instead_of_close"]))
                if "always_on_top" in sc: self.always_on_top_var.set(bool(sc["always_on_top"]))
                if "disable_track_sorting" in sc: self.disable_track_sorting_var.set(bool(sc["disable_track_sorting"]))
                if "alternate_row_colors" in sc: self.alternate_row_colors_var.set(bool(sc["alternate_row_colors"]))
                if "typing_starts_search" in sc: self.typing_starts_search_var.set(bool(sc["typing_starts_search"]))
                if "tts_input_priority" in sc: self.tts_input_priority_var.set(bool(sc["tts_input_priority"]))
                if "remember_playback_state" in sc: self.remember_playback_state_var.set(bool(sc["remember_playback_state"]))
                if "check_device_config_on_launch" in sc: self.check_device_config_on_launch_var.set(bool(sc["check_device_config_on_launch"]))
                if "recording_devices_checked" in sc and isinstance(sc["recording_devices_checked"], dict):
                    self.recording_devices_checked.update(sc["recording_devices_checked"])
                if "track_source" in sc and hasattr(self, "track_source_var"):
                    self.track_source_var.set(str(sc["track_source"]))
            folder = data.get("folder")
            folder_valid = bool(folder and folder != "Папку не вибрано" and Path(folder).is_dir())
            if folder_valid:
                self.folder_var.set(folder)
            saved_tracks = data.get("tracks") or []
            loaded: list[LocalTrack] = []
            pruned = bool(folder and folder != "Папку не вибрано" and not folder_valid)
            for item in saved_tracks:
                p = str(item.get("path", "")).strip()
                is_url = bool(item.get("is_url", False))
                if not p:
                    pruned = True
                    continue
                if not is_url and not Path(p).is_file():
                    relocated = AudioEngine._cache_dir() / Path(p).name
                    if relocated.is_file():
                        p = str(relocated)
                        pruned = True
                if is_url or Path(p).is_file():
                    loaded.append(
                        LocalTrack(
                            title=str(item.get("title") or Path(p).stem),
                            path=p,
                            duration=float(item.get("duration") or 0.0),
                            hotkey=str(item.get("hotkey") or ""),
                            is_url=is_url,
                        )
                    )
                else:
                    pruned = True
            if not loaded and folder_valid and folder:
                loaded = scan_folder(folder)
            if loaded:
                self.local_tracks = loaded
                self._filter_tracks()
            if hasattr(self, "lang_var"):
                self._apply_language()
            if hasattr(self, "remember_playback_state_var") and self.remember_playback_state_var.get():
                ps = data.get("playback_state")
                if isinstance(ps, dict):
                    if "volume" in ps and hasattr(self, "volume_var"):
                        try:
                            self.volume_var.set(float(ps["volume"]))
                        except Exception:
                            pass
                    target_path = ps.get("path")
                    target_title = ps.get("title")
                    target_pos = float(ps.get("position") or 0.0)
                    target_duration = float(ps.get("duration") or 0.0)
                    target_status = ps.get("status", "stopped")

                    matched_idx = -1
                    for idx, t in enumerate(self.local_tracks):
                        if (target_path and t.path == target_path) or (target_title and t.title == target_title):
                            matched_idx = idx
                            break

                    if matched_idx >= 0:
                        self.current_index = matched_idx
                        track = self.local_tracks[matched_idx]
                        self.current_duration = track.duration or target_duration
                        if hasattr(self, "track_list"):
                            children = self.track_list.get_children()
                            if 0 <= matched_idx < len(children):
                                item_id = children[matched_idx]
                                self.track_list.selection_set(item_id)
                                self.track_list.see(item_id)
                                self.track_list.focus(item_id)
                        self.now_playing_var.set(self._tr("now_playing", track.title) if hasattr(self, "_tr") else track.title)
                        if hasattr(self, "seek_scale"):
                            self.seek_scale.configure(to=max(self.current_duration, 1.0), state="normal")
                        self.seek_var.set(target_pos)
                        self.time_display_var.set(format_duration(target_pos))
                        self.time_var.set(f"{format_duration(target_pos)} / {format_duration(self.current_duration)}")
                        if target_status in ("playing", "paused"):
                            self.is_paused = True
                            self.is_playing = False
                            if hasattr(self, "pause_button"):
                                self.pause_button.configure(text=self._tr("btn_resume"))
            else:
                if hasattr(self, "track_list"):
                    self.track_list.selection_remove(self.track_list.selection())
                self.current_index = -1
                self.current_duration = 0.0
                if hasattr(self, "seek_scale"):
                    self.seek_scale.configure(to=100.0, state="disabled")
                self.seek_var.set(0.0)
                self.time_display_var.set("0:00")
                self.time_var.set("0:00 / 0:00")
                self.now_playing_var.set(self._tr("nothing_playing"))
                self.is_playing = False
                self.is_paused = False
                if hasattr(self, "pause_button"):
                    self.pause_button.configure(text=self._tr("btn_play"))
            if data.get("mic_enabled"):
                self.mic_enabled.set(True)
                self._mic_setting_changed()
            if pruned:
                self._save_state()
        except Exception:
            pass

    def _toggle_theme(self) -> None:
        self.theme = "light" if self.theme == "dark" else "dark"
        if hasattr(self, "tb_style_var"):
            if hasattr(self, "_is_english") and self._is_english():
                self.tb_style_var.set("Light" if self.theme == "light" else "Dark")
            else:
                self.tb_style_var.set("Світлий" if self.theme == "light" else "Темний")
        self._apply_theme()
        self._apply_dark_title_bar(self)
        if hasattr(self, "_settings_window") and self._settings_window is not None and self._settings_window.winfo_exists():
            self._apply_dark_title_bar(self._settings_window)
        self._save_state()

    def _show_error(self, message: str) -> None:
        self.notify_error(message)

    def _on_finished(self) -> None:
        def reset_ui() -> None:
            if self.loop_enabled.get() and 0 <= self.current_index < len(self.local_tracks):
                self._play_local_track(self.local_tracks[self.current_index])
                return
            self.is_playing = False
            self.is_paused = False
            self.pause_button.configure(text=self._tr("btn_play") if hasattr(self, "_tr") else "▶ Відтворити")
            self.seek_var.set(0.0)
            self.time_display_var.set("0:00")
            self.time_var.set(f"0:00 / {format_duration(self.current_duration)}")
            self.now_playing_var.set(self._tr("nothing_playing") if hasattr(self, "_tr") else "Нічого не відтворюється")
            self.status_var.set("")
            self._filter_tracks()

        self.after(0, reset_ui)

    def _on_track_title(self, title: str) -> None:
        def update_title() -> None:
            active = getattr(getattr(self, "engine", None), "_active_track", None)
            try:
                act_dur = float(getattr(active, "duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                act_dur = 0.0
            if active is not None and act_dur > 0.0:
                self.current_duration = act_dur
                self.seek_scale.configure(to=max(1.0, self.current_duration))
                self.time_var.set(f"0:00 / {format_duration(self.current_duration)}")
            self.now_playing_var.set(self._tr("now_playing", title) if hasattr(self, "_tr") else f"Зараз грає: {title}")
            self.notify_info(self._tr("playing", title) if hasattr(self, "_tr") else f"Відтворюється: {title}")
            self._filter_tracks()

        self.after(0, update_title)

    def _close(self) -> None:
        if hasattr(self, "notification_manager"):
            self.notification_manager.dismiss(immediate=True)
        self._save_state()
        if not self.hide_to_tray.get():
            self._exit()
            return
        self.withdraw()
        if self._tray_icon is not None and hasattr(self._tray_icon, "notify"):
            try:
                self._tray_icon.notify("MusicPad працює у системному треї", "MusicPad")
            except Exception:
                pass
        self.notify_info("MusicPad працює у системному треї")

    def _exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        self._save_state()
        if hasattr(self, "notification_manager"):
            self.notification_manager.destroy()
        if hasattr(self, "engine"):
            try:
                self.engine.stop()
                if hasattr(self.engine, "shutdown") and not hasattr(self.engine, "_mock_name"):
                    self.engine.shutdown()
            except Exception:
                pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        if hasattr(self, "_settings_window") and self._settings_window is not None and self._settings_window.winfo_exists():
            try:
                self._settings_window.destroy()
            except Exception:
                pass
        self._teardown_windows_hooks()
        self.destroy()


def _run_self_test(report_path: str) -> int:
    import math
    import os
    import tempfile
    from array import array
    from musicpad import audio_engine as ae_mod

    result: dict[str, Any] = {"ok": False}
    try:
        app = MusicPadApp()
        app.update()
        win_title = app.title()
        win_exists = bool(app.winfo_exists())
        cfg_path = str(app._config_path())
        cache_dir = str(AudioEngine._cache_dir())
        tracks_after_prune = [t.title for t in app.local_tracks]
        app._exit()

        cable_idx = AudioEngine.find_cable_output()
        mic_idx = AudioEngine.find_physical_mic()
        mon_idx = AudioEngine.find_default_monitor(exclude_device=cable_idx)
        devs = AudioEngine.list_devices()

        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "tone.wav"
            rate = 44100
            samples = array("h")
            for i in range(int(rate * 0.22)):
                v = int(8000 * math.sin(2.0 * math.pi * 440.0 * i / rate))
                samples.append(v)
                samples.append(v)
            if ae_mod.wave is not None:
                with ae_mod.wave.open(str(wav_path), "wb") as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(rate)
                    wf.writeframes(samples.tobytes())

            done = threading.Event()
            errors: list[str] = []
            engine = AudioEngine(
                output_device=cable_idx,
                input_device=mic_idx,
                monitor_device=mon_idx,
                on_error=errors.append,
                on_finished=done.set,
                monitor_mic=True,
            )
            engine.set_volume(0.02)
            engine.set_mic_volume(0.5)
            t0 = time.monotonic()
            engine.play(StreamTrack("Tone", str(wav_path)))
            finished = done.wait(timeout=5.0)
            elapsed = time.monotonic() - t0
            engine.shutdown()

            # Also verify dual-output when monitor device uses 48000 Hz alongside 44100 Hz output + mic
            done_48k = threading.Event()
            orig_rate_fn = AudioEngine._device_samplerate
            AudioEngine._device_samplerate = staticmethod(lambda _idx: 48000)  # type: ignore[assignment]
            try:
                engine_48k = AudioEngine(
                    output_device=cable_idx,
                    input_device=mic_idx,
                    monitor_device=mon_idx,
                    on_error=errors.append,
                    on_finished=done_48k.set,
                    monitor_mic=True,
                )
                engine_48k.set_volume(0.02)
                engine_48k.play(StreamTrack("Tone48k", str(wav_path)))
                finished_48k = done_48k.wait(timeout=5.0)
                engine_48k.shutdown()
            finally:
                AudioEngine._device_samplerate = orig_rate_fn  # type: ignore[assignment]

        result = {
            "ok": bool(
                win_title == "MusicPad"
                and win_exists
                and finished
                and finished_48k
                and not errors
                and elapsed >= 0.12
            ),
            "frozen": bool(getattr(sys, "frozen", False)),
            "window_title": win_title,
            "window_exists": win_exists,
            "sd_module": getattr(ae_mod.sd, "__name__", type(ae_mod.sd).__name__),
            "has_numpy": ae_mod.np is not None,
            "has_wave": ae_mod.wave is not None,
            "has_yt_dlp": ae_mod.YoutubeDL is not None,
            "has_ytmusic": ae_mod.YTMusic is not None,
            "config_path": cfg_path,
            "cache_dir": cache_dir,
            "tracks_after_prune": tracks_after_prune,
            "cable_device": devs[cable_idx]["name"] if cable_idx is not None else None,
            "cable_samplerate": devs[cable_idx].get("default_samplerate") if cable_idx is not None else None,
            "monitor_device": devs[mon_idx]["name"] if mon_idx is not None else None,
            "monitor_samplerate": devs[mon_idx].get("default_samplerate") if mon_idx is not None else None,
            "mic_device": devs[mic_idx]["name"] if mic_idx is not None else None,
            "finished": finished,
            "finished_48k": finished_48k,
            "elapsed": round(elapsed, 3),
            "errors": errors,
        }
    except Exception as exc:
        import traceback

        result = {"ok": False, "exception": repr(exc), "traceback": traceback.format_exc()}

    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        import os

        os._exit(_run_self_test(sys.argv[2]))
    MusicPadApp().mainloop()
