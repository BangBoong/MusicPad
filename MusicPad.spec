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

native_binaries = []
for dll_name in ("musicpad_apo.dll", "musicpad_dsp.dll"):
    dll_path = Path("native") / "bin" / dll_name
    if dll_path.is_file():
        native_binaries.append((str(dll_path.resolve()), '.'))
        native_binaries.append((str(dll_path.resolve()), 'native/bin'))

deepfilter_datas = []
try:
    import deepfilternet_slim as dfs
    df_lib = Path(dfs.library_path())
    df_model = Path(dfs.model_path())
    if df_lib.is_file():
        native_binaries.append((str(df_lib.resolve()), '.'))
        native_binaries.append((str(df_lib.resolve()), '_native'))
    if df_model.is_file():
        deepfilter_datas.append((str(df_model.resolve()), '.'))
        deepfilter_datas.append((str(df_model.resolve()), '_models'))
except Exception:
    pass

df_bin = Path("native") / "bin" / "deepfilter.dll"
df_model_bin = Path("native") / "bin" / "DeepFilterNet3_onnx.tar.gz"
if df_bin.is_file():
    native_binaries.append((str(df_bin.resolve()), '.'))
    native_binaries.append((str(df_bin.resolve()), 'native/bin'))
if df_model_bin.is_file():
    deepfilter_datas.append((str(df_model_bin.resolve()), '.'))
    deepfilter_datas.append((str(df_model_bin.resolve()), 'native/bin'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=ffmpeg_binaries + native_binaries,
    datas=[('musicpad.png', '.'), ('assets', 'assets')] + ytmusicapi_datas + deepfilter_datas,
    hiddenimports=['wave', 'queue', 'pystray._win32', 'deepfilternet_slim', 'musicpad.deepfilter_engine'],
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
