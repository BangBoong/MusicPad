# 13. Embedded YouTube Panel Streamlining, Full UK/EN Localization, Filter Limit Slider, and Album Autoplay Engine Drain

Date: 2026-11-04

## Status

Accepted

## Context

Following the migration to an embedded layout for the YouTube search panel inside the main window, the UI retained artifacts from its original standalone dialog design:
1. `self.yt_titlebar` (dialog header with title icon, label, and red close button "✕").
2. `self.yt_bottom` (bottom action bar with "▶ Відтворити вибране", "➕ Закешувати в саундпад", and "Закрити").
In the embedded layout, closing the panel is cleanly performed via category navigation on the left panel ("Мої звуки"), while playback and caching are natively available via double click, Enter, and right-click context menu. Removing these bars allows the search results table to extend seamlessly down to the bottom of the right pane.

Additionally, user testing revealed three further requirements:
1. **Full Localization (UK / EN)**: The interface contained hardcoded strings (e.g. statistics captions "Звуки:", "Вибрано:", "Лічильник відтворень:", cache button "🗑 Очистити кеш", YouTube search controls, table column headers, and album navigation) that did not dynamically update when changing language in Settings.
2. **Filter Limit Slider (5–35, step 1)**: YouTube search previously hardcoded `limit=35`. Users required an interactive slider within the "Filter" option to configure the number of retrieved results between 5 and 35 with step 1, with persistent state storage.
3. **Album Autoplay Regression**: When the broadcast mute feature (`mute_track_to_others = True` / `_route_mic = False`) was engaged, audio in albums stopped advancing to the next song upon track completion. Investigation in `AudioEngine` revealed that when mic routing was muted, `READER_INJECTION` in the ring buffer was never read, leaving lingering frames that caused `has_music` to remain permanently `True`, starving `on_finished` from firing.

## Decision

1. **YouTube Search Panel Streamlining**:
   - Removed `self.yt_titlebar` and `self.yt_bottom`.
   - Set `self.yt_table_box.pack(fill="both", expand=True, padx=12, pady=(0, 10))` to utilize the full height of the pane.
   - Added `_show_yt_context_menu` on right-click (`<Button-3>`) on `yt_tree` with "▶ Відтворити" / "▶ Play", "➕ Додати в саундпад (миттєвий старт)" / "➕ Add to soundpad", and "📋 Копіювати посилання" / "📋 Copy link".
   - Preserved backward-compatible safe null references for removed widget attributes.

2. **Album Autoplay Engine Drain**:
   - In `musicpad/audio_engine.py`, when `not track_active_for_apo` (mic broadcast muted or inactive), unconsumed frames in `READER_INJECTION` are drained and discarded in sync with monitor playback.
   - When monitor playback completes or when broadcast is muted, leftover injection frames are discarded immediately.
   - Guarded `has_music` calculation so `avail_inj` and `avail_shm` only count if `self._route_mic and apo_status is not None`.
   - When headphone monitor playback completes, `has_music` transitions to `False`, immediately triggering `on_finished()` and allowing album queues to advance seamlessly to the next track.

3. **Filter Limit Slider & In-Frame Filter Panel**:
   - Added `self.yt_search_limit_var = tk.IntVar(value=35)` (persisted in `musicpad_state.json` under `"yt_search_limit"`).
   - Replaced fragile floating `tk.Toplevel(overrideredirect=True)` with an integrated in-frame collapsible panel `self.yt_filter_panel` embedded directly beneath the search bar and above the results table.
   - Panel features dynamic toggle button indicators (`▴` / `▾`), dark/light theme background and borders, radio options ("Всі результати", "Без альбомів", "Тільки альбоми"), and an interactive `tk.Scale` (`from_=5, to=35, resolution=1`) with live localized count formatting (`Кількість треків: {count}` / `Track count: {count}`).
   - Embedded placement prevents OS-level window hierarchy, Z-order, and DWM occlusion glitches on Windows.
   - Connected `f_limit = int(self.yt_search_limit_var.get())` into `AudioEngine.search_youtube(q, limit=f_limit, filter_mode=f_mode)`.

4. **Complete Localization Architecture & Now-Playing Sync**:
   - Expanded `I18N["uk"]` and `I18N["en"]` with complete keys for category titles, bottom statistics captions, clear cache dialog & buttons, search button, filter modes & slider, table headers (`yt_col_title`, `yt_col_artist`, `yt_col_duration`, `yt_col_source`, `yt_col_cover`), badges (`badge_album`, `badge_song`, `badge_video`), search status (`yt_status_found`, `yt_status_searching`, `yt_status_error`), and album navigation.
   - Enhanced `_tr(key, *args, **kwargs)` to support both positional and named format arguments.
   - Synchronized `_update_now_playing_display()` inside `_apply_language()` so the top playback toolbar immediately renders `"Nothing is playing"` / `"Нічого не відтворюється"` across language switches.
   - Enhanced `format_album_duration()` to support bidirectional localization (`lang="uk"` / `lang="en"`) preserving composite track counts and duration units across re-renders.
   - Enhanced `_apply_language()` to dynamically update all primary UI labels, headings, menus, search result rows, and album navigation headers without requiring an application restart.

## Consequences

- The YouTube search panel is spacious and visually uncluttered, aligning with native Soundpad aesthetics.
- Seamless bilingual switching (English and Ukrainian) updates 100% of the visible UI elements in real-time.
- Users can customize search result batch sizes between 5 and 35.
- Album autoplay functions reliably regardless of whether broadcast to others is muted or enabled.
