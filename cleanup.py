#!/usr/bin/env python3
"""
Phase 3: Cleanup — restore original filenames.

After reviewing duplicates in the Phase 2 UI and staging unwanted files, run this
script to:

  1. Strip the Dup###x_ prefix from any duplicate files still in the main directory
     (i.e. the ones you chose to keep).
  2. Report any files flagged with _Review_ prefix — they are left untouched.
  3. Delete manifest.json, 000_Scanned.log, and the .thumbcache folder.

The 000_To_Delete staging folder is left in place. Delete it manually when ready.

Usage:
    python cleanup.py --dir /path/to/files
    python cleanup.py --dir /path/to/files --dry-run
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


STAGING_DIR_NAME = "000_To_Delete"
THUMB_CACHE_DIR_NAME = ".thumbcache"
DUP_PREFIX_PATTERN = re.compile(r'^Dup\d{3}[a-z]_', re.IGNORECASE)
REVIEW_PREFIX = "_Review_"


def load_manifest(directory: Path) -> dict:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest.json found in '{directory}'.")
        print("Run scan.py first.")
        sys.exit(1)
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def safe_restore_target(directory: Path, original_name: str) -> Path:
    """
    Return a path for the restored filename that does not collide.
    If original_name already exists, appends _2, _3, etc. to the stem.
    """
    candidate = directory / original_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 3: Restore original filenames after duplicate review.",
    )
    parser.add_argument("--dir",     required=True,       help="Directory to clean up")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()

    directory = Path(args.dir).resolve()
    if not directory.is_dir():
        print(f"Error: '{args.dir}' is not a valid directory.")
        sys.exit(1)

    staging_dir = directory / STAGING_DIR_NAME
    manifest = load_manifest(directory)
    separator = "-" * 60

    print(separator)
    print("File Organizer — Phase 3: Cleanup")
    print(separator)
    print(f"Directory : {directory}")
    if args.dry_run:
        print("Mode      : DRY RUN (no changes will be made)")
    print(separator)

    # -------------------------------------------------------------------------
    # Build lookup: current renamed filename -> original filename
    # -------------------------------------------------------------------------
    rename_map: dict[str, str] = {}
    for entry in manifest.get("duplicates", []):
        renamed_to   = entry.get("renamed_to", "")
        original     = entry.get("original_name", "")
        if renamed_to and original:
            rename_map[renamed_to] = original

    # -------------------------------------------------------------------------
    # Pass 1: find files in the main directory that need their prefix stripped
    # -------------------------------------------------------------------------
    to_restore: list[tuple[Path, Path]] = []   # (current_path, target_path)
    review_flagged: list[str] = []
    already_clean: int = 0

    for current_path in sorted(directory.iterdir()):
        if not current_path.is_file():
            continue
        name = current_path.name

        # Skip manifest, log, and hidden files
        if name in ("manifest.json", "000_Scanned.log") or name.startswith("."):
            continue

        if name.startswith(REVIEW_PREFIX):
            review_flagged.append(name)
            continue

        if name in rename_map:
            original_name = rename_map[name]
            target = safe_restore_target(directory, original_name)
            to_restore.append((current_path, target))
        elif DUP_PREFIX_PATTERN.match(name):
            # Has Dup prefix but wasn't in the manifest (edge case — still strip it)
            stripped = DUP_PREFIX_PATTERN.sub("", name)
            target = safe_restore_target(directory, stripped)
            to_restore.append((current_path, target))
        else:
            already_clean += 1

    # -------------------------------------------------------------------------
    # Report
    # -------------------------------------------------------------------------
    print(f"\nFiles to restore  : {len(to_restore)}")
    print(f"Already clean     : {already_clean}")
    print(f"Flagged _Review_  : {len(review_flagged)}")

    staged_files = [f for f in staging_dir.iterdir() if f.is_file()] if staging_dir.exists() else []
    staged_mb = sum(f.stat().st_size for f in staged_files) / (1024 * 1024)
    print(f"Staged for delete : {len(staged_files)} file(s)  ({staged_mb:.1f} MB)  — delete {STAGING_DIR_NAME} manually when ready")

    # -------------------------------------------------------------------------
    # Restore
    # -------------------------------------------------------------------------
    if to_restore:
        print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Restoring original filenames...")
        restored = 0
        errors = 0
        for current_path, target_path in to_restore:
            if args.dry_run:
                print(f"  [would rename] {current_path.name}  ->  {target_path.name}")
            else:
                try:
                    current_path.rename(target_path)
                    print(f"  [restored]     {current_path.name}  ->  {target_path.name}")
                    restored += 1
                except OSError as exc:
                    print(f"  [error]        {current_path.name}: {exc}")
                    errors += 1
        if not args.dry_run:
            print(f"\n  {restored} file(s) restored.", end="")
            if errors:
                print(f"  {errors} error(s).", end="")
            print()
    else:
        print("\nNo Dup-prefixed files found in directory — nothing to restore.")

    # -------------------------------------------------------------------------
    # Scan artefacts (manifest, log, thumbnail cache)
    # -------------------------------------------------------------------------
    artefacts = [
        directory / "manifest.json",
        directory / "000_Scanned.log",
    ]
    thumb_dir = directory / THUMB_CACHE_DIR_NAME

    if args.dry_run:
        for a in artefacts:
            if a.exists():
                print(f"\n[DRY RUN] Would delete: {a.name}")
        if thumb_dir.exists():
            thumb_count = sum(1 for f in thumb_dir.iterdir() if f.is_file())
            print(f"\n[DRY RUN] Would delete thumbnail cache ({thumb_count} file(s)): {thumb_dir}")
    else:
        for a in artefacts:
            if a.exists():
                try:
                    a.unlink()
                    print(f"\nDeleted: {a.name}")
                except OSError as exc:
                    print(f"\n[warn] Could not delete {a.name}: {exc}")
        if thumb_dir.exists():
            try:
                shutil.rmtree(thumb_dir)
                print(f"Deleted: {thumb_dir.name}/")
            except OSError as exc:
                print(f"\n[warn] Could not delete thumbnail cache: {exc}")

    # -------------------------------------------------------------------------
    # Review-flagged files
    # -------------------------------------------------------------------------
    if review_flagged:
        print(f"\n{separator}")
        print(f"_Review_ flagged files ({len(review_flagged)}) — left untouched:")
        for name in review_flagged:
            print(f"  {name}")

    # -------------------------------------------------------------------------
    # Done
    # -------------------------------------------------------------------------
    print(f"\n{separator}")
    if args.dry_run:
        print("Dry run complete. No files were modified.")
    else:
        print("Cleanup complete.")
    print(separator)


if __name__ == "__main__":
    main()
