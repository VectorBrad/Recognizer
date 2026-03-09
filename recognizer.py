#!/usr/bin/env python3
"""
File Organizer — Recognizer

Single-server entry point.  Serves the launcher UI, runs scan.py on the
chosen directory, then serves the review UI — all on one port.

Usage:
    python recognizer.py
    python recognizer.py --port 5000
"""

import argparse
import json
import shutil
import threading
import webbrowser
import subprocess
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file

BASE_DIR = Path(__file__).parent
SCAN_SCRIPT    = BASE_DIR / "scan.py"
CLEANUP_SCRIPT = BASE_DIR / "cleanup.py"

STAGING_DIR_NAME   = "000_To_Delete"
THUMB_CACHE_DIR_NAME = ".thumbcache"

# Active directory — set when the user launches a scan
DIRECTORY: Path | None = None

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    if DIRECTORY is None:
        return {}
    manifest_path = DIRECTORY / "manifest.json"
    if not manifest_path.exists():
        return {}
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def get_staging_dir() -> Path:
    return DIRECTORY / STAGING_DIR_NAME


def get_staged_filenames() -> set[str]:
    staging_dir = get_staging_dir()
    if not staging_dir.exists():
        return set()
    return {f.name for f in staging_dir.iterdir() if f.is_file()}


def _validate_filename(filename: str) -> Path | None:
    """Return resolved Path only if filename is safe (no path traversal)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    filepath = (DIRECTORY / filename).resolve()
    if filepath.parent != DIRECTORY.resolve():
        return None
    return filepath


def _validate_staged_filename(filename: str) -> Path | None:
    """Return resolved Path inside staging dir if safe, else None."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    filepath = (get_staging_dir() / filename).resolve()
    if filepath.parent != get_staging_dir().resolve():
        return None
    return filepath


def get_or_create_mp4_thumbnail(filepath: Path) -> Path | None:
    """Extract a frame from an MP4/MOV and cache it as JPEG. Returns path or None."""
    thumb_dir = DIRECTORY / THUMB_CACHE_DIR_NAME
    thumb_dir.mkdir(exist_ok=True)
    thumb_path = thumb_dir / (filepath.stem + "_thumb.jpg")

    if thumb_path.exists():
        return thumb_path

    # Try ffmpeg first (cross-platform, handles MP4 and MOV)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(filepath),
                "-ss", "00:00:05",
                "-vframes", "1",
                "-vf", "scale=320:-1",
                "-y", str(thumb_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and thumb_path.exists():
            return thumb_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fall back to opencv-python
    try:
        import cv2
        cap = cv2.VideoCapture(str(filepath))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        target_frame = int(fps * 5)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(target_frame, max(0, total_frames - 1)))
        ret, frame = cap.read()
        cap.release()
        if ret:
            h, w = frame.shape[:2]
            scale = 320 / w if w > 320 else 1.0
            if scale < 1.0:
                frame = cv2.resize(frame, (320, int(h * scale)))
            cv2.imwrite(str(thumb_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if thumb_path.exists():
                return thumb_path
    except Exception:
        pass

    return None


def _no_preview_svg(label: str = "No preview") -> Response:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">
        <rect width="320" height="180" fill="#2a2a2a"/>
        <text x="160" y="85" font-family="sans-serif" font-size="13"
              fill="#666" text-anchor="middle">&#9654;</text>
        <text x="160" y="108" font-family="sans-serif" font-size="12"
              fill="#666" text-anchor="middle">{label}</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


# ---------------------------------------------------------------------------
# Launcher routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def launcher():
    return render_template("launcher.html", directory="", error=None)


@app.route("/launch", methods=["POST"])
def launch():
    global DIRECTORY

    directory = request.form.get("directory", "").strip()
    if not directory:
        return render_template("launcher.html", directory="", error="Please enter a directory path.")

    path = Path(directory).resolve()
    if not path.is_dir():
        return render_template("launcher.html", directory=directory,
                               error=f"Directory not found: {directory}")

    scan_result = subprocess.run(
        [sys.executable, str(SCAN_SCRIPT), "--dir", str(path)],
        capture_output=True, text=True
    )
    if scan_result.returncode != 0:
        err = scan_result.stderr.strip() or scan_result.stdout.strip() or "scan.py failed"
        return render_template("launcher.html", directory=directory, error=err)

    DIRECTORY = path
    return redirect("/review")


# ---------------------------------------------------------------------------
# Review routes
# ---------------------------------------------------------------------------

@app.route("/review")
def review():
    if DIRECTORY is None:
        return redirect("/")

    manifest = load_manifest()
    staged = get_staged_filenames()

    dup_groups: dict[int, list[dict]] = {}
    for entry in manifest.get("duplicates", []):
        dup_groups.setdefault(entry["group"], []).append(entry)

    all_issues = manifest.get("type_mismatches", [])
    unreadable = [m for m in all_issues if m.get("detected_type") is None]
    mismatched = [m for m in all_issues if m.get("detected_type") is not None]

    return render_template(
        "index.html",
        manifest=manifest,
        dup_groups=sorted(dup_groups.items()),
        resolution_variants=sorted(manifest.get("resolution_variants", {}).items()),
        truncated_downloads=manifest.get("truncated_downloads", []),
        unreadable=unreadable,
        mismatched=mismatched,
        staged=staged,
        directory=str(DIRECTORY),
        staging_dir_name=STAGING_DIR_NAME,
    )


@app.route("/thumb/<filename>")
def thumbnail(filename: str):
    if "/" in filename or ".." in filename:
        return "", 404
    if DIRECTORY is None:
        return _no_preview_svg("No directory")

    filepath = DIRECTORY / filename
    if not filepath.exists():
        filepath = get_staging_dir() / filename
    if not filepath.exists():
        return _no_preview_svg("File not found")

    ext = filepath.suffix.lower()

    if ext in (".jpg", ".jpeg"):
        return send_file(filepath, mimetype="image/jpeg")

    if ext in (".mp4", ".mov"):
        thumb = get_or_create_mp4_thumbnail(filepath)
        if thumb:
            return send_file(thumb, mimetype="image/jpeg")
        return _no_preview_svg("No preview")

    return _no_preview_svg("No preview")


@app.route("/video/<filename>")
def video(filename: str):
    if "/" in filename or ".." in filename:
        return "", 404
    if DIRECTORY is None:
        return "", 404

    filepath = DIRECTORY / filename
    if not filepath.exists():
        filepath = get_staging_dir() / filename
    if not filepath.exists():
        return "", 404

    ext = filepath.suffix.lower()
    mimetype = "video/quicktime" if ext == ".mov" else "video/mp4"
    return send_file(filepath, mimetype=mimetype, conditional=True)


@app.route("/file")
def serve_file():
    filename = request.args.get("f", "")
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return "", 404
    if DIRECTORY is None:
        return "", 404

    filepath = DIRECTORY / filename
    if not filepath.exists():
        filepath = get_staging_dir() / filename
    if not filepath.exists():
        return "", 404

    ext = filepath.suffix.lower()
    mimetypes = {
        ".mp3":  "audio/mpeg",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    mimetype = mimetypes.get(ext, "application/octet-stream")
    return send_file(filepath, mimetype=mimetype, conditional=True)


@app.route("/api/stage", methods=["POST"])
def stage():
    filename = (request.get_json() or {}).get("filename", "")
    filepath = _validate_filename(filename)
    if not filepath or not filepath.exists():
        return jsonify({"error": "File not found or invalid path"}), 400

    staging_dir = get_staging_dir()
    staging_dir.mkdir(exist_ok=True)
    try:
        shutil.move(str(filepath), str(staging_dir / filename))
        return jsonify({"success": True, "staged_count": len(get_staged_filenames())})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/unstage", methods=["POST"])
def unstage():
    filename = (request.get_json() or {}).get("filename", "")
    staged_path = _validate_staged_filename(filename)
    if not staged_path or not staged_path.exists():
        return jsonify({"error": "File not found in staging"}), 400

    try:
        shutil.move(str(staged_path), str(DIRECTORY / filename))
        return jsonify({"success": True, "staged_count": len(get_staged_filenames())})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/staged")
def api_staged():
    if DIRECTORY is None:
        return jsonify([])
    staging_dir = get_staging_dir()
    files = []
    if staging_dir.exists():
        for f in sorted(staging_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                files.append({"filename": f.name, "size_bytes": stat.st_size})
    return jsonify(files)


@app.route("/api/rename", methods=["POST"])
def rename_file():
    data = request.get_json() or {}
    old_name = data.get("old_filename", "")
    new_name = data.get("new_filename", "").strip()

    if not new_name:
        return jsonify({"error": "New filename cannot be empty"}), 400

    old_path = _validate_filename(old_name)
    if not old_path or not old_path.exists():
        return jsonify({"error": "File not found"}), 400

    new_path = _validate_filename(new_name)
    if not new_path:
        return jsonify({"error": "Invalid filename"}), 400
    if new_path.exists():
        return jsonify({"error": "A file with that name already exists"}), 400

    try:
        old_path.rename(new_path)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    if DIRECTORY is None:
        return jsonify({"error": "No directory loaded"}), 400
    result = subprocess.run(
        [sys.executable, str(CLEANUP_SCRIPT), "--dir", str(DIRECTORY)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "cleanup.py failed"
        return jsonify({"error": err}), 500
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="File Organizer — Recognizer")
    parser.add_argument("--port", type=int, default=5000, help="Port to serve on (default: 5000)")
    args = parser.parse_args()

    print("-" * 50)
    print("Recognizer")
    print("-" * 50)
    print(f"Open: http://localhost:{args.port}")
    print("Press Ctrl+C to stop.\n")

    url = f"http://localhost:{args.port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
