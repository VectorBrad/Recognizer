# Recognizer

A three-phase tool for reviewing and cleaning up duplicate and near-duplicate media files in a flat directory.

Supports: **MP3, MP4, MOV, JPEG** (`.mp3`, `.mp4`, `.mov`, `.jpg`, `.jpeg`)

---

## Prerequisites

- **Python 3.9+** must be installed and on your PATH
- **ffmpeg** is optional but recommended for video thumbnail generation. Without it the tool falls back to opencv-python (also optional). Without either, video thumbnails show a placeholder.

---

## Installation (Windows)

1. Double-click **`installer.bat`**
   - If prompted by Windows UAC, click **Yes**
2. When asked where to install project files, press Enter to accept the default (`C:\Users\<you>\Recognizer`) or type a custom path
3. When asked where to place `recognizer.bat`, press Enter to accept the default (`C:\Users\<you>`) or type a custom path
4. The installer copies all project files and installs Flask automatically

That's it. No manual pip commands needed.

---

## Usage

1. Double-click **`recognizer.bat`** (wherever you placed it during install)
2. Your browser opens automatically at `http://localhost:5000`
3. Enter the directory you want to scan and press **Enter**
4. Review flagged files in the web UI
5. Click **Cleanup** when done — you are returned to the launcher to process another directory

---

## Workflow

### Phase 1 — Scan

Runs automatically when you submit a directory. Scans all supported files directly in that folder (non-recursive) and:

- Detects **byte-for-byte duplicates** via a 3-pass hash (size → partial SHA-256 → full SHA-256), renames them with a `Dup###x_` prefix
- Detects **resolution variants** — files sharing a base name at different resolutions (`_480p`, `_720p`, etc.)
- Detects **truncated downloads** — files with identical opening content but significantly different sizes
- Flags **type mismatches** — files whose extension doesn't match their actual format
- Writes `manifest.json` and `000_Scanned.log` to the directory

### Phase 2 — Review

Web UI for inspecting scan results:

- **Duplicates** — view thumbnails side by side, stage copies for deletion
- **Resolution variants** — keep the highest resolution, stage the rest
- **Truncated downloads** — identify and stage incomplete files
- **Type mismatches / unreadable files** — inspect and decide
- **Staging** — files are moved to `000_To_Delete/` (not permanently deleted)

### Phase 3 — Cleanup

Triggered by the **Cleanup** button in the UI:

- Strips `Dup###x_` prefixes from files remaining in the main directory (restores original names)
- Deletes `manifest.json`, `000_Scanned.log`, and the `.thumbcache/` folder
- Leaves `000_To_Delete/` in place — delete it manually when you are satisfied

---

## File structure (installed)

```
Recognizer/
├── recognizer.py       # Web server (launcher + review UI)
├── scan.py             # Phase 1: duplicate detection and renaming
├── cleanup.py          # Phase 3: restore filenames, remove artefacts
├── installer.py        # Re-run to change install location
├── requirements.txt
├── README.md
└── templates/
    ├── launcher.html
    └── index.html      # Review UI

recognizer.bat          # Placed wherever you chose during install
```

---

## Notes

- Scanning is **non-recursive** — only files directly in the chosen directory are processed
- The `.thumbcache/` folder is created inside the scanned directory and removed by Cleanup
- The `000_To_Delete/` staging folder is intentionally left for manual review before permanent deletion
- Re-running `installer.bat` installs to an additional location — it does **not** remove the previous install. Delete the old folder manually if you no longer need it.
