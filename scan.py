#!/usr/bin/env python3
"""
Phase 1: Media file duplicate scanner and renamer.

Scans a flat directory for mp3, mp4, mov, and jpeg files.
Detects duplicates by content hash (regardless of filename).
Verifies file types via magic bytes.
Renames every file in a duplicate group with the scheme:

    Dup{group:03d}{letter}_{original_filename}

  - group  : 3-digit zero-padded counter, increments per duplicate group found
  - letter : 'a' for oldest file, 'b' for next oldest, etc.

Outputs a manifest.json for use by the Phase 2 review UI.

Usage:
    python scan.py --dir /path/to/files
    python scan.py --dir /path/to/files --dry-run
"""

import argparse
import hashlib
import json
import re
import string
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Magic byte signatures for supported media types
# Format: (byte_offset, expected_bytes)
# ---------------------------------------------------------------------------
MAGIC_SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    "jpeg": [
        (0, b"\xFF\xD8\xFF"),
    ],
    "mp3": [
        (0, b"ID3"),        # ID3v2 tag header
        (0, b"\xFF\xFB"),   # MPEG1 Layer3, no CRC
        (0, b"\xFF\xF3"),   # MPEG2 Layer3, no CRC
        (0, b"\xFF\xF2"),   # MPEG2 Layer3, CRC
    ],
    "mp4": [
        (4, b"ftyp"),       # ISO Base Media / MP4 box header
    ],
    "mov": [
        (4, b"ftyp"),       # QuickTime/MOV shares the same ftyp box
        (4, b"wide"),       # older QuickTime
        (4, b"mdat"),       # older QuickTime
        (4, b"moov"),       # older QuickTime
    ],
}

# Extensions we care about, mapped to their canonical type name
EXTENSION_TO_TYPE: dict[str, str] = {
    ".jpg":  "jpeg",
    ".jpeg": "jpeg",
    ".mp3":  "mp3",
    ".mp4":  "mp4",
    ".mov":  "mov",
}

HEADER_READ_BYTES = 12      # enough to cover all magic signatures above
PARTIAL_HASH_BYTES = 65536  # 64 KB for the first-pass hash
TRUNCATED_SIZE_RATIO = 0.90  # flag pair if smaller/larger <= this
LETTERS = string.ascii_lowercase  # a-z (25 slots, far more than needed)

# Matches resolution suffixes like _480p, _720, -1080p, -240 at end of stem
RESOLUTION_PATTERN = re.compile(
    r'[-_](240|360|480|720|1080|1024|1440|2160)p?$',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

def detect_type_by_magic(path: Path) -> str | None:
    """Return the detected media type from the file's magic bytes, or None."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(HEADER_READ_BYTES)
    except OSError:
        return None

    for media_type, signatures in MAGIC_SIGNATURES.items():
        for offset, magic in signatures:
            end = offset + len(magic)
            if len(header) >= end and header[offset:end] == magic:
                return media_type

    return None


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def hash_file(path: Path, partial: bool = False) -> str:
    """
    Compute SHA-256 of the file content.
    If partial=True, hash only the first PARTIAL_HASH_BYTES bytes (faster).
    Returns empty string on read failure.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            if partial:
                h.update(fh.read(PARTIAL_HASH_BYTES))
            else:
                while chunk := fh.read(PARTIAL_HASH_BYTES):
                    h.update(chunk)
    except OSError as exc:
        print(f"  [warn] Cannot read {path.name}: {exc}")
        return ""
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Scanning and duplicate detection
# ---------------------------------------------------------------------------

def scan_directory(directory: Path) -> list[Path]:
    """Return sorted list of supported media files in directory (non-recursive)."""
    return sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in EXTENSION_TO_TYPE
    )


def find_duplicate_groups(files: list[Path]) -> dict[str, list[Path]]:
    """
    Three-pass duplicate detection:
      Pass 1 — group by file size         (free, eliminates most non-duplicates)
      Pass 2 — partial hash of first 64KB  (fast, filters remaining candidates)
      Pass 3 — full SHA-256 hash           (definitive)

    Returns a dict mapping full_hash -> list[Path] for groups with 2+ files.
    """
    # --- Pass 1: group by size ---
    by_size: dict[int, list[Path]] = {}
    for f in files:
        by_size.setdefault(f.stat().st_size, []).append(f)

    size_candidates = [g for g in by_size.values() if len(g) > 1]
    if not size_candidates:
        return {}

    # --- Pass 2: partial hash ---
    by_partial: dict[str, list[Path]] = {}
    for group in size_candidates:
        for f in group:
            ph = hash_file(f, partial=True)
            if ph:
                by_partial.setdefault(ph, []).append(f)

    partial_candidates = [g for g in by_partial.values() if len(g) > 1]
    if not partial_candidates:
        return {}

    # --- Pass 3: full hash ---
    by_full: dict[str, list[Path]] = {}
    for group in partial_candidates:
        for f in group:
            fh = hash_file(f, partial=False)
            if fh:
                by_full.setdefault(fh, []).append(f)

    return {h: g for h, g in by_full.items() if len(g) > 1}


# ---------------------------------------------------------------------------
# Resolution variant detection
# ---------------------------------------------------------------------------

def _strip_resolution(stem: str) -> tuple[str, int | None]:
    """Strip resolution suffix from a filename stem.
    Returns (base_stem, resolution_int) or (stem, None) if no resolution found.
    """
    m = RESOLUTION_PATTERN.search(stem)
    if m:
        return stem[:m.start()], int(m.group(1))
    return stem, None


def _normalize_base(base: str) -> str:
    """Normalize a base name for fuzzy matching: lowercase, unify separators, strip trailing."""
    return base.lower().replace("-", "_").strip("_")


def find_resolution_variants(
    files: list[Path],
    confirmed_duplicate_paths: set[Path],
) -> dict[str, list[dict]]:
    """
    Group files by normalized base name after stripping resolution suffix.
    Normalization (lowercase, hyphen→underscore, strip trailing underscores) allows
    files like 'Whipped-Cream.mp4' and 'Whipped_Cream__720p.mp4' to be grouped.
    Files without a resolution suffix are included (as resolution=0) when their
    normalized name matches a group that has resolution-tagged files.
    Excludes confirmed hash duplicates.
    """
    # Pass 1: collect resolution-tagged files keyed by normalized base
    tagged: dict[str, list[dict]] = {}       # norm_base -> entries
    display_base: dict[str, str] = {}        # norm_base -> display label

    for f in files:
        if f in confirmed_duplicate_paths:
            continue
        base, resolution = _strip_resolution(f.stem)
        if resolution is None:
            continue
        norm = _normalize_base(base)
        stat = f.stat()
        tagged.setdefault(norm, []).append({
            "file": f.name,
            "resolution": resolution,
            "size_bytes": stat.st_size,
            "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
        display_base.setdefault(norm, base)

    # Pass 2: untagged files join a group if their normalized stem matches
    for f in files:
        if f in confirmed_duplicate_paths:
            continue
        _, resolution = _strip_resolution(f.stem)
        if resolution is not None:
            continue  # already handled in pass 1
        norm = _normalize_base(f.stem)
        if norm in tagged:
            stat = f.stat()
            tagged[norm].append({
                "file": f.name,
                "resolution": 0,   # untagged — displayed as "—" in UI
                "size_bytes": stat.st_size,
                "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    return {
        display_base[norm]: sorted(group, key=lambda e: e["resolution"])
        for norm, group in tagged.items()
        if len(group) > 1
    }


# ---------------------------------------------------------------------------
# Truncated download detection
# ---------------------------------------------------------------------------

def find_truncated_downloads(
    files: list[Path],
    confirmed_duplicate_paths: set[Path],
) -> list[dict]:
    """
    Detect probable truncated/incomplete downloads.

    Two files with the same partial hash (first 64KB) but meaningfully
    different sizes are likely an interrupted and a complete download of the
    same source.  The smaller file is flagged as the probable incomplete copy.

    A '(1)' / '(2)' suffix on either filename is recorded as an extra signal
    that the user intentionally re-downloaded the file.
    """
    candidates = [f for f in files if f not in confirmed_duplicate_paths]

    by_partial: dict[str, list[Path]] = {}
    for f in candidates:
        ph = hash_file(f, partial=True)
        if ph:
            by_partial.setdefault(ph, []).append(f)

    results = []
    for ph, group in by_partial.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda f: f.stat().st_size)
        seen: set[tuple[str, str]] = set()
        for i, smaller in enumerate(group_sorted):
            for larger in group_sorted[i + 1:]:
                s_size = smaller.stat().st_size
                l_size = larger.stat().st_size
                if l_size == 0:
                    continue
                ratio = s_size / l_size
                if ratio > TRUNCATED_SIZE_RATIO:
                    continue
                pair = (smaller.name, larger.name)
                if pair in seen:
                    continue
                seen.add(pair)
                likely_redownload = bool(
                    re.search(r'\s*\(\d+\)\s*$', smaller.stem) or
                    re.search(r'\s*\(\d+\)\s*$', larger.stem)
                )
                results.append({
                    "smaller_file": smaller.name,
                    "larger_file": larger.name,
                    "smaller_size_bytes": s_size,
                    "larger_size_bytes": l_size,
                    "size_ratio": round(ratio, 3),
                    "partial_hash": ph[:12],
                    "likely_redownload": likely_redownload,
                    "smaller_modified": datetime.fromtimestamp(smaller.stat().st_mtime).isoformat(),
                    "larger_modified": datetime.fromtimestamp(larger.stat().st_mtime).isoformat(),
                })

    return results


# ---------------------------------------------------------------------------
# Renaming
# ---------------------------------------------------------------------------

def _safe_rename_target(path: Path, new_name: str) -> Path:
    """
    Return a rename target that does not collide with an existing file.
    If Dup001a_foo.jpg already exists, appends _2, _3, etc. to the stem.
    """
    candidate = path.parent / new_name
    if not candidate.exists():
        return candidate

    base_path = path.parent / new_name
    stem = base_path.stem
    suffix = base_path.suffix
    counter = 2
    while True:
        candidate = path.parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def rename_duplicate_groups(
    duplicate_groups: dict[str, list[Path]],
    dry_run: bool,
) -> list[dict]:
    """
    Rename every file in every duplicate group using the scheme:
        Dup{group:03d}{letter}_{original_filename}

    Files within each group are ordered oldest-first by modification time.
    The oldest file receives letter 'a', next oldest 'b', and so on.

    Returns a list of manifest entries describing what was (or would be) done.
    """
    entries = []

    for group_num, (file_hash, group) in enumerate(duplicate_groups.items(), start=1):
        # Sort oldest-first by modification time, fall back to alphabetical on ties
        ordered = sorted(group, key=lambda p: (p.stat().st_mtime, p.name.lower()))

        for letter_idx, f in enumerate(ordered):
            letter = LETTERS[letter_idx]
            new_name = f"Dup{group_num:03d}{letter}_{f.name}"
            target = _safe_rename_target(f, new_name)

            mtime = datetime.fromtimestamp(f.stat().st_mtime).isoformat()
            entry = {
                "hash": file_hash,
                "group": group_num,
                "letter": letter,
                "original_name": f.name,
                "renamed_to": target.name,
                "modified_time": mtime,
                "size_bytes": f.stat().st_size,
                "status": None,
            }

            if dry_run:
                entry["status"] = "dry_run"
            else:
                try:
                    f.rename(target)
                    entry["status"] = "renamed"
                    print(f"  [Dup{group_num:03d}{letter}]  {f.name}  ->  {target.name}")
                except OSError as exc:
                    entry["status"] = "error"
                    entry["error"] = str(exc)
                    print(f"  [error]      {f.name}: {exc}")

            entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def write_log(
    log_path: Path,
    directory: Path,
    duplicate_groups: dict[str, list[Path]],
    rename_entries: list[dict],
    type_mismatches: list[dict],
    resolution_variants: dict[str, list[dict]],
    truncated_downloads: list[dict],
    total_files_scanned: int,
    dry_run: bool = False,
) -> None:
    """Write a human-readable duplicate rename plan to a log file."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = "-" * 60

    with open(log_path, "w", encoding="utf-8") as lf:
        mode_label = " (DRY RUN)" if dry_run else ""
        lf.write(f"File Organizer — Duplicate Scan Log{mode_label}\n")
        lf.write(f"Generated : {now}\n")
        lf.write(f"Directory : {directory}\n")
        unreadable_count = sum(1 for m in type_mismatches if m["detected_type"] is None)
        mismatch_count = sum(1 for m in type_mismatches if m["detected_type"] is not None)
        lf.write(f"Files scanned         : {total_files_scanned}\n")
        lf.write(f"Duplicate groups      : {len(duplicate_groups)}\n")
        lf.write(f"Files to rename       : {sum(len(g) for g in duplicate_groups.values())}\n")
        lf.write(f"Resolution var. groups: {len(resolution_variants)}\n")
        lf.write(f"Truncated downloads   : {len(truncated_downloads)}\n")
        lf.write(f"Unreadable files      : {unreadable_count}\n")
        lf.write(f"Type mismatches       : {mismatch_count}\n")

        unreadable = [m for m in type_mismatches if m["detected_type"] is None]
        mismatched = [m for m in type_mismatches if m["detected_type"] is not None]

        if unreadable:
            lf.write(f"\n{separator}\n")
            lf.write(f"UNREADABLE FILES ({len(unreadable)})\n")
            lf.write(f"{separator}\n")
            lf.write("These files could not be identified from their header.\n")
            lf.write("They may be corrupt, partially downloaded, or an unsupported format.\n\n")
            for m in unreadable:
                lf.write(f"  {m['file']}\n")
                lf.write(f"    Extension : {m['extension_type']}\n")

        if mismatched:
            lf.write(f"\n{separator}\n")
            lf.write(f"TYPE MISMATCHES ({len(mismatched)})\n")
            lf.write(f"{separator}\n")
            lf.write("These files have an extension that does not match their actual content.\n\n")
            for m in mismatched:
                lf.write(f"  {m['file']}\n")
                lf.write(f"    Extension says : {m['extension_type']}\n")
                lf.write(f"    Header says    : {m['detected_type']}\n")

        if duplicate_groups:
            lf.write(f"\n{separator}\n")
            lf.write("DUPLICATE GROUPS — PROPOSED RENAMES\n")
            lf.write(f"{separator}\n")

            # Group entries by group number for clean display
            by_group: dict[int, list[dict]] = {}
            for entry in rename_entries:
                by_group.setdefault(entry["group"], []).append(entry)

            for group_num, entries in sorted(by_group.items()):
                file_hash = entries[0]["hash"]
                lf.write(f"\n  Group Dup{group_num:03d}  [{file_hash[:12]}...]  ({len(entries)} files)\n")
                for e in entries:
                    mtime = e["modified_time"][:16].replace("T", " ")
                    size_kb = e["size_bytes"] / 1024
                    lf.write(f"    [{e['letter']}]  {e['original_name']}\n")
                    lf.write(f"         -> {e['renamed_to']}\n")
                    lf.write(f"            ({size_kb:.1f} KB, modified {mtime})\n")
        else:
            lf.write(f"\n{separator}\n")
            lf.write("No duplicates found.\n")

        if truncated_downloads:
            lf.write(f"\n{separator}\n")
            lf.write(f"PROBABLE TRUNCATED DOWNLOADS ({len(truncated_downloads)} pair(s))\n")
            lf.write(f"{separator}\n")
            lf.write("These file pairs share identical opening content (same 64KB partial hash)\n")
            lf.write("but differ significantly in size — the smaller is likely an interrupted download.\n\n")
            for t in truncated_downloads:
                s_mb = t["smaller_size_bytes"] / (1024 * 1024)
                l_mb = t["larger_size_bytes"] / (1024 * 1024)
                pct = int(t["size_ratio"] * 100)
                flag = "  <- LIKELY RE-DOWNLOAD" if t["likely_redownload"] else ""
                lf.write(f"  Smaller ({s_mb:.1f} MB, {pct}% of larger): {t['smaller_file']}{flag}\n")
                lf.write(f"  Larger  ({l_mb:.1f} MB):                  {t['larger_file']}\n")
                lf.write(f"  Partial hash: {t['partial_hash']}...\n\n")

        if resolution_variants:
            lf.write(f"\n{separator}\n")
            lf.write(f"RESOLUTION VARIANTS ({len(resolution_variants)} group(s))\n")
            lf.write(f"{separator}\n")
            lf.write("These files share the same base name at different resolutions.\n")
            lf.write("They are NOT confirmed duplicates — content differs by quality.\n")
            lf.write("Consider keeping only the highest resolution version.\n")
            for base, group in sorted(resolution_variants.items()):
                highest_res = max(e["resolution"] for e in group)
                lf.write(f"\n  Base: {base}\n")
                for e in group:
                    size_mb = e["size_bytes"] / (1024 * 1024)
                    mtime = e["modified_time"][:16].replace("T", " ")
                    marker = "  <- keep (highest resolution)" if e["resolution"] == highest_res else ""
                    lf.write(f"    {e['file']}\n")
                    lf.write(f"        {e['resolution']}p  |  {size_mb:.1f} MB  |  modified {mtime}{marker}\n")

        lf.write(f"\n{separator}\n")
        lf.write("End of log.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan a directory for duplicate media files and rename them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scan.py --dir ~/Downloads/media
  python scan.py --dir ~/Downloads/media --dry-run
        """,
    )
    parser.add_argument("--dir",     required=True,       help="Directory to scan")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("--log",     metavar="FILE",      help="Override log file path (default: 000_Scanned.log in the scanned directory)")
    args = parser.parse_args()

    directory = Path(args.dir).resolve()
    if not directory.is_dir():
        print(f"Error: '{args.dir}' is not a valid directory.")
        sys.exit(1)

    separator = "-" * 60
    print(separator)
    print("File Organizer — Phase 1: Duplicate Scanner")
    print(separator)
    print(f"Directory : {directory}")
    print(f"Scheme    : Dup{{group:03d}}{{letter}}_{{filename}}")
    if args.dry_run:
        print("Mode      : DRY RUN (no changes will be made)")
    print(separator)

    # --- Scan ---
    print(f"\nScanning for media files...")
    files = scan_directory(directory)
    print(f"  Found {len(files)} media file(s).")

    if not files:
        print("\nNothing to do.")
        sys.exit(0)

    # --- Magic byte verification ---
    print("\nVerifying file types via magic bytes...")
    type_mismatches: list[dict] = []
    for f in files:
        expected_type = EXTENSION_TO_TYPE[f.suffix.lower()]
        detected_type = detect_type_by_magic(f)
        if detected_type is None:
            print(f"  [unknown]  {f.name}: could not determine type from header")
            type_mismatches.append({
                "file": f.name,
                "extension_type": expected_type,
                "detected_type": None,
                "note": "unreadable or unknown header",
            })
        elif detected_type != expected_type:
            print(f"  [mismatch] {f.name}: extension says '{expected_type}', header says '{detected_type}'")
            type_mismatches.append({
                "file": f.name,
                "extension_type": expected_type,
                "detected_type": detected_type,
                "note": "extension does not match file content",
            })

    if not type_mismatches:
        print("  All file types match their extensions.")

    # --- Duplicate detection ---
    print("\nDetecting duplicates...")
    duplicate_groups = find_duplicate_groups(files)

    total_groups = len(duplicate_groups)
    total_files_renamed = sum(len(g) for g in duplicate_groups.values())
    print(f"  Found {total_groups} duplicate group(s) — {total_files_renamed} file(s) will be renamed.")

    if duplicate_groups:
        print()
        for group_num, (file_hash, group) in enumerate(duplicate_groups.items(), start=1):
            ordered = sorted(group, key=lambda p: (p.stat().st_mtime, p.name.lower()))
            print(f"  Group Dup{group_num:03d}  [{file_hash[:12]}...]  ({len(group)} files)")
            for letter_idx, f in enumerate(ordered):
                letter = LETTERS[letter_idx]
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                size_kb = f.stat().st_size / 1024
                print(f"    [{letter}]  {f.name}  ({size_kb:.1f} KB, modified {mtime})")

    # --- Resolution variant detection ---
    print("\nChecking for resolution variants...")
    confirmed_duplicate_paths: set[Path] = {
        f for group in duplicate_groups.values() for f in group
    }
    resolution_variants = find_resolution_variants(files, confirmed_duplicate_paths)
    if resolution_variants:
        total_variant_files = sum(len(g) for g in resolution_variants.values())
        print(f"  Found {len(resolution_variants)} resolution variant group(s) — {total_variant_files} file(s) flagged for review.")
        for base, group in sorted(resolution_variants.items()):
            resolutions = ", ".join(str(e["resolution"]) for e in group)
            print(f"    {base}  [{resolutions}]")
    else:
        print("  No resolution variants found.")

    # --- Truncated download detection ---
    print("\nChecking for truncated/incomplete downloads...")
    truncated_downloads = find_truncated_downloads(files, confirmed_duplicate_paths)
    if truncated_downloads:
        print(f"  Found {len(truncated_downloads)} probable truncated download pair(s).")
        for t in truncated_downloads:
            s_mb = t["smaller_size_bytes"] / (1024 * 1024)
            l_mb = t["larger_size_bytes"] / (1024 * 1024)
            flag = "  [re-download]" if t["likely_redownload"] else ""
            print(f"    {t['smaller_file']} ({s_mb:.1f} MB){flag}")
            print(f"      vs {t['larger_file']} ({l_mb:.1f} MB)")
    else:
        print("  No truncated downloads detected.")

    # --- Rename ---
    rename_entries: list[dict] = []
    if duplicate_groups:
        if args.dry_run:
            print(f"\n[DRY RUN] Would rename {total_files_renamed} file(s). Run without --dry-run to apply.")
            rename_entries = rename_duplicate_groups(duplicate_groups, dry_run=True)
        else:
            print(f"\nRenaming {total_files_renamed} file(s)...")
            rename_entries = rename_duplicate_groups(duplicate_groups, dry_run=False)

    # --- Manifest ---
    manifest = {
        "scan_time": datetime.now().isoformat(),
        "directory": str(directory),
        "dry_run": args.dry_run,
        "stats": {
            "total_files_scanned": len(files),
            "duplicate_groups_found": total_groups,
            "total_files_renamed": total_files_renamed,
            "type_mismatches_found": len(type_mismatches),
            "truncated_downloads_found": len(truncated_downloads),
        },
        "type_mismatches": type_mismatches,
        "duplicates": rename_entries,
        "resolution_variants": {
            base: group for base, group in resolution_variants.items()
        },
        "truncated_downloads": truncated_downloads,
    }

    manifest_path = directory / "manifest.json"
    try:
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2)
        print(f"\nManifest saved: {manifest_path}")
    except OSError as exc:
        print(f"\n[warn] Could not write manifest: {exc}")

    log_path = Path(args.log) if args.log else directory / "000_Scanned.log"
    try:
        write_log(log_path, directory, duplicate_groups, rename_entries, type_mismatches, resolution_variants, truncated_downloads, len(files), dry_run=args.dry_run)
        print(f"Log saved:      {log_path}")
    except OSError as exc:
        print(f"\n[warn] Could not write log: {exc}")

    print(separator)
    if args.dry_run:
        print("Dry run complete. No files were modified.")
    else:
        renamed_count = sum(1 for e in rename_entries if e["status"] == "renamed")
        error_count = sum(1 for e in rename_entries if e["status"] == "error")
        print(f"Done. {renamed_count} file(s) renamed across {total_groups} group(s).", end="")
        if error_count:
            print(f"  {error_count} error(s) — see manifest for details.", end="")
        print()
    print(separator)


if __name__ == "__main__":
    main()
