import configparser
import hashlib
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

GUID_FILENAME_RE = re.compile(r"^(?P<stem>.*)\s+\{[0-9A-Fa-f-]{36}\}\.xml$")


@dataclass(frozen=True)
class Settings:
    bms_config_dir: Path
    backup_dir: Path
    zip_name_prefix: str
    zip_destination: str  # "backup_dir" or "bms_config_dir"
    copy_if_missing: bool


def read_settings(config_path: Path) -> Settings:
    if not config_path.exists():
        raise FileNotFoundError(f"config.ini not found at: {config_path}")

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    bms_config_dir = Path(cfg.get("paths", "bms_config_dir")).expanduser()
    backup_dir = Path(cfg.get("paths", "backup_dir")).expanduser()

    zip_name_prefix = cfg.get("zip", "zip_name_prefix", fallback="config").strip() or "config"
    zip_destination = cfg.get("zip", "zip_destination", fallback="backup_dir").strip()
    if zip_destination not in ("backup_dir", "bms_config_dir"):
        raise ValueError("zip_destination must be 'backup_dir' or 'bms_config_dir'")

    copy_if_missing = cfg.getboolean("behavior", "copy_if_missing", fallback=False)

    return Settings(
        bms_config_dir=bms_config_dir,
        backup_dir=backup_dir,
        zip_name_prefix=zip_name_prefix,
        zip_destination=zip_destination,
        copy_if_missing=copy_if_missing,
    )


def normalize_key(xml_filename: str) -> str:
    """
    Turn:
      'Setup.v100.WINCTRL ... {GUID}.xml'
    into:
      'Setup.v100.WINCTRL ...'
    If it doesn't match the GUID pattern, fall back to the plain stem.
    """
    m = GUID_FILENAME_RE.match(xml_filename)
    if m:
        return m.group("stem").strip()
    # fallback: remove trailing ".xml"
    return Path(xml_filename).stem.strip()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_xml_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".xml"])


def build_index(folder: Path) -> dict[str, list[Path]]:
    """
    key -> list of matching files (usually 1)
    """
    idx: dict[str, list[Path]] = {}
    for p in list_xml_files(folder):
        k = normalize_key(p.name)
        idx.setdefault(k, []).append(p)
    return idx


def choose_target(paths: list[Path]) -> Path:
    """
    If there are multiple candidates, pick the newest modified one.
    """
    if len(paths) == 1:
        return paths[0]
    return max(paths, key=lambda p: p.stat().st_mtime)


def make_zip_of_bms_config(settings: Settings) -> Path:
    src_dir = settings.bms_config_dir
    dest_dir = (
        settings.backup_dir
        if settings.zip_destination == "backup_dir"
        else settings.bms_config_dir
    )

    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    zip_path = dest_dir / f"{settings.zip_name_prefix}.{ts}.zip"

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            root_path = Path(root)
            for fname in files:
                full_path = root_path / fname
                rel_path = full_path.relative_to(src_dir)
                zf.write(full_path, arcname=rel_path)

    print(f"[ZIP] Wrote full folder backup: {zip_path}")
    return zip_path


def restore_from_backups(settings: Settings) -> None:
    backup_dir = settings.backup_dir
    bms_dir = settings.bms_config_dir

    if not backup_dir.exists():
        print(f"[WARN] Backup dir does not exist: {backup_dir}")
        return
    if not bms_dir.exists():
        raise FileNotFoundError(f"BMS config dir does not exist: {bms_dir}")

    backup_index = build_index(backup_dir)
    bms_index = build_index(bms_dir)

    restored = 0
    identical = 0
    missing = 0

    for key, backup_files in backup_index.items():
        backup_file = choose_target(backup_files)

        targets = bms_index.get(key, [])
        if not targets:
            missing += 1
            if settings.copy_if_missing:
                dest = bms_dir / backup_file.name
                shutil.copy2(backup_file, dest)
                print(f"[COPY] Missing in BMS -> copied backup as: {dest.name}")
            else:
                print(f"[MISS] No matching target in BMS for: '{key}'")
            continue

        target = choose_target(targets)

        hb = sha256_file(backup_file)
        ht = sha256_file(target)
        if hb == ht:
            identical += 1
            # No need to spam too much, but keep one line:
            print(f"[OK]   {target.name} already matches backup")
            continue

        # Overwrite CONTENT only, keep target filename (incl. new GUID)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            shutil.copyfile(backup_file, tmp)
            os.replace(tmp, target)  # atomic replace on Windows
            restored += 1
            print(f"[FIX]  Restored content -> {target.name}")
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
        print("  (copy_if_missing=true was enabled)")


def default_ini_path(filename: str) -> Path:
    # When frozen (PyInstaller), sys.executable is the .exe path
    if getattr(sys, "frozen", False):
        return Path(sys.executable).with_name(filename)
    # Normal python run
    return Path(__file__).with_name(filename)

def main() -> int:
    # Expect config.ini next to the script, unless user passes a path
    config_path = default_ini_path("bmsconfig.ini")
    if len(sys.argv) >= 2:
        config_path = Path(sys.argv[1]).expanduser()

    settings = read_settings(config_path)

    # Basic sanity checks
    if not settings.bms_config_dir.exists():
        raise FileNotFoundError(f"bms_config_dir not found: {settings.bms_config_dir}")
    if not settings.backup_dir.exists():
        print(f"[WARN] backup_dir not found yet (ok if empty/new): {settings.backup_dir}")

    # Step 1: zip current BMS config XMLs
    make_zip_of_bms_config(settings)

    # Step 2: restore from backups (content replacement keeping new filename/GUID)
    restore_from_backups(settings)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
