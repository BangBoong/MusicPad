"""Release version resolution shared between build scripts and runtime."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_VERSION = "1.6.4"

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)+(?:[-+._a-zA-Z0-9]*)?)$")


def normalize_release_version(raw: Optional[str], fallback: str = DEFAULT_VERSION) -> str:
    """Normalize a Git tag or version string (strip leading 'v'/'V', return fallback if invalid)."""
    if not raw:
        return fallback
    text = str(raw).strip()
    match = _VERSION_RE.match(text)
    if not match:
        return fallback
    return match.group(1)


def parse_numeric_version_tuple(version: str) -> tuple[int, int, int, int]:
    """Parse up to 4 numeric components for Windows PE FileVersion/ProductVersion."""
    core = re.split(r"[-+]", normalize_release_version(version), maxsplit=1)[0]
    nums: list[int] = []
    for part in core.split("."):
        digits = re.match(r"\d+", part)
        if digits:
            nums.append(min(65535, int(digits.group(0))))
        if len(nums) == 4:
            break
    while len(nums) < 4:
        nums.append(0)
    return (nums[0], nums[1], nums[2], nums[3])


def _read_git_tag_version(repo_dir: Path) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(repo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            candidate = res.stdout.strip()
            if _VERSION_RE.match(candidate):
                return normalize_release_version(candidate)
    except Exception:
        pass
    return None


def resolve_build_version(
    repo_dir: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    fallback: str = DEFAULT_VERSION,
) -> str:
    """Resolve the release version from CI env vars, git tag, or *fallback*."""
    env_map = os.environ if env is None else env
    for key in ("MUSICPAD_VERSION", "GITHUB_REF_NAME"):
        raw = env_map.get(key, "")
        if _VERSION_RE.match(raw.strip()):
            return normalize_release_version(raw, fallback=fallback)

    root = Path(__file__).resolve().parent.parent if repo_dir is None else Path(repo_dir)
    git_ver = _read_git_tag_version(root)
    if git_ver:
        return git_ver
    return fallback


def _bundled_version_candidates() -> list[Path]:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "VERSION")
    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([exe_dir / "VERSION", exe_dir / "_internal" / "VERSION"])
    except OSError:
        pass
    return candidates


def get_app_version() -> str:
    """Return the application version matching GitHub Releases."""
    for cand in _bundled_version_candidates():
        if cand.is_file():
            try:
                content = cand.read_text(encoding="utf-8").strip()
                if _VERSION_RE.match(content):
                    return normalize_release_version(content)
            except OSError:
                pass

    if not getattr(sys, "frozen", False):
        repo_root = Path(__file__).resolve().parent.parent
        git_ver = _read_git_tag_version(repo_root)
        if git_ver:
            return git_ver

    return DEFAULT_VERSION
