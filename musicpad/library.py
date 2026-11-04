"""Local music library discovery and metadata."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Any

try:
    import wave
except ModuleNotFoundError:
    wave = None  # type: ignore[assignment]

try:
    from mutagen import File as MutagenFile
except ModuleNotFoundError:
    MutagenFile = None  # type: ignore[assignment]


SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma"}


@dataclass
class LocalTrack:
    title: str
    path: str
    duration: float
    hotkey: str = ""
    is_url: bool = False
    artist: str = ""
    thumbnail_path: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "path": self.path,
            "duration": self.duration,
            "hotkey": self.hotkey,
            "is_url": self.is_url,
            "artist": self.artist,
            "thumbnail_path": self.thumbnail_path,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LocalTrack:
        return cls(
            title=str(data.get("title", "")),
            path=str(data.get("path", "")),
            duration=float(data.get("duration", 0.0)),
            hotkey=str(data.get("hotkey", "")),
            is_url=bool(data.get("is_url", False)),
            artist=str(data.get("artist", "")),
            thumbnail_path=str(data.get("thumbnail_path", "")),
            source=str(data.get("source", "")),
        )


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def format_album_duration(
    track_count: Optional[int],
    duration_sec: Optional[float] = None,
    duration_str: str = "",
    lang: str = "uk",
) -> str:
    if duration_str and re.search(r"\bтр\b\.?|\btr\b\.?|tracks?", duration_str, re.IGNORECASE):
        if lang == "en":
            s = re.sub(r"\bтр\b\.?|tracks?", "tr.", duration_str, flags=re.IGNORECASE)
            s = re.sub(r"\bхв\b\.?|minutes?|mins?", "min", s, flags=re.IGNORECASE)
            s = re.sub(r"\bгод\b\.?|hours?|hrs?", "hr", s, flags=re.IGNORECASE)
        else:
            s = re.sub(r"\btr\b\.?|tracks?", "тр.", duration_str, flags=re.IGNORECASE)
            s = re.sub(r"\bmin\b\.?|minutes?|mins?", "хв", s, flags=re.IGNORECASE)
            s = re.sub(r"\bhr\b\.?|hours?|hrs?", "год", s, flags=re.IGNORECASE)
        return s

    parts: list[str] = []
    unit_tr = "tr." if lang == "en" else "тр."
    unit_hr = "hr" if lang == "en" else "год"
    unit_min = "min" if lang == "en" else "хв"
    if track_count is not None:
        try:
            tc_int = int(track_count)
            if tc_int > 0:
                parts.append(f"{tc_int} {unit_tr}")
        except (ValueError, TypeError):
            pass

    dur_text = ""
    if duration_sec is not None and float(duration_sec or 0.0) > 0:
        sec = int(duration_sec)
        hours = sec // 3600
        mins = (sec % 3600) // 60
        if hours > 0:
            dur_text = f"{hours} {unit_hr} {mins} {unit_min}" if mins > 0 else f"{hours} {unit_hr}"
        else:
            dur_text = f"{max(1, mins)} {unit_min}"
    elif duration_str:
        ds = str(duration_str).lower().strip()
        h_m = re.search(r"(\d+)\s*(?:hour|hr|h|год)", ds)
        m_m = re.search(r"(\d+)\s*(?:minute|min|m|хв)", ds)
        if h_m or m_m:
            h = int(h_m.group(1)) if h_m else 0
            m = int(m_m.group(1)) if m_m else 0
            if h > 0:
                dur_text = f"{h} {unit_hr} {m} {unit_min}" if m > 0 else f"{h} {unit_hr}"
            elif m > 0:
                dur_text = f"{m} {unit_min}"
        elif ":" in ds:
            col_parts = [int(p) for p in ds.split(":") if p.isdigit()]
            if len(col_parts) == 3:
                h, m = col_parts[0], col_parts[1]
                dur_text = f"{h} {unit_hr} {m} {unit_min}" if m > 0 else f"{h} {unit_hr}"
            elif len(col_parts) == 2:
                m = col_parts[0]
                dur_text = f"{max(1, m)} {unit_min}"
            else:
                dur_text = duration_str
        else:
            dur_text = duration_str

    if dur_text:
        parts.append(dur_text)

    return " • ".join(parts) if parts else ""


def _probe_wav_duration(path: Path) -> float:
    if wave is not None:
        try:
            with wave.open(str(path), "rb") as wav_file:
                rate = wav_file.getframerate()
                frames = wav_file.getnframes()
                if rate > 0:
                    return float(frames) / float(rate)
        except Exception:
            pass
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return 0.0
            byte_rate = 0
            data_size = 0
            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break
                chunk_id, chunk_size = struct.unpack("<4sI", chunk_hdr)
                if chunk_id == b"fmt " and chunk_size >= 16:
                    fmt_data = f.read(chunk_size)
                    _, _, _, byte_rate, _, _ = struct.unpack("<HHIIHH", fmt_data[:16])
                elif chunk_id == b"data":
                    data_size = chunk_size
                    break
                else:
                    f.seek(chunk_size, 1)
                if chunk_size % 2 == 1:
                    f.seek(1, 1)
            if byte_rate > 0 and data_size > 0:
                return float(data_size) / float(byte_rate)
    except Exception:
        pass
    return 0.0


def _probe_ffprobe_metadata(path: Path) -> tuple[Optional[str], float]:
    ffprobe: Optional[str] = None
    ffmpeg_bin: Optional[str] = None
    try:
        from musicpad.audio_engine import AudioEngine

        ffprobe = AudioEngine.find_ffprobe()
    except Exception:
        pass
    if not ffprobe:
        ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        try:
            from musicpad.audio_engine import AudioEngine

            ffmpeg_bin = AudioEngine.find_ffmpeg()
            if ffmpeg_bin:
                sibling = Path(ffmpeg_bin).with_name("ffprobe.exe")
                if sibling.is_file():
                    ffprobe = str(sibling)
        except Exception:
            pass
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:format_tags=title",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout:
                data = json.loads(proc.stdout).get("format", {})
                duration = float(data.get("duration", 0.0) or 0.0)
                tags = data.get("tags") or {}
                title = tags.get("title") or tags.get("TITLE")
                return (str(title).strip() if title else None), max(0.0, duration)
        except Exception:
            pass
    if not ffmpeg_bin:
        try:
            from musicpad.audio_engine import AudioEngine

            ffmpeg_bin = AudioEngine.find_ffmpeg()
        except Exception:
            pass
    if ffmpeg_bin:
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-i", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            err = proc.stderr or ""
            m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
            dur = 0.0
            if m_dur:
                dur = int(m_dur.group(1)) * 3600 + int(m_dur.group(2)) * 60 + float(m_dur.group(3))
            m_title = re.search(r"^\s*title\s*:\s*(.+)$", err, re.IGNORECASE | re.MULTILINE)
            title = m_title.group(1).strip() if m_title else None
            return (title or None), max(0.0, dur)
        except Exception:
            pass
    return None, 0.0


def extract_embedded_cover(file_path: str | Path, cache_dir: str = ".cache/thumbnails") -> Optional[str]:
    p = Path(file_path)
    if not p.is_file():
        return None
    stem_hash = hashlib.md5(f"{p.resolve()}_{p.stat().st_mtime}".encode("utf-8")).hexdigest()
    out_dir = Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"local_{stem_hash}.jpg"
    if out_path.is_file() and out_path.stat().st_size > 0:
        return str(out_path)

    if MutagenFile is None:
        return None

    try:
        data = None
        f = MutagenFile(p)
        if f is None:
            return None
        # ID3 (MP3, etc.)
        if hasattr(f, "tags") and f.tags:
            for tag in f.tags.values():
                if tag.__class__.__name__ == "APIC" or getattr(tag, "mime", "").startswith("image/"):
                    data = getattr(tag, "data", None)
                    if data:
                        break
        # FLAC
        if not data and hasattr(f, "pictures") and f.pictures:
            data = f.pictures[0].data
        # MP4 / M4A
        if not data and hasattr(f, "get"):
            covr = f.get("covr")
            if covr and len(covr) > 0:
                data = bytes(covr[0])

        if data:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(data)).convert("RGB")
                w, h = img.size
                if w != h:
                    min_dim = min(w, h)
                    left = (w - min_dim) // 2
                    top = (h - min_dim) // 2
                    img = img.crop((left, top, left + min_dim, top + min_dim))
                img = img.resize((40, 40), Image.Resampling.LANCZOS)
                img.save(str(out_path), "JPEG", quality=90)
                return str(out_path)
            except Exception:
                pass
    except Exception:
        pass
    return None


def scan_file(file_path: str | Path) -> Optional[LocalTrack]:
    path = Path(file_path)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    title = path.stem
    artist = ""
    duration = 0.0
    if MutagenFile is not None:
        try:
            metadata = MutagenFile(path)
            if metadata is not None:
                duration = float(getattr(metadata.info, "length", 0.0) or 0.0)
                raw_title = metadata.get("title") if hasattr(metadata, "get") else None
                if raw_title:
                    title = str(raw_title[0] if isinstance(raw_title, list) else raw_title)
                raw_artist = metadata.get("artist") if hasattr(metadata, "get") else None
                if not raw_artist and hasattr(metadata, "get"):
                    raw_artist = metadata.get("ARTIST")
                if raw_artist:
                    artist = str(raw_artist[0] if isinstance(raw_artist, list) else raw_artist)
        except (OSError, TypeError, ValueError):
            pass
    if duration <= 0.0 and path.suffix.lower() == ".wav":
        duration = _probe_wav_duration(path)
    if duration <= 0.0 and MutagenFile is None:
        probed_title, probed_duration = _probe_ffprobe_metadata(path)
        if probed_title:
            title = probed_title
        if probed_duration > 0.0:
            duration = probed_duration
    if not artist:
        if " — " in title:
            parts = title.split(" — ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        elif " - " in title:
            parts = title.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
    return LocalTrack(title=title, path=str(path), duration=duration, artist=artist, source="file")


def scan_folder(folder: str) -> list[LocalTrack]:
    tracks: list[LocalTrack] = []
    folder_path = Path(folder)
    if not folder_path.exists():
        return tracks
    for path in sorted(folder_path.rglob("*"), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        track = scan_file(path)
        if track is not None:
            tracks.append(track)
    return tracks


