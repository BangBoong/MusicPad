# 14. Hotkeys Tab Simplification, Obsolete Settings Pruning, and Interactive Key Capture

Date: 2026-11-05

## Status

Accepted

## Context

The "Hotkeys" tab in the Settings dialog previously contained four separate sections:
1. `Спеціальні гарячі клавіші` (Special hotkeys table with 19 actions, using text-based `simpledialog.askstring` input).
2. `Гарячі клавіші цифрового блоку` (Numpad hotkey options).
3. `Автоклавіші` (Auto-keys / push-to-talk automation table with add/edit/delete buttons).
4. `Розширені параметри` (Advanced hotkey parameters, including passthrough, L/R modifiers, and redundant category options).

Furthermore, the special hotkeys table included 11 actions that were either non-functional placeholders (`""` shortcuts without engine dispatchers) or redundant list editing options. Assigning a hotkey required manually typing strings (such as "F5" or "Ctrl+K") into a text dialog rather than pressing the physical key on the keyboard. There was also no duplicate conflict prevention between actions or with assigned track hotkeys.

## Decision

1. **Tab Structure Pruning & Expansion**:
   - Removed the obsolete sections: "Гарячі клавіші цифрового блоку", "Автоклавіші", and "Розширені параметри".
   - Expanded "Спеціальні гарячі клавіші" to fill the entire tab height (`fill="both", expand=True`) with an expanded 12-row table.
   - Retained only 8 core actions in `special_hotkeys_dict`:
     1. "Зупинити відтворення" (Stop playback)
     2. "Призупинити/продовжити відтворення" (Pause / resume playback)
     3. "Скасувати" (Undo)
     4. "Копіювати" (Copy)
     5. "Вставити" (Paste)
     6. "Видалити вибрані записи" (Delete selected)
     7. "Пошук" (Search)
     8. "Вихід" (Exit)
   - Preserved standard global/local editor bindings (`Ctrl+S`, `Ctrl+A`, `Ctrl+X`, `Ctrl+Y`, etc.) in the main application window.

2. **Interactive Direct Key Capture**:
   - Replaced `simpledialog.askstring` with a modal capture dialog (`_set_special_hotkey`).
   - Pressing any key or combination (with `Ctrl`, `Alt`, `Shift`) formats the key via `_format_tk_hotkey(event)` and assigns it immediately.
   - `Esc` cancels without changes; `Backspace` clears the assigned key.
   - Bound `<Double-1>` double click on table rows to invoke key assignment directly.
   - Added a dedicated "Очистити" button to clear hotkeys.

3. **Conflict Detection & Resolution**:
   - When a pressed key is already assigned to another special action or a track, the application prompts:
     `"Гаряча клавіша [{hk}] вже використовується для «{conflict_name}». Перепризначити її?"`
   - If confirmed, the key is removed from the conflicting action/track and assigned to the selected action.

4. **Dynamic Global & Local Runtime Dispatching**:
   - Playback control actions ("Зупинити відтворення", "Призупинити/продовжити відтворення") are polled globally in `_poll_global_hotkeys` so they function across Windows and inside fullscreen games.
   - Editing operations are dynamically rebound to local Tkinter window sequences (`_hotkey_to_tk_sequences`).

5. **State & Config Cleanup**:
   - Removed `numpad_alt`, `numpad_ctrl_alt`, `auto_keys`, `pass_hotkeys`, `allow_lr_modifiers`, and `same_hotkeys_all_categories` from runtime variables and `musicpad_state.json`.
   - Sanitized state loading to ensure obsolete keys from older configuration files are ignored.

## Consequences

- The Hotkeys settings tab is clean, spacious, and responsive.
- Hotkey configuration is intuitive and instantaneous: clicking "Встановити гарячу клавішу" or double-clicking a row followed by pressing the desired key assigns it directly.
- Key conflicts are detected and resolved with user confirmation.
- Memory and state file serialization are free of dead code and obsolete settings.
