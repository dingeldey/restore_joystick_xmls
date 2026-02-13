# okb_update_directinput_guids.py
# ============================================================
# OpenKneeboard DirectInput GUID fixer (robust ANSI DirectInput)
#
# - Recursively scans all DirectInput.json under OpenKneeboard root
# - Enumerates attached DirectInput devices via IDirectInput8A
# - Matches by (Kind, Name) with strict exact match after normalization
# - Updates volatile GUIDs (dict key + "ID")
# - One ZIP backup per run (always created if there are pending changes)
# - Interactive Dry-Run / Replace
# - DRY-RUN prints exactly what would change
# - Waits for ENTER before exit
#
# Windows-only, no external dependencies
# ============================================================

import ctypes
import json
import zipfile
import configparser
import uuid
import struct
from ctypes import wintypes
from pathlib import Path
from datetime import datetime

# ============================================================
# Defaults (hard-coded)
# ============================================================

ZIP_PREFIX = "okb_directinput"
DEFAULT_DRY_RUN = True
CASE_INSENSITIVE_NAMES = True
NORMALIZE_WHITESPACE = True

# ============================================================
# Windows / DirectInput types
# ============================================================

HRESULT = ctypes.c_long

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]

MAX_PATH = 260

# DIDEVICEINSTANCEA (ANSI)
class DIDEVICEINSTANCEA(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("guidInstance", GUID),
        ("guidProduct", GUID),
        ("dwDevType", wintypes.DWORD),
        ("tszInstanceName", ctypes.c_char * MAX_PATH),
        ("tszProductName", ctypes.c_char * MAX_PATH),
        ("guidFFDriver", GUID),
        ("wUsagePage", wintypes.WORD),
        ("wUsage", wintypes.WORD),
    ]

LPDIDEVICEINSTANCEA = ctypes.POINTER(DIDEVICEINSTANCEA)

DI8DEVCLASS_ALL = 0
DIEDFL_ATTACHEDONLY = 0x00000001

DIDEVTYPE_KEYBOARD  = 0x12
DIDEVTYPE_MOUSE     = 0x13
DIDEVTYPE_JOYSTICK  = 0x14
DIDEVTYPE_GAMEPAD   = 0x15
DIDEVTYPE_DRIVING   = 0x16
DIDEVTYPE_FLIGHT    = 0x17
DIDEVTYPE_1STPERSON = 0x18

EnumDevicesCallbackA = ctypes.WINFUNCTYPE(wintypes.BOOL, LPDIDEVICEINSTANCEA, wintypes.LPVOID)

# ============================================================
# Helpers
# ============================================================

def guid_from_string_py(s: str) -> GUID:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    u = uuid.UUID(s)

    b = u.bytes_le
    data1, data2, data3 = struct.unpack("<IHH", b[:8])

    g = GUID()
    g.Data1 = data1
    g.Data2 = data2
    g.Data3 = data3
    for i in range(8):
        g.Data4[i] = b[8 + i]
    return g

def guid_to_string(g: GUID) -> str:
    d4 = bytes(g.Data4)
    return (
        f"{{{g.Data1:08x}-{g.Data2:04x}-{g.Data3:04x}-"
        f"{d4[0]:02x}{d4[1]:02x}-"
        f"{d4[2]:02x}{d4[3]:02x}{d4[4]:02x}{d4[5]:02x}{d4[6]:02x}{d4[7]:02x}}}"
    ).upper()

def kind_from_dwDevType(dwDevType: int) -> str:
    t = dwDevType & 0xFF
    if t == DIDEVTYPE_KEYBOARD:
        return "Keyboard"
    if t == DIDEVTYPE_MOUSE:
        return "Mouse"
    if t in (
            DIDEVTYPE_JOYSTICK,
            DIDEVTYPE_GAMEPAD,
            DIDEVTYPE_DRIVING,
            DIDEVTYPE_FLIGHT,
            DIDEVTYPE_1STPERSON,
    ):
        return "GameController"
    return "Other"

def normalize_name(name: str) -> str:
    s = name
    if NORMALIZE_WHITESPACE:
        s = " ".join(s.split())
    if CASE_INSENSITIVE_NAMES:
        s = s.casefold()
    return s

def decode_ansi(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("mbcs", errors="replace").strip()

def ask_mode(default_dry_run=True) -> bool:
    default = "D" if default_dry_run else "R"
    while True:
        ans = input(f"Mode? [D]ry-run or [R]eplace (default {default}): ").strip().lower()
        if ans == "":
            return default_dry_run
        if ans.startswith("d"):
            return True
        if ans.startswith("r"):
            return False
        print("Please enter D or R.")

def wait_for_key():
    try:
        input("Press ENTER to exit...")
    except EOFError:
        pass

import sys

def get_executable_dir() -> Path:
    """
    Returns the directory where the EXE lives (PyInstaller-safe),
    or the script directory when run as .py.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def load_ini():
    exe_dir = get_executable_dir()
    ini_path = exe_dir / "okb_update_directinput_guids.ini"

    if not ini_path.exists():
        raise SystemExit(f"INI file not found: {ini_path}")

    cfg = configparser.ConfigParser()
    cfg.read(ini_path, encoding="utf-8")

    if "paths" not in cfg:
        raise SystemExit("INI missing [paths] section")

    okb_root = Path(cfg["paths"]["openkneeboard_root"]).expanduser()
    backup_root = Path(cfg["paths"]["backup_root"]).expanduser()

    if not okb_root.exists():
        raise SystemExit(f"OpenKneeboard root does not exist: {okb_root}")

    backup_root.mkdir(parents=True, exist_ok=True)
    return okb_root, backup_root

# ============================================================
# DirectInput enumeration (IDirectInput8A)
# ============================================================

def enumerate_directinput_devices():
    dinput8 = ctypes.WinDLL("dinput8", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    DirectInput8Create = dinput8.DirectInput8Create
    DirectInput8Create.argtypes = [
        wintypes.HINSTANCE,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    DirectInput8Create.restype = HRESULT

    # IID_IDirectInput8A
    iid = guid_from_string_py("{BF798030-483A-4DA2-AA99-5D64ED369700}")

    hinst = kernel32.GetModuleHandleW(None)
    if not hinst:
        raise RuntimeError(f"GetModuleHandleW failed (WinErr={ctypes.get_last_error()})")

    DIRECTINPUT_VERSION = 0x0800
    di_ptr = ctypes.c_void_p()

    hr = DirectInput8Create(hinst, DIRECTINPUT_VERSION, ctypes.byref(iid), ctypes.byref(di_ptr), None)
    if hr < 0 or not di_ptr.value:
        raise RuntimeError(f"DirectInput8Create failed (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

    vtbl = ctypes.cast(di_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

    EnumDevices = ctypes.WINFUNCTYPE(
        HRESULT,
        ctypes.c_void_p,
        wintypes.DWORD,
        EnumDevicesCallbackA,
        wintypes.LPVOID,
        wintypes.DWORD
    )(vtbl[4])

    Release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])

    results = {}

    @EnumDevicesCallbackA
    def cb(pinst, _ref):
        inst = pinst.contents
        kind = kind_from_dwDevType(inst.dwDevType)

        product = decode_ansi(bytes(inst.tszProductName))
        instance = decode_ansi(bytes(inst.tszInstanceName))
        name = product or instance

        key = (kind, normalize_name(name))
        results.setdefault(key, []).append(guid_to_string(inst.guidInstance))
        return True

    hr = EnumDevices(di_ptr, DI8DEVCLASS_ALL, cb, None, DIEDFL_ATTACHEDONLY)
    Release(di_ptr)

    if hr < 0:
        raise RuntimeError(f"EnumDevices failed (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

    return results

# ============================================================
# Reporting
# ============================================================

def print_enumerated_devices(device_map):
    print("Enumerated DirectInput devices (used for matching):")
    print("--------------------------------------------------")
    if not device_map:
        print("  (none)")
        print("--------------------------------------------------\n")
        return
    for (kind, norm_name), guids in sorted(device_map.items()):
        # norm_name is normalized because matching is strict on this
        print(f"[{kind}] {norm_name}")
        for g in guids:
            print(f"  GUID: {g}")
    print("--------------------------------------------------\n")

def print_directinput_files(files):
    print("DirectInput.json files checked:")
    print("-------------------------------")
    if not files:
        print("  (none)")
    for f in files:
        print(f"  {f}")
    print("-------------------------------\n")

def print_planned_changes(plans, okb_root: Path):
    """
    plans: list of dicts:
      {
        'file': Path,
        'changes': [ {kind, name, from_key, from_id, to} ... ],
        'new_data': dict
      }
    """
    print("Planned changes (what would be updated):")
    print("---------------------------------------")
    if not plans:
        print("  (none)")
        print("---------------------------------------\n")
        return

    total = 0
    for plan in plans:
        rel = plan["file"].relative_to(okb_root)
        ch = plan["changes"]
        if not ch:
            continue
        total += len(ch)
        print(f"{rel}  ({len(ch)} change(s))")
        for c in ch:
            # show the *original JSON name* (not normalized)
            print(f"  - [{c['kind']}] {c['name']}")
            print(f"    key: {c['from_key']} -> {c['to']}")
            # Only show ID if it differs or exists
            if c["from_id"] != c["to"]:
                print(f"    id : {c['from_id']} -> {c['to']}")
        print()
    print(f"Total changes: {total}")
    print("---------------------------------------\n")

# ============================================================
# JSON processing (collect detailed plan)
# ============================================================

def build_plan_for_file(path: Path, device_map):
    """
    Returns:
      None if no changes needed
      else dict with {file, changes[], new_data}
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    devices = data.get("Devices", {})
    if not isinstance(devices, dict):
        return None

    new_devices = {}
    changes = []

    for old_guid, entry in devices.items():
        if not isinstance(entry, dict):
            new_devices[old_guid] = entry
            continue

        kind = entry.get("Kind")
        name = entry.get("Name")
        old_id = entry.get("ID", old_guid)

        if not kind or not name:
            new_devices[old_guid] = entry
            continue

        key = (kind, normalize_name(name))
        matches = device_map.get(key)

        if not matches:
            new_devices[old_guid] = entry
            continue

        new_guid = matches[0]
        old_key_u = str(old_guid).upper()
        old_id_u = str(old_id).upper()

        if old_key_u != new_guid or old_id_u != new_guid:
            changes.append({
                "kind": kind,
                "name": name,         # keep original JSON name for readability
                "from_key": old_key_u,
                "from_id": old_id_u,
                "to": new_guid,
            })

        new_entry = dict(entry)
        new_entry["ID"] = new_guid
        new_devices[new_guid] = new_entry

    if not changes:
        return None

    new_data = dict(data)
    new_data["Devices"] = new_devices
    return {"file": path, "changes": changes, "new_data": new_data}

# ============================================================
# Main
# ============================================================

def main():
    okb_root, backup_root = load_ini()
    dry_run = ask_mode(DEFAULT_DRY_RUN)

    print(f"\nOpenKneeboard root : {okb_root}")
    print(f"Backup root        : {backup_root}")
    print(f"Mode               : {'DRY-RUN' if dry_run else 'REPLACE'}\n")

    device_map = enumerate_directinput_devices()
    # print_enumerated_devices(device_map)

    json_files = sorted(okb_root.rglob("DirectInput.json"))
    # _directinput_files(json_files)

    if not json_files:
        print("No DirectInput.json files found.")
        wait_for_key()
        return

    plans = []
    for f in json_files:
        plan = build_plan_for_file(f, device_map)
        if plan:
            plans.append(plan)

    # Show what would change (this is what you asked for)
    print_planned_changes(plans, okb_root)

    if not plans:
        print("No changes required.")
        wait_for_key()
        return

    # ZIP backup (original files that will be changed)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    zip_path = backup_root / f"{ZIP_PREFIX}_{ts}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for plan in plans:
            p = plan["file"]
            zf.write(p, p.relative_to(okb_root))

    print(f"Backup written: {zip_path}")

    if not dry_run:
        for plan in plans:
            p = plan["file"]
            new_data = plan["new_data"]
            p.write_text(json.dumps(new_data, indent=2) + "\n", encoding="utf-8")
        print("Changes applied.")
    else:
        print("Dry-run: no files modified.")

    wait_for_key()

if __name__ == "__main__":
    main()
