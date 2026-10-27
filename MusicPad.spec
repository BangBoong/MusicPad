# -*- mode: python ; coding: utf-8 -*-
import shutil
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

ytmusicapi_datas = collect_data_files('ytmusicapi')

ffmpeg_binaries = []
# Look for ffmpeg in PATH or local directory to bundle it inside the executable
ffmpeg_path = shutil.which("ffmpeg")
if not ffmpeg_path and Path("ffmpeg.exe").is_file():
    ffmpeg_path = str(Path("ffmpeg.exe").resolve())

if ffmpeg_path:
    ffmpeg_binaries.append((ffmpeg_path, '.'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=ffmpeg_binaries,
    datas=[('musicpad.png', '.')] + ytmusicapi_datas,
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
