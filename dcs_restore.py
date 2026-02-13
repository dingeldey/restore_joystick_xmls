import configparser
import hashlib
import os
import shutil
import sys
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

GUID_DIFF_RE = re.compile(r"^(?P<stem>.*)\s+\{[0-9A-Fa-f-]{36}\}\.diff\.lua$")


@dataclass(frozen=True)
class Settings:
    dcs_input_dir: Path
    backup_dir: Path
    zip_name_prefix: str
    zip_destination: str  # "backup_dir" or "dcs_input_dir"
    copy_if_missing: bool

def ask_run_mode() -> bool:
    """
    Returns True if dry-run is selected, False if replace is selected.
    """
    while True:
        print("")
        print("Select mode:")
        print("  [D] Dry run (no changes)")
        print("  [R] Replace files")
        choice = input("Choice [D/R]: ").strip().lower()

        if choice == "" or choice == "d":
            print("Dry-run selected. No files will be modified.")
            return True
        if choice == "r":
            print("Replace mode selected. Files may be modified.")
            return False

        print("Invalid choice. Please enter D or R.")


def read_settings(config_path: Path) -> Settings:
    if not config_path.exists():
        raise FileNotFoundError(f"dcsconfig.ini not found at: {config_path}")

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    dcs_input_dir = Path(cfg.get("paths", "dcs_input_dir")).expanduser()
    backup_dir = Path(cfg.get("paths", "backup_dir")).expanduser()

    zip_name_prefix = cfg.get("zip", "zip_name_prefix", fallback="config").strip() or "config"
    zip_destination = cfg.get("zip", "zip_destination", fallback="backup_dir").strip()
    if zip_destination not in ("backup_dir", "dcs_input_dir"):
        raise ValueError("zip_destination must be 'backup_dir' or 'dcs_input_dir'")

    copy_if_missing = cfg.getboolean("behavior", "copy_if_missing", fallback=False)

    return Settings(
        dcs_input_dir=dcs_input_dir,
        backup_dir=backup_dir,
        zip_name_prefix=zip_name_prefix,
        zip_destination=zip_destination,
        copy_if_missing=copy_if_missing,
    )


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_guid_filename(name: str) -> str:
    """
    Turn:
      'WINWING ICP {GUID}.diff.lua'
    into:
      'WINWING ICP'
    If it doesn't match, return the filename as-is.
    """
    m = GUID_DIFF_RE.match(name)
    if m:
        return m.group("stem").strip()
    return name


def choose_target(candidates: list[Path]) -> Path:
    """
    If multiple candidates exist, choose the most recently modified.
    """
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda p: p.stat().st_mtime)


def zip_entire_folder(src_dir: Path, dest_dir: Path, prefix: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    zip_path = dest_dir / f"{prefix}.{ts}.zip"

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_dir):
            root_path = Path(root)
            for fname in files:
                full_path = root_path / fname
                rel_path = full_path.relative_to(src_dir)
                zf.write(full_path, arcname=rel_path)

    print(f"[ZIP] Wrote full folder backup: {zip_path}")
    return zip_path


def restore_dcs(settings: Settings, dry_run) -> None:
    backup_root = settings.backup_dir
    dcs_root = settings.dcs_input_dir

    if not backup_root.exists():
        raise FileNotFoundError(f"backup_dir does not exist: {backup_root}")
    if not dcs_root.exists():
        raise FileNotFoundError(f"dcs_input_dir does not exist: {dcs_root}")

    restored = 0
    identical = 0
    missing = 0
    copied_missing = 0

    # Walk every file in backup tree and apply to matching location in DCS tree
    for backup_file in sorted(backup_root.rglob("*")):
        if not backup_file.is_file():
            continue

        # Skip zip artifacts (and any other non-binding files)
        if backup_file.suffix.lower() == ".zip":
            continue

        # Only handle device-specific diff files with GUID in filename
        if backup_file.suffixes[-2:] != [".diff", ".lua"]:
            continue
        if not GUID_DIFF_RE.match(backup_file.name):
            continue

        # Strongly recommended: only joystick device bindings (not keyboard/mouse)
        if backup_file.parent.name.lower() != "joystick":
            continue


        rel = backup_file.relative_to(backup_root)
        target_parent = dcs_root / rel.parent

        # If the target folder doesn't exist, either skip or create+copy if enabled
        if not target_parent.exists():
            missing += 1
            if settings.copy_if_missing:
                target_parent.mkdir(parents=True, exist_ok=True)
                dest = dcs_root / rel
                shutil.copy2(backup_file, dest)
                copied_missing += 1
                print(f"[COPY] Created folder + copied: {rel.as_posix()}")
            else:
                print(f"[MISS] Target folder missing: {rel.parent.as_posix()}")
            continue

        # GUID-based diff.lua: match by stem within same folder
        if backup_file.suffixes[-2:] == [".diff", ".lua"] and GUID_DIFF_RE.match(backup_file.name):
            key = normalize_guid_filename(backup_file.name)

            # Find candidates in target folder with same normalized name
            candidates = []
            for p in target_parent.iterdir():
                if not p.is_file():
                    continue
                if p.suffixes[-2:] != [".diff", ".lua"]:
                    continue
                if normalize_guid_filename(p.name) == key:
                    candidates.append(p)

            if not candidates:
                missing += 1
                if settings.copy_if_missing:
                    # No target GUID exists; copy backup as-is (backup GUID filename)
                    dest = target_parent / backup_file.name
                    shutil.copy2(backup_file, dest)
                    copied_missing += 1
                    print(f"[COPY] No target match -> copied backup file: {rel.as_posix()}")
                else:
                    print(f"[MISS] No target match for device '{key}' in {rel.parent.as_posix()}")
                continue

            target_file = choose_target(candidates)

            hb = sha256_file(backup_file)
            ht = sha256_file(target_file)
            if hb == ht:
                identical += 1
                print(f"[OK]   {target_file.relative_to(dcs_root).as_posix()} already matches backup")
                continue

            # Replace content while keeping target filename/GUID
            tmp = target_file.with_suffix(target_file.suffix + ".tmp")
            try:
                if dry_run:
                    print(f"[DRYRUN] Would restore -> {target_file.relative_to(dcs_root)}")
                else:
                    shutil.copyfile(backup_file, tmp)
                    os.replace(tmp, target_file)
                    print(f"[FIX]  Restored -> {target_file.relative_to(dcs_root)}")
                restored += 1
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

        else:
            # Non-GUID file: match exact relative path
            target_file = dcs_root / rel

            if not target_file.exists():
                missing += 1
                if settings.copy_if_missing:
                    shutil.copy2(backup_file, target_file)
                    copied_missing += 1
                    print(f"[COPY] Missing -> copied: {rel.as_posix()}")
                else:
                    print(f"[MISS] Missing file: {rel.as_posix()}")
                continue

            hb = sha256_file(backup_file)
            ht = sha256_file(target_file)
            if hb == ht:
                identical += 1
                # keep quiet-ish:
                print(f"[OK]   {rel.as_posix()} already matches backup")
                continue

            tmp = target_file.with_suffix(target_file.suffix + ".tmp")
            try:
                if dry_run:
                    print(f"[DRYRUN] Would restore -> {target_file.relative_to(dcs_root)}")
                else:
                    shutil.copyfile(backup_file, tmp)
                    os.replace(tmp, target_file)
                    print(f"[FIX]  Restored -> {target_file.relative_to(dcs_root)}")
                restored += 1
                print(f"[FIX]  Restored -> {rel.as_posix()}")
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    print("")
    print("[SUMMARY]")
    print(f"  Restored (content replaced): {restored}")
    print(f"  Already identical:           {identical}")
    print(f"  Missing targets:             {missing}")
    if settings.copy_if_missing:
        print(f"  Copied missing:              {copied_missing}")

def pause_exit() -> None:
    print("")
    print("Press any key to continue...")
    try:
        import msvcrt
        msvcrt.getch()
    except Exception:
        input()

def default_ini_path(filename: str) -> Path:
    # When frozen (PyInstaller), sys.executable is the .exe path
    if getattr(sys, "frozen", False):
        return Path(sys.executable).with_name(filename)
    # Normal python run
    return Path(__file__).with_name(filename)

def main() -> int:
    config_path = default_ini_path("dcsconfig.ini")
    if len(sys.argv) >= 2:
        config_path = Path(sys.argv[1]).expanduser()

    settings = read_settings(config_path)
    dry_run = ask_run_mode()

    # Step 1: zip snapshot (entire folder)
    if not dry_run:
        # Put ZIPs one hierarchy level above the backup directory
        dest_dir = settings.backup_dir.parent
        dest_dir.mkdir(parents=True, exist_ok=True)

        zip_entire_folder(
            settings.dcs_input_dir,
            dest_dir,
            settings.zip_name_prefix,
        )


    # Step 2: restore
    restore_dcs(settings, dry_run)
    pause_exit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
