# -*- mode: python ; coding: utf-8 -*-
import shutil
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ytmusicapi_datas = collect_data_files('ytmusicapi')

ffmpeg_binaries = []
# Look for ffmpeg and ffprobe in local directory or PATH to bundle inside the executable
ffmpeg_path = None
if Path("ffmpeg.exe").is_file():
    ffmpeg_path = str(Path("ffmpeg.exe").resolve())
elif shutil.which("ffmpeg"):
    ffmpeg_path = shutil.which("ffmpeg")

if ffmpeg_path:
    ffmpeg_binaries.append((ffmpeg_path, '.'))

ffprobe_path = None
if Path("ffprobe.exe").is_file():
    ffprobe_path = str(Path("ffprobe.exe").resolve())
elif shutil.which("ffprobe"):
    ffprobe_path = shutil.which("ffprobe")

if ffprobe_path:
    ffmpeg_binaries.append((ffprobe_path, '.'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=ffmpeg_binaries,
    datas=[('musicpad.png', '.'), ('assets', 'assets')] + ytmusicapi_datas,
    hiddenimports=['wave', 'queue', 'pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MusicPad',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['musicpad.ico'],
)
