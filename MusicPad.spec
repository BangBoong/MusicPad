# -*- mode: python ; coding: utf-8 -*-
import os
import shutil
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VSVersionInfo,
    VarFileInfo,
    VarStruct,
)

if SPECPATH not in sys.path:
    sys.path.insert(0, SPECPATH)

from musicpad.version import parse_numeric_version_tuple, resolve_build_version

app_version = resolve_build_version(Path(SPECPATH))
ver_tuple = parse_numeric_version_tuple(app_version)
version_file = Path(SPECPATH) / "build" / "VERSION"
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(app_version, encoding="utf-8")

pe_version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=ver_tuple,
        prodvers=ver_tuple,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "MusicPad"),
                        StringStruct("FileDescription", "MusicPad"),
                        StringStruct("FileVersion", app_version),
                        StringStruct("InternalName", "MusicPad"),
                        StringStruct("OriginalFilename", "MusicPad.exe"),
                        StringStruct("ProductName", "MusicPad"),
                        StringStruct("ProductVersion", app_version),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

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
    if not dll_path.is_file():
        raise FileNotFoundError(f"Missing mandatory native library: {dll_path}. Run native/build.ps1 first.")
    native_binaries.append((str(dll_path.resolve()), '.'))
    native_binaries.append((str(dll_path.resolve()), 'native/bin'))

for vc_name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
    sys_vc = Path(r"C:\Windows\System32") / vc_name
    if sys_vc.is_file():
        native_binaries.append((str(sys_vc.resolve()), '.'))

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
    datas=[('assets', 'assets'), ('assets/musicpad.png', '.'), (str(version_file.resolve()), '.')] + ytmusicapi_datas + deepfilter_datas,
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
    [],
    exclude_binaries=True,
    name='MusicPad',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/musicpad.ico'] if os.path.exists('assets/musicpad.ico') else ['musicpad.ico'],
    version=pe_version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MusicPad',
)
