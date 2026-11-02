"""On-demand UAC driver installer, uninstaller, repair helper, and health inspector for MusicPad.

Implements ADR-0001 and ADR-0002:
- Registers MusicPadAPO ({9D4E7A12-6C8B-4F2A-B5E1-3A9C7D2E4F10}) in EFX slot of the target microphone
- Wraps any existing OriginalApo on that endpoint
- Backs up and restores endpoint Properties, FxProperties, and DisableProtectedAudioDG in
  C:\\ProgramData\\MusicPad\\backup.json
- Pre-creates C:\\ProgramData\\MusicPad\\injection.bin with LocalService and Users Modify ACLs
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

if os.name == "nt":
    import winreg

from musicpad.injection_shm import (
    InjectionShm,
    MPAD_APO_BUILD_VERSION,
    MPAD_SHM_DEFAULT_PATH,
    MPAD_SHM_TOTAL_BYTES,
    MPAD_SHM_VERSION,
)

MUSICPAD_APO_CLSID = "{9D4E7A12-6C8B-4F2A-B5E1-3A9C7D2E4F10}"
LEGACY_SPIKE_CLSIDS = {
    "{8E65B49A-5B3E-4B3D-9E5D-2C1B8A4F7E21}",
    "{3B97825E-5D34-4C2E-8A53-7BC22F9E014A}",
}
DEFAULT_PROCESSING_MODE = "{C18E2F7E-933D-4965-B7D1-1EEF228D2AF3}"
NULL_GUID = "{00000000-0000-0000-0000-000000000000}"

DATA_DIR = Path(r"C:\ProgramData\MusicPad")
BACKUP_FILE = DATA_DIR / "backup.json"
SPIKE_BACKUP_FILE = DATA_DIR / "spike_backup.json"
INSTALL_DIR = Path(r"C:\Program Files\MusicPad")
SYSTEM32_APO_DLL = Path(r"C:\Windows\System32\musicpad_apo.dll")
SYSTEM32_SPIKE_DLL = Path(r"C:\Windows\System32\musicpad_apo_spike.dll")
SYSTEM32_DF_DLL = Path(r"C:\Windows\System32\deepfilter.dll")
SYSTEM32_DF_MODEL = Path(r"C:\Windows\System32\DeepFilterNet3_onnx.tar.gz")

MMDEVICES_CAPTURE_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
AUDIO_CONFIG_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Audio"
MUSICPAD_APO_STATE_SUBKEY = r"SOFTWARE\MusicPad\APO"

# Property keys inside MMDevices\Audio\Capture\<GUID>\Properties
PKEY_ENDPOINT_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PKEY_ADAPTER_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
PKEY_DISABLE_SYSFX = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"
PKEY_ALLOW_EXCLUSIVE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},3"
PKEY_EXCLUSIVE_PRIORITY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},4"

# Property keys inside MMDevices\Audio\Capture\<GUID>\FxProperties
PKEY_FX_ASSOC = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},0"
PKEY_FX_LFX_LEGACY = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},1"
PKEY_FX_SFX_SINGLE = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},5"
PKEY_FX_MFX_SINGLE = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},6"
PKEY_FX_EFX_SINGLE = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},7"
PKEY_FX_SFX_MULTI = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},13"
PKEY_FX_MFX_MULTI = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},14"
PKEY_FX_EFX_MULTI = "{d04e05a6-594b-4fb6-a80d-01af5eed7d1d},15"
PKEY_MODE_SFX = "{d3993a3f-99c2-4402-b5ec-a92a0367664b},5"
PKEY_MODE_MFX = "{d3993a3f-99c2-4402-b5ec-a92a0367664b},6"
PKEY_MODE_EFX = "{d3993a3f-99c2-4402-b5ec-a92a0367664b},7"


@dataclass(frozen=True)
class CaptureEndpoint:
    """Represents an active Windows capture (microphone) endpoint."""

    guid: str
    endpoint_name: str
    adapter_name: str
    enhancements_enabled: bool
    exclusive_allowed: bool
    efx_clsid: str

    @property
    def display_name(self) -> str:
        if self.endpoint_name and self.adapter_name:
            return f"{self.endpoint_name} ({self.adapter_name})"
        return self.adapter_name or self.endpoint_name or self.guid


@dataclass(frozen=True)
class DriverStatus:
    """Health and installation state of the MusicPad APO driver."""

    state: str  # "active" | "not_installed" | "inactive" | "outdated" | "wrong_endpoint"
    installed_endpoint_guid: str
    target_endpoint_guid: str
    reason: str
    original_apo_clsid: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.state == "active"

    @property
    def message(self) -> str:
        return self.reason


def normalize_guid(guid: Optional[str]) -> str:
    if not guid:
        return ""
    text = guid.strip()
    if not text:
        return ""
    if not text.startswith("{"):
        text = "{" + text
    if not text.endswith("}"):
        text = text + "}"
    return text.lower()


def is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _find_built_dll(dll_name: str) -> Optional[Path]:
    """Find musicpad_apo.dll or musicpad_dsp.dll in PyInstaller bundle or repo."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates.extend([base / dll_name, base / "native" / "bin" / dll_name])

    exe_dir = Path(sys.executable).resolve().parent
    repo_root = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            exe_dir / dll_name,
            exe_dir / "native" / "bin" / dll_name,
            repo_root / "native" / "bin" / dll_name,
            INSTALL_DIR / dll_name,
        ]
    )
    for c in candidates:
        if c.is_file():
            return c
    return None


def list_capture_endpoints(include_virtual_cables: bool = False) -> list[CaptureEndpoint]:
    """Enumerate active capture endpoints from HKLM\\...\\MMDevices\\Audio\\Capture."""
    if os.name != "nt":
        return []

    results: list[CaptureEndpoint] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MMDEVICES_CAPTURE_SUBKEY, 0, winreg.KEY_READ) as cap_key:
            subkey_count = winreg.QueryInfoKey(cap_key)[0]
            for idx in range(subkey_count):
                guid_name = winreg.EnumKey(cap_key, idx)
                ep_subkey = f"{MMDEVICES_CAPTURE_SUBKEY}\\{guid_name}"
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ep_subkey, 0, winreg.KEY_READ) as ep_key:
                        dev_state, _ = winreg.QueryValueEx(ep_key, "DeviceState")
                        if int(dev_state) != 1:  # 1 == DEVICE_STATE_ACTIVE
                            continue
                except OSError:
                    continue

                endpoint_name = ""
                adapter_name = ""
                disable_sysfx = 0
                allow_exclusive = 1
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, f"{ep_subkey}\\Properties", 0, winreg.KEY_READ
                    ) as prop_key:
                        try:
                            endpoint_name = str(winreg.QueryValueEx(prop_key, PKEY_ENDPOINT_NAME)[0])
                        except OSError:
                            pass
                        try:
                            adapter_name = str(winreg.QueryValueEx(prop_key, PKEY_ADAPTER_NAME)[0])
                        except OSError:
                            pass
                        try:
                            disable_sysfx = int(winreg.QueryValueEx(prop_key, PKEY_DISABLE_SYSFX)[0])
                        except OSError:
                            disable_sysfx = 0
                        try:
                            allow_exclusive = int(winreg.QueryValueEx(prop_key, PKEY_ALLOW_EXCLUSIVE)[0])
                        except OSError:
                            allow_exclusive = 1
                except OSError:
                    pass

                if not include_virtual_cables:
                    combined_lower = f"{endpoint_name} {adapter_name}".lower()
                    if any(k in combined_lower for k in ("vb-audio", "cable output", "virtual cable", "voicemeeter")):
                        continue

                efx_clsid = ""
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, f"{ep_subkey}\\FxProperties", 0, winreg.KEY_READ
                    ) as fx_key:
                        try:
                            val, _ = winreg.QueryValueEx(fx_key, PKEY_FX_EFX_SINGLE)
                            efx_clsid = str(val)
                        except OSError:
                            try:
                                val, _ = winreg.QueryValueEx(fx_key, PKEY_FX_EFX_MULTI)
                                if isinstance(val, list) and val:
                                    efx_clsid = str(val[0])
                            except OSError:
                                pass
                except OSError:
                    pass

                results.append(
                    CaptureEndpoint(
                        guid=normalize_guid(guid_name),
                        endpoint_name=endpoint_name,
                        adapter_name=adapter_name,
                        enhancements_enabled=(disable_sysfx == 0),
                        exclusive_allowed=(allow_exclusive != 0),
                        efx_clsid=efx_clsid,
                    )
                )
    except OSError:
        return []

    return results


def find_endpoint_for_sounddevice(
    sd_device_name: str,
    endpoints: Optional[list[CaptureEndpoint]] = None,
) -> Optional[CaptureEndpoint]:
    """Match a sounddevice input device string (even MME-truncated to 31 chars) to a CaptureEndpoint."""
    if not sd_device_name:
        return None
    eps = endpoints if endpoints is not None else list_capture_endpoints(include_virtual_cables=True)
    if not eps:
        return None

    target_lower = sd_device_name.strip().lower()
    for ep in eps:
        disp_lower = ep.display_name.lower()
        if disp_lower == target_lower or disp_lower.startswith(target_lower) or target_lower.startswith(disp_lower):
            return ep

    for ep in eps:
        adapter_lower = ep.adapter_name.lower()
        if adapter_lower and (adapter_lower in target_lower or target_lower in adapter_lower):
            return ep
        # Handle MME 31-character truncation e.g. 'Microphone (5- fifine Microphon'
        if adapter_lower and len(adapter_lower) >= 8 and adapter_lower[:12] in target_lower:
            return ep

    return None


def get_driver_status(
    target_endpoint_guid: Optional[str] = None,
    *,
    system32_dll_path: Path = SYSTEM32_APO_DLL,
    shm_path: Path = MPAD_SHM_DEFAULT_PATH,
) -> DriverStatus:
    """Inspect whether the MusicPad APO driver is installed and healthy on target_endpoint_guid."""
    norm_target = normalize_guid(target_endpoint_guid)

    if os.name != "nt":
        return DriverStatus(
            state="not_installed",
            installed_endpoint_guid="",
            target_endpoint_guid=norm_target,
            reason="Підтримується лише Windows x64",
        )

    installed_guid = ""
    original_apo = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MUSICPAD_APO_STATE_SUBKEY, 0, winreg.KEY_READ) as st_key:
            try:
                installed_guid = normalize_guid(str(winreg.QueryValueEx(st_key, "InstalledEndpointGuid")[0]))
            except OSError:
                pass
    except OSError:
        pass

    clsid_registered = False
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"SOFTWARE\\Classes\\CLSID\\{MUSICPAD_APO_CLSID}\\InprocServer32",
            0,
            winreg.KEY_READ,
        ):
            clsid_registered = True
    except OSError:
        clsid_registered = False

    if not clsid_registered and not installed_guid:
        return DriverStatus(
            state="not_installed",
            installed_endpoint_guid="",
            target_endpoint_guid=norm_target,
            reason="Драйвер MusicPad не встановлено",
        )

    if not system32_dll_path.is_file() or not clsid_registered:
        return DriverStatus(
            state="inactive",
            installed_endpoint_guid=installed_guid,
            target_endpoint_guid=norm_target or installed_guid,
            reason="Файл драйвера або реєстрація COM відсутні (потрібен ремонт)",
        )

    # Check DisableProtectedAudioDG
    disable_prot = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, AUDIO_CONFIG_SUBKEY, 0, winreg.KEY_READ) as aud_key:
            disable_prot, _ = winreg.QueryValueEx(aud_key, "DisableProtectedAudioDG")
    except OSError:
        disable_prot = 0

    if int(disable_prot) != 1:
        return DriverStatus(
            state="inactive",
            installed_endpoint_guid=installed_guid,
            target_endpoint_guid=norm_target or installed_guid,
            reason="DisableProtectedAudioDG вимкнено (потрібен ремонт)",
        )

    check_guid = norm_target or installed_guid
    if not check_guid:
        return DriverStatus(
            state="inactive",
            installed_endpoint_guid=installed_guid,
            target_endpoint_guid="",
            reason="Не вибрано цільовий мікрофон",
        )

    if installed_guid and norm_target and installed_guid != norm_target:
        return DriverStatus(
            state="wrong_endpoint",
            installed_endpoint_guid=installed_guid,
            target_endpoint_guid=norm_target,
            reason="Драйвер встановлено на інший мікрофон (потрібно перенести)",
        )

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"{MUSICPAD_APO_STATE_SUBKEY}\\{check_guid}",
            0,
            winreg.KEY_READ,
        ) as ep_cfg_key:
            try:
                original_apo = str(winreg.QueryValueEx(ep_cfg_key, "OriginalApo")[0])
            except OSError:
                pass
    except OSError:
        pass

    # Verify endpoint Properties (enhancements enabled) and FxProperties (EFX slot points to MusicPadAPO)
    ep_subkey = f"{MMDEVICES_CAPTURE_SUBKEY}\\{check_guid}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{ep_subkey}\\Properties", 0, winreg.KEY_READ) as prop_key:
            try:
                disable_sysfx = int(winreg.QueryValueEx(prop_key, PKEY_DISABLE_SYSFX)[0])
                if disable_sysfx != 0:
                    return DriverStatus(
                        state="inactive",
                        installed_endpoint_guid=installed_guid,
                        target_endpoint_guid=check_guid,
                        reason="Обробку звуку (Enhancements) вимкнено на цільовому мікрофоні",
                        original_apo_clsid=original_apo,
                    )
            except OSError:
                pass
    except OSError:
        return DriverStatus(
            state="inactive",
            installed_endpoint_guid=installed_guid,
            target_endpoint_guid=check_guid,
            reason="Цільовий мікрофон не знайдено в системі",
            original_apo_clsid=original_apo,
        )

    efx_val = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{ep_subkey}\\FxProperties", 0, winreg.KEY_READ) as fx_key:
            try:
                efx_val = str(winreg.QueryValueEx(fx_key, PKEY_FX_EFX_SINGLE)[0])
            except OSError:
                pass
    except OSError:
        pass

    if normalize_guid(efx_val) != normalize_guid(MUSICPAD_APO_CLSID):
        return DriverStatus(
            state="inactive",
            installed_endpoint_guid=installed_guid,
            target_endpoint_guid=check_guid,
            reason="Слот EFX цільового мікрофона скинуто після оновлення (потрібен ремонт)",
            original_apo_clsid=original_apo,
        )

    # Check protocol/build version in injection.bin if it exists
    if shm_path.is_file():
        shm = InjectionShm(shm_path)
        try:
            shm.open(acquire_lease=False)
            st = shm.get_apo_status()
            if st.protocol_version not in (0, MPAD_SHM_VERSION) or (
                st.apo_build_version not in (0, MPAD_APO_BUILD_VERSION)
            ):
                return DriverStatus(
                    state="outdated",
                    installed_endpoint_guid=installed_guid,
                    target_endpoint_guid=check_guid,
                    reason="Версія драйвера застаріла (потрібен ремонт)",
                    original_apo_clsid=original_apo,
                )
        except OSError:
            pass
        finally:
            shm.close()

    return DriverStatus(
        state="active",
        installed_endpoint_guid=installed_guid or check_guid,
        target_endpoint_guid=check_guid,
        reason="Драйвер активний",
        original_apo_clsid=original_apo,
    )


# ============================================================================
# Win32 Privilege & Registry Backup/Restore Helpers (Administrator path)
# ============================================================================

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", wintypes.DWORD),
        ("Luid", _LUID),
        ("Attributes", wintypes.DWORD),
    ]


def _ensure_win32_prototypes() -> None:
    """Explicitly configure ctypes argument and return types for 64-bit Win32 APIs."""
    if os.name != "nt":
        return
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL

    advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_LUID)]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL

    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(_TOKEN_PRIVILEGES),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    advapi32.RegCreateKeyExW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.RegCreateKeyExW.restype = wintypes.LONG

    advapi32.RegOpenKeyExW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.RegOpenKeyExW.restype = wintypes.LONG

    advapi32.RegDeleteTreeW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    advapi32.RegDeleteTreeW.restype = wintypes.LONG


def _enable_backup_restore_privileges() -> None:
    """Enable SeBackupPrivilege, SeRestorePrivilege, and SeTakeOwnershipPrivilege on current token."""
    if os.name != "nt":
        return
    _ensure_win32_prototypes()
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002

    h_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(h_token),
    ):
        return

    try:
        for priv_name in ("SeBackupPrivilege", "SeRestorePrivilege", "SeTakeOwnershipPrivilege"):
            luid = _LUID()
            if advapi32.LookupPrivilegeValueW(None, priv_name, ctypes.byref(luid)):
                tp = _TOKEN_PRIVILEGES(1, luid, SE_PRIVILEGE_ENABLED)
                advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
    finally:
        kernel32.CloseHandle(h_token)


def _open_hklm_key_backup_restore(subkey: str) -> int:
    """Open or create an HKLM key using REG_OPTION_BACKUP_RESTORE with standard DACL fallback."""
    if os.name != "nt":
        raise OSError("Registry is only supported on Windows")

    _ensure_win32_prototypes()
    _enable_backup_restore_privileges()

    HKEY_LOCAL_MACHINE_VAL = winreg.HKEY_LOCAL_MACHINE
    REG_OPTION_BACKUP_RESTORE = 0x00000004
    KEY_WOW64_64KEY = 0x0100
    KEY_ALL_ACCESS = 0x000F003F
    KEY_WRITE_BACKUP = winreg.KEY_READ | winreg.KEY_WRITE | KEY_WOW64_64KEY

    # Attempt 0: RegOpenKeyExW with REG_OPTION_BACKUP_RESTORE (fastest if key exists)
    hk_result = wintypes.HANDLE()
    rc0 = ctypes.windll.advapi32.RegOpenKeyExW(
        ctypes.c_void_p(HKEY_LOCAL_MACHINE_VAL),
        subkey,
        REG_OPTION_BACKUP_RESTORE,
        KEY_WRITE_BACKUP,
        ctypes.byref(hk_result),
    )
    if rc0 == 0 and hk_result.value:
        return int(hk_result.value)

    # Attempt 1: RegCreateKeyExW with REG_OPTION_BACKUP_RESTORE (bypasses ACL if privilege granted)
    disposition = wintypes.DWORD()
    rc1 = ctypes.windll.advapi32.RegCreateKeyExW(
        ctypes.c_void_p(HKEY_LOCAL_MACHINE_VAL),
        subkey,
        0,
        None,
        REG_OPTION_BACKUP_RESTORE,
        KEY_WRITE_BACKUP,
        None,
        ctypes.byref(hk_result),
        ctypes.byref(disposition),
    )
    if rc1 == 0 and hk_result.value:
        return int(hk_result.value)

    # Attempt 2: RegCreateKeyExW with standard options (relies on Administrator DACL)
    hk_result = wintypes.HANDLE()
    rc2 = ctypes.windll.advapi32.RegCreateKeyExW(
        ctypes.c_void_p(HKEY_LOCAL_MACHINE_VAL),
        subkey,
        0,
        None,
        0,
        KEY_ALL_ACCESS | KEY_WOW64_64KEY,
        None,
        ctypes.byref(hk_result),
        ctypes.byref(disposition),
    )
    if rc2 == 0 and hk_result.value:
        return int(hk_result.value)

    # Attempt 3: winreg.CreateKeyEx / OpenKey fallback
    try:
        h_winreg = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, subkey, 0, KEY_ALL_ACCESS | KEY_WOW64_64KEY)
        return int(h_winreg.Detach())
    except OSError:
        pass

    try:
        h_winreg = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_ALL_ACCESS | KEY_WOW64_64KEY)
        return int(h_winreg.Detach())
    except OSError:
        pass

    # Attempt 4: If access denied and admin, grant BUILTIN\Administrators FullControl via PowerShell and retry
    if is_admin():
        try:
            ps_cmd = (
                f"$acl = Get-Acl 'HKLM:\\{subkey}'; "
                f"$rule = New-Object System.Security.AccessControl.RegistryAccessRule("
                f"'BUILTIN\\Administrators','FullControl','ContainerInherit,ObjectInherit','None','Allow'); "
                f"$acl.SetAccessRule($rule); Set-Acl 'HKLM:\\{subkey}' $acl"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                check=False,
            )
            h_winreg = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_ALL_ACCESS | KEY_WOW64_64KEY)
            return int(h_winreg.Detach())
        except Exception:
            pass

    raise OSError(
        rc1 or rc2 or 5,
        f"RegCreateKeyExW failed for {subkey}: attempt0_err={rc0}, attempt1_err={rc1}, attempt2_err={rc2}",
    )


def _delete_hklm_subkey_tree(subkey: str) -> None:
    """Delete a subkey tree under HKLM using backup/restore privileges with winreg fallback."""
    if os.name != "nt":
        return
    _ensure_win32_prototypes()
    _enable_backup_restore_privileges()

    HKEY_LOCAL_MACHINE_VAL = winreg.HKEY_LOCAL_MACHINE
    rc = ctypes.windll.advapi32.RegDeleteTreeW(ctypes.c_void_p(HKEY_LOCAL_MACHINE_VAL), subkey)
    if rc == 0 or rc in (2, 3):  # 0 = success, 2 = ERROR_FILE_NOT_FOUND, 3 = ERROR_PATH_NOT_FOUND
        return

    try:
        winreg.DeleteTree(winreg.HKEY_LOCAL_MACHINE, subkey)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _read_all_registry_values(hkey: int) -> dict[str, dict[str, Any]]:
    """Read all values of an open registry key handle as {name: {'type': int, 'value': val}}."""
    out: dict[str, dict[str, Any]] = {}
    num_vals = winreg.QueryInfoKey(hkey)[1]
    for i in range(num_vals):
        name, val, reg_type = winreg.EnumValue(hkey, i)
        if reg_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ, winreg.REG_DWORD, winreg.REG_MULTI_SZ):
            out[name] = {"type": int(reg_type), "value": val}
    return out


def _is_our_or_spike_clsid(val: Any) -> bool:
    our_set = {normalize_guid(MUSICPAD_APO_CLSID)} | {normalize_guid(c) for c in LEGACY_SPIKE_CLSIDS}
    if isinstance(val, str):
        return normalize_guid(val) in our_set or "381d" in val.lower()
    if isinstance(val, list):
        return any(normalize_guid(str(x)) in our_set or "381d" in str(x).lower() for x in val)
    return False


def _stop_audio_services() -> None:
    """Stop Audiosrv and AudioEndpointBuilder before modifying endpoint registry or System32 DLL."""
    subprocess.run(["net", "stop", "Audiosrv", "/y"], capture_output=True, check=False)
    subprocess.run(["net", "stop", "AudioEndpointBuilder", "/y"], capture_output=True, check=False)
    subprocess.run(["taskkill", "/F", "/IM", "audiodg.exe"], capture_output=True, check=False)
    time.sleep(0.25)


def _start_audio_services() -> None:
    """Ensure AudioEndpointBuilder and Audiosrv are running."""
    subprocess.run(["net", "start", "AudioEndpointBuilder"], capture_output=True, check=False)
    subprocess.run(["net", "start", "Audiosrv"], capture_output=True, check=False)


def _restore_single_endpoint_from_backup(endpoint_guid: str, ep_backup: dict[str, Any]) -> None:
    """Restore Properties and FxProperties for a single endpoint from its backup dictionary."""
    norm_guid = normalize_guid(endpoint_guid)
    if not norm_guid:
        return

    ep_subkey = f"{MMDEVICES_CAPTURE_SUBKEY}\\{norm_guid}"
    fx_subkey = f"{ep_subkey}\\FxProperties"
    props_subkey = f"{ep_subkey}\\Properties"

    _delete_hklm_subkey_tree(f"{fx_subkey}\\EndpointProcessingModes")

    try:
        h_fx = _open_hklm_key_backup_restore(fx_subkey)
        try:
            current_vals = _read_all_registry_values(h_fx)
            for name, meta in current_vals.items():
                if _is_our_or_spike_clsid(meta["value"]):
                    try:
                        winreg.DeleteValue(h_fx, name)
                    except OSError:
                        pass
            for prop_to_clear in (
                PKEY_FX_ASSOC,
                PKEY_FX_SFX_SINGLE,
                PKEY_FX_MFX_SINGLE,
                PKEY_FX_EFX_SINGLE,
                PKEY_FX_SFX_MULTI,
                PKEY_FX_MFX_MULTI,
                PKEY_FX_EFX_MULTI,
                PKEY_MODE_SFX,
                PKEY_MODE_MFX,
                PKEY_MODE_EFX,
            ):
                try:
                    winreg.DeleteValue(h_fx, prop_to_clear)
                except OSError:
                    pass

            saved_fx = ep_backup.get("FxProperties", {})
            for name, meta in saved_fx.items():
                reg_type = int(meta.get("type", winreg.REG_SZ))
                val = meta.get("value")
                if not _is_our_or_spike_clsid(val):
                    winreg.SetValueEx(h_fx, name, 0, reg_type, val)
        finally:
            winreg.CloseKey(h_fx)
    except OSError:
        pass

    try:
        h_props = _open_hklm_key_backup_restore(props_subkey)
        try:
            saved_props = ep_backup.get("Properties", {})
            for pkey in (PKEY_DISABLE_SYSFX, PKEY_ALLOW_EXCLUSIVE, PKEY_EXCLUSIVE_PRIORITY):
                if pkey in saved_props and saved_props[pkey] is not None:
                    meta = saved_props[pkey]
                    winreg.SetValueEx(h_props, pkey, 0, int(meta["type"]), meta["value"])
        finally:
            winreg.CloseKey(h_props)
    except OSError:
        pass


def _load_or_init_backup() -> dict[str, Any]:
    if BACKUP_FILE.is_file():
        try:
            return json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"DisableProtectedAudioDG": None, "Endpoints": {}}


def _save_backup(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _snapshot_endpoint_if_needed(backup: dict[str, Any], endpoint_guid: str) -> dict[str, Any]:
    """Capture a clean snapshot of the endpoint's original Properties and FxProperties."""
    norm_guid = normalize_guid(endpoint_guid)
    endpoints_map = backup.setdefault("Endpoints", {})
    if norm_guid in endpoints_map:
        return endpoints_map[norm_guid]

    ep_subkey = f"{MMDEVICES_CAPTURE_SUBKEY}\\{norm_guid}"
    fx_subkey = f"{ep_subkey}\\FxProperties"
    props_subkey = f"{ep_subkey}\\Properties"

    clean_fx: dict[str, dict[str, Any]] = {}
    h_fx = _open_hklm_key_backup_restore(fx_subkey)
    try:
        for name, meta in _read_all_registry_values(h_fx).items():
            if not _is_our_or_spike_clsid(meta["value"]):
                clean_fx[name] = meta
    finally:
        winreg.CloseKey(h_fx)

    clean_props: dict[str, Any] = {}
    h_props = _open_hklm_key_backup_restore(props_subkey)
    try:
        all_props = _read_all_registry_values(h_props)
        for pkey in (PKEY_DISABLE_SYSFX, PKEY_ALLOW_EXCLUSIVE, PKEY_EXCLUSIVE_PRIORITY):
            clean_props[pkey] = all_props.get(pkey)
    finally:
        winreg.CloseKey(h_props)

    ep_entry = {"FxProperties": clean_fx, "Properties": clean_props}
    endpoints_map[norm_guid] = ep_entry
    return ep_entry


def _detect_original_efx_apo(ep_backup: dict[str, Any]) -> str:
    """Extract the original EFX APO CLSID from the clean endpoint backup, if any."""
    fx = ep_backup.get("FxProperties", {})
    for key in (PKEY_FX_EFX_SINGLE, PKEY_FX_EFX_MULTI, PKEY_FX_LFX_LEGACY):
        entry = fx.get(key)
        if not entry:
            continue
        val = entry.get("value")
        if isinstance(val, list) and val:
            val = val[0]
        if isinstance(val, str) and val.startswith("{") and not _is_our_or_spike_clsid(val):
            return val
    return ""


def _prepare_data_dir_and_shm() -> None:
    """Create C:\\ProgramData\\MusicPad and pre-initialize injection.bin with LocalService/Users ACLs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shm = InjectionShm(MPAD_SHM_DEFAULT_PATH)
    try:
        shm.open(acquire_lease=False)
        shm.reset_storage()
    finally:
        shm.close()

    subprocess.run(
        [
            "icacls.exe",
            str(DATA_DIR),
            "/grant",
            "*S-1-5-19:(OI)(CI)M",
            "*S-1-5-32-545:(OI)(CI)M",
        ],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "icacls.exe",
            str(MPAD_SHM_DEFAULT_PATH),
            "/grant",
            "*S-1-5-19:M",
            "*S-1-5-32-545:M",
        ],
        capture_output=True,
        check=False,
    )


def install_driver(endpoint_guid: Optional[str] = None) -> None:
    """Install or repair the MusicPad APO driver on the specified capture endpoint (requires Admin)."""
    if not is_admin():
        raise PermissionError("Встановлення драйвера потребує прав адміністратора (UAC).")

    norm_guid = normalize_guid(endpoint_guid)
    if not norm_guid:
        # Check if an endpoint was previously configured, or fall back to first active physical mic
        st = get_driver_status()
        norm_guid = st.installed_endpoint_guid
    if not norm_guid:
        eps = list_capture_endpoints(include_virtual_cables=False)
        if not eps:
            raise RuntimeError("Не знайдено жодного активного мікрофона для встановлення драйвера.")
        norm_guid = eps[0].guid

    apo_dll_src = _find_built_dll("musicpad_apo.dll")
    if apo_dll_src is None:
        raise FileNotFoundError("Не знайдено musicpad_apo.dll для встановлення.")
    dsp_dll_src = _find_built_dll("musicpad_dsp.dll")

    _prepare_data_dir_and_shm()
    backup = _load_or_init_backup()

    if "DisableProtectedAudioDG" not in backup or backup["DisableProtectedAudioDG"] is None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, AUDIO_CONFIG_SUBKEY, 0, winreg.KEY_READ) as k:
                val, _ = winreg.QueryValueEx(k, "DisableProtectedAudioDG")
                backup["DisableProtectedAudioDG"] = int(val)
        except OSError:
            backup["DisableProtectedAudioDG"] = "ABSENT"

    _stop_audio_services()
    try:
        # Restore any previously hooked endpoints before hooking the target endpoint (Decision 3)
        for prev_guid, prev_ep_backup in list(backup.get("Endpoints", {}).items()):
            if normalize_guid(prev_guid) != norm_guid:
                _restore_single_endpoint_from_backup(prev_guid, prev_ep_backup)

        ep_backup = _snapshot_endpoint_if_needed(backup, norm_guid)
        _save_backup(backup)

        # Copy DLLs to Program Files\MusicPad and C:\Windows\System32\musicpad_apo.dll
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        target_prog_apo = INSTALL_DIR / "musicpad_apo.dll"
        if apo_dll_src.resolve() != target_prog_apo.resolve():
            shutil.copy2(apo_dll_src, target_prog_apo)
        shutil.copy2(apo_dll_src, SYSTEM32_APO_DLL)

        if dsp_dll_src is not None:
            target_prog_dsp = INSTALL_DIR / "musicpad_dsp.dll"
            if dsp_dll_src.resolve() != target_prog_dsp.resolve():
                shutil.copy2(dsp_dll_src, target_prog_dsp)

        # Copy DeepFilterNet native assets for audiodg.exe
        df_dll_src = _find_built_dll("deepfilter.dll")
        if df_dll_src is not None:
            target_prog_df = INSTALL_DIR / "deepfilter.dll"
            if df_dll_src.resolve() != target_prog_df.resolve():
                shutil.copy2(df_dll_src, target_prog_df)
            shutil.copy2(df_dll_src, SYSTEM32_DF_DLL)
            subprocess.run(
                ["icacls.exe", str(SYSTEM32_DF_DLL), "/grant", "*S-1-5-19:RX", "*S-1-5-32-545:RX"],
                capture_output=True,
                check=False,
            )

        df_model_src = _find_built_dll("DeepFilterNet3_onnx.tar.gz")
        if df_model_src is not None:
            target_prog_model = INSTALL_DIR / "DeepFilterNet3_onnx.tar.gz"
            if df_model_src.resolve() != target_prog_model.resolve():
                shutil.copy2(df_model_src, target_prog_model)
            shutil.copy2(df_model_src, SYSTEM32_DF_MODEL)
            subprocess.run(
                ["icacls.exe", str(SYSTEM32_DF_MODEL), "/grant", "*S-1-5-19:RX", "*S-1-5-32-545:RX"],
                capture_output=True,
                check=False,
            )

        # Clean up any leftover Stage 0 spike DLL/CLSIDs
        if SYSTEM32_SPIKE_DLL.is_file():
            try:
                SYSTEM32_SPIKE_DLL.unlink()
            except OSError:
                pass
        for old_clsid in LEGACY_SPIKE_CLSIDS:
            _delete_hklm_subkey_tree(f"SOFTWARE\\Classes\\CLSID\\{old_clsid}")
            _delete_hklm_subkey_tree(f"SOFTWARE\\Classes\\AudioEngine\\AudioProcessingObjects\\{old_clsid}")
            _delete_hklm_subkey_tree(
                f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AudioEngine\\AudioProcessingObjects\\{old_clsid}"
            )

        subprocess.run(
            ["icacls.exe", str(INSTALL_DIR), "/grant", "*S-1-5-19:(OI)(CI)RX", "*S-1-5-32-545:(OI)(CI)RX"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["icacls.exe", str(SYSTEM32_APO_DLL), "/grant", "*S-1-5-19:RX", "*S-1-5-32-545:RX"],
            capture_output=True,
            check=False,
        )

        # Register COM CLSID and AudioEngine AudioProcessingObjects
        clsid_subkey = f"SOFTWARE\\Classes\\CLSID\\{MUSICPAD_APO_CLSID}"
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, clsid_subkey, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "MusicPad Audio Processing Object")
        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, f"{clsid_subkey}\\InprocServer32", 0, winreg.KEY_WRITE
        ) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(SYSTEM32_APO_DLL))
            winreg.SetValueEx(k, "ThreadingModel", 0, winreg.REG_SZ, "Both")

        for apo_root in (
            f"SOFTWARE\\Classes\\AudioEngine\\AudioProcessingObjects\\{MUSICPAD_APO_CLSID}",
            f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AudioEngine\\AudioProcessingObjects\\{MUSICPAD_APO_CLSID}",
        ):
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, apo_root, 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "FriendlyName", 0, winreg.REG_SZ, "MusicPad Audio Processing Object")
                winreg.SetValueEx(k, "Copyright", 0, winreg.REG_SZ, "Copyright (c) 2026 MusicPad")
                winreg.SetValueEx(k, "MajorVersion", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "MinorVersion", 0, winreg.REG_DWORD, 0)
                winreg.SetValueEx(k, "Flags", 0, winreg.REG_DWORD, 0x0000000E)
                winreg.SetValueEx(k, "MinInputConnections", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "MaxInputConnections", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "MinOutputConnections", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "MaxOutputConnections", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "MaxInstances", 0, winreg.REG_DWORD, 0xFFFFFFFF)
                winreg.SetValueEx(k, "NumAPOInterfaces", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(
                    k, "APOInterface0", 0, winreg.REG_SZ, "{FD7F2B29-24D0-4B5C-B177-592C39F9CA10}"
                )

        # Enable unsigned APO loading in audiodg.exe (Decision 16)
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, AUDIO_CONFIG_SUBKEY, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "DisableProtectedAudioDG", 0, winreg.REG_DWORD, 1)

        # Configure Properties on target microphone (Decision 8: enable enhancements, disable exclusive mode)
        ep_subkey = f"{MMDEVICES_CAPTURE_SUBKEY}\\{norm_guid}"
        h_props = _open_hklm_key_backup_restore(f"{ep_subkey}\\Properties")
        try:
            winreg.SetValueEx(h_props, PKEY_DISABLE_SYSFX, 0, winreg.REG_DWORD, 0)
            winreg.SetValueEx(h_props, PKEY_ALLOW_EXCLUSIVE, 0, winreg.REG_DWORD, 0)
            winreg.SetValueEx(h_props, PKEY_EXCLUSIVE_PRIORITY, 0, winreg.REG_DWORD, 0)
            for fault_key in (
                "{24dbb0fc-9311-4b3d-9cf0-18ff155639d4},1",
                "{24dbb0fc-9311-4b3d-9cf0-18ff155639d4},2",
                "{c876062a-a276-4ed6-b15b-3962e69007f8},3",
                "{3f777207-7e55-4a2a-8a26-39e31d49bdc1},0",
            ):
                try:
                    winreg.DeleteValue(h_props, fault_key)
                except OSError:
                    pass
        finally:
            winreg.CloseKey(h_props)

        # Configure FxProperties in EFX slot (Decision 1 & 7)
        fx_subkey = f"{ep_subkey}\\FxProperties"
        _delete_hklm_subkey_tree(f"{fx_subkey}\\EndpointProcessingModes")
        h_fx = _open_hklm_key_backup_restore(fx_subkey)
        try:
            current_vals = _read_all_registry_values(h_fx)
            for name, meta in current_vals.items():
                if _is_our_or_spike_clsid(meta["value"]):
                    try:
                        winreg.DeleteValue(h_fx, name)
                    except OSError:
                        pass
            winreg.SetValueEx(h_fx, PKEY_FX_ASSOC, 0, winreg.REG_SZ, NULL_GUID)
            winreg.SetValueEx(h_fx, PKEY_FX_EFX_SINGLE, 0, winreg.REG_SZ, MUSICPAD_APO_CLSID)
            winreg.SetValueEx(h_fx, PKEY_FX_EFX_MULTI, 0, winreg.REG_MULTI_SZ, [MUSICPAD_APO_CLSID])
            winreg.SetValueEx(h_fx, PKEY_MODE_EFX, 0, winreg.REG_MULTI_SZ, [DEFAULT_PROCESSING_MODE])
        finally:
            winreg.CloseKey(h_fx)

        # Record installed endpoint and OriginalApo in HKLM\SOFTWARE\MusicPad\APO
        original_apo = _detect_original_efx_apo(ep_backup)
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, MUSICPAD_APO_STATE_SUBKEY, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "InstalledEndpointGuid", 0, winreg.REG_SZ, norm_guid)
            winreg.SetValueEx(k, "ApoBuildVersion", 0, winreg.REG_DWORD, MPAD_APO_BUILD_VERSION)

        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, f"{MUSICPAD_APO_STATE_SUBKEY}\\{norm_guid}", 0, winreg.KEY_WRITE
        ) as k:
            if original_apo:
                winreg.SetValueEx(k, "OriginalApo", 0, winreg.REG_SZ, original_apo)
            else:
                try:
                    winreg.DeleteValue(k, "OriginalApo")
                except OSError:
                    pass

        # Write target_endpoint_guid into injection.bin header and ensure clean state
        shm = InjectionShm(MPAD_SHM_DEFAULT_PATH)
        try:
            if shm.open(acquire_lease=True):
                shm.reset_storage()
                shm.set_state(target_endpoint_guid=norm_guid, app_active=False)
        finally:
            shm.close()
    finally:
        _start_audio_services()


def uninstall_driver() -> None:
    """Uninstall the MusicPad APO driver and restore all backed-up system/endpoint settings (requires Admin)."""
    if not is_admin():
        raise PermissionError("Видалення драйвера потребує прав адміністратора (UAC).")

    backup = _load_or_init_backup()
    _stop_audio_services()
    try:
        for ep_guid, ep_backup in list(backup.get("Endpoints", {}).items()):
            _restore_single_endpoint_from_backup(ep_guid, ep_backup)

        # Also check InstalledEndpointGuid in case it wasn't in backup
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MUSICPAD_APO_STATE_SUBKEY, 0, winreg.KEY_READ) as k:
                inst_guid = normalize_guid(str(winreg.QueryValueEx(k, "InstalledEndpointGuid")[0]))
                if inst_guid and inst_guid not in backup.get("Endpoints", {}):
                    _restore_single_endpoint_from_backup(inst_guid, {})
        except OSError:
            pass

        # Restore DisableProtectedAudioDG (Decision 16)
        prev_prot = backup.get("DisableProtectedAudioDG")
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, AUDIO_CONFIG_SUBKEY, 0, winreg.KEY_WRITE) as k:
                if prev_prot == "ABSENT":
                    try:
                        winreg.DeleteValue(k, "DisableProtectedAudioDG")
                    except OSError:
                        pass
                elif isinstance(prev_prot, int):
                    winreg.SetValueEx(k, "DisableProtectedAudioDG", 0, winreg.REG_DWORD, int(prev_prot))
        except OSError:
            pass

        # Remove COM and AudioEngine registrations
        for clsid in {MUSICPAD_APO_CLSID} | LEGACY_SPIKE_CLSIDS:
            _delete_hklm_subkey_tree(f"SOFTWARE\\Classes\\CLSID\\{clsid}")
            _delete_hklm_subkey_tree(f"SOFTWARE\\Classes\\AudioEngine\\AudioProcessingObjects\\{clsid}")
            _delete_hklm_subkey_tree(
                f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AudioEngine\\AudioProcessingObjects\\{clsid}"
            )
        _delete_hklm_subkey_tree("SOFTWARE\\MusicPad")

        for dll_file in (SYSTEM32_APO_DLL, SYSTEM32_SPIKE_DLL, SYSTEM32_DF_DLL, SYSTEM32_DF_MODEL):
            if dll_file.is_file():
                try:
                    dll_file.unlink()
                except OSError:
                    pass

        if INSTALL_DIR.is_dir():
            shutil.rmtree(INSTALL_DIR, ignore_errors=True)

        if BACKUP_FILE.is_file():
            try:
                BACKUP_FILE.unlink()
            except OSError:
                pass
    finally:
        _start_audio_services()


def run_driver_cli(action: str, endpoint_guid: Optional[str] = None) -> int:
    """Execute `--driver install|uninstall|repair` CLI command and return process exit code."""
    act = action.strip().lower()
    try:
        if act in ("install", "repair"):
            install_driver(endpoint_guid=endpoint_guid)
        elif act == "uninstall":
            uninstall_driver()
        else:
            print(f"Невідома команда драйвера: {action}", file=sys.stderr)
            return 2
        return 0
    except Exception as exc:
        try:
            err_log = DATA_DIR / "driver_helper_error.log"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            err_log.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except Exception:
            pass
        print(f"Помилка операції з драйвером ({act}): {exc}", file=sys.stderr)
        return 1


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_driver_helper_uac(action: str, endpoint_guid: Optional[str] = None) -> tuple[bool, str]:
    """Launch `--driver install|uninstall|repair` elevated via UAC (`runas`) and wait for completion."""
    if os.name != "nt":
        return False, "Встановлення драйвера підтримується лише у Windows."

    if is_admin():
        rc = run_driver_cli(action, endpoint_guid=endpoint_guid)
        if rc == 0:
            return True, "Операцію успішно виконано."
        return False, "Не вдалося виконати операцію з драйвером."

    if getattr(sys, "frozen", False):
        exe_file = sys.executable
        params = f"--driver {action}"
    else:
        exe_file = sys.executable
        main_script = Path(__file__).resolve().parent.parent / "main.py"
        params = f'"{main_script}" --driver {action}'

    if endpoint_guid:
        raw_guid = normalize_guid(endpoint_guid).strip("{}")
        params += f" --endpoint-guid {raw_guid}"

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    INFINITE = 0xFFFFFFFF

    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = exe_file
    sei.lpParameters = params
    sei.lpDirectory = str(Path(__file__).resolve().parent.parent)
    sei.nShow = SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.GetLastError()
        if err == 1223:  # ERROR_CANCELLED
            return False, "Запит прав адміністратора (UAC) скасовано користувачем."
        return False, f"Не вдалося запустити UAC-helper (код {err})."

    try:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
        exit_code = wintypes.DWORD(1)
        ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code))
        if exit_code.value == 0:
            return True, "Операцію успішно виконано."
        err_log = DATA_DIR / "driver_helper_error.log"
        detail = ""
        if err_log.is_file():
            try:
                detail = err_log.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return False, detail or f"UAC-helper завершився з кодом {exit_code.value}."
    finally:
        if sei.hProcess:
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
