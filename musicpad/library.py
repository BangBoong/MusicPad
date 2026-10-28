"""Local music library discovery and metadata."""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


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


def scan_file(file_path: str | Path) -> Optional[LocalTrack]:
    path = Path(file_path)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    title = path.stem
    duration = 0.0
    if MutagenFile is not None:
        try:
            metadata = MutagenFile(path)
            if metadata is not None:
                duration = float(getattr(metadata.info, "length", 0.0) or 0.0)
                raw_title = metadata.get("title") if hasattr(metadata, "get") else None
                if raw_title:
                    title = str(raw_title[0] if isinstance(raw_title, list) else raw_title)
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
    return LocalTrack(title=title, path=str(path), duration=duration)


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


