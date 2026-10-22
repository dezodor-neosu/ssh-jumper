#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
.venv/bin/python -m PyInstaller --noconfirm --clean --windowed --onedir --name SSH-Jumper --collect-submodules paramiko --hidden-import keyring.backends.SecretService main.py
.venv/bin/python collect_licenses.py dist/SSH-Jumper
QT_QPA_PLATFORM=offscreen .venv/bin/python finalize_linux.py dist/SSH-Jumper
