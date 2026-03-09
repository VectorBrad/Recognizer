#!/usr/bin/env python3
"""
Recognizer — Installer

Copies the project files to a chosen location and writes a recognizer.bat
(Windows) or start.sh (Mac/Linux) pointing to that location.

Usage:
    python installer.py
"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).parent

FILES_TO_COPY = [
    "recognizer.py",
    "scan.py",
    "cleanup.py",
    "installer.py",
    "requirements.txt",
    "README.md",
]
TEMPLATE_DIR = "templates"


def prompt(message: str, default: str = "") -> str:
    if default:
        display = f"{message} [{default}]: "
    else:
        display = f"{message}: "
    value = input(display).strip()
    return value or default


def main():
    print()
    print("=" * 50)
    print("  Recognizer - Installer")
    print("=" * 50)
    print()

    is_windows = platform.system() == "Windows"
    home = Path.home()

    print("Press Enter to accept the default, or type a new path.")
    print()

    # --- Where to install the project files ---
    default_install = str(home / "Recognizer")
    while True:
        install_dir = Path(prompt("Install project files to", default_install)).expanduser().resolve()
        print(f"  -> {install_dir}")
        ok = input("     Use this path? [Y/n]: ").strip().lower()
        if not ok or ok == "y":
            break
        print()

    print()

    # --- Where to put the launcher shortcut ---
    if is_windows:
        default_launcher = str(home)
        launcher_name = "recognizer.bat"
    else:
        default_launcher = str(home / "bin")
        launcher_name = "start.sh"

    while True:
        launcher_dir = Path(prompt(f"Place {launcher_name} in", default_launcher)).expanduser().resolve()
        print(f"  -> {launcher_dir / launcher_name}")
        ok = input("     Use this path? [Y/n]: ").strip().lower()
        if not ok or ok == "y":
            break
        print()

    print()
    print(f"  Project files  ->  {install_dir}")
    print(f"  {launcher_name:14} ->  {launcher_dir / launcher_name}")
    print()
    confirm = input("Proceed? [Y/n]: ").strip().lower()
    if confirm and confirm != "y":
        print("Aborted.")
        sys.exit(0)

    # --- Copy project files ---
    install_dir.mkdir(parents=True, exist_ok=True)
    for filename in FILES_TO_COPY:
        src = SOURCE_DIR / filename
        dst = install_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  copied  {filename}")
        else:
            print(f"  [skip]  {filename} not found in source")

    # Copy templates folder
    src_templates = SOURCE_DIR / TEMPLATE_DIR
    dst_templates = install_dir / TEMPLATE_DIR
    if src_templates.exists():
        if dst_templates.exists():
            shutil.rmtree(dst_templates)
        shutil.copytree(src_templates, dst_templates)
        print(f"  copied  templates/")
    else:
        print(f"  [skip]  templates/ not found in source")

    # --- Write launcher ---
    launcher_dir.mkdir(parents=True, exist_ok=True)

    if is_windows:
        bat_path = launcher_dir / "recognizer.bat"
        bat_path.write_text(
            f'@echo off\r\n'
            f'python "{install_dir / "recognizer.py"}"\r\n',
            encoding="utf-8",
        )
        print(f"  wrote   {bat_path}")
    else:
        sh_path = launcher_dir / "start.sh"
        sh_path.write_text(
            f'#!/bin/bash\n'
            f'open http://localhost:5000 2>/dev/null || xdg-open http://localhost:5000 2>/dev/null &\n'
            f'python3 "{install_dir / "recognizer.py"}"\n',
            encoding="utf-8",
        )
        sh_path.chmod(sh_path.stat().st_mode | 0o111)  # make executable
        print(f"  wrote   {sh_path}")

    # --- Install dependencies if needed ---
    print()
    flask_installed = subprocess.run(
        [sys.executable, "-m", "pip", "show", "flask"],
        capture_output=True,
    ).returncode == 0

    if flask_installed:
        print("Flask already installed — skipping.")
    else:
        print("Flask not found — installing...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "flask"],
            text=True,
        )
        if result.returncode != 0:
            print("  [warn] pip install failed — run manually: pip install flask")
        else:
            print("  Flask installed.")

    print()
    print("Done.")
    print()
    if is_windows:
        print(f"Double-click recognizer.bat to launch.")
    else:
        print(f"Run ./start.sh to launch.")
    print()


if __name__ == "__main__":
    main()
