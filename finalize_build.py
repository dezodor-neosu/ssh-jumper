"""Normalize Windows runtime DLLs and verify the packaged GUI starts."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import pefile
import PySide6
from ssh_jumper.instance import SingleInstance

root = Path(__file__).parent
bundle = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "dist" / "SSH-Jumper"
internal = bundle / "_internal"
qt = Path(PySide6.__file__).parent
# Python's older runtime must not take precedence over Qt's newer one.
for library in qt.glob("vcruntime140*.dll"):
    shutil.copy2(library, internal / library.name)

# Qt uses Windows' unversioned ICU API. A different ICU found on PATH (e.g.
# Poppler/Conda's version-suffixed API) must not shadow the system DLL.
icu = internal / "icuuc.dll"
if icu.exists():
    with pefile.PE(str(icu), fast_load=True) as pe:
        pe.parse_data_directories(directories=[0])
        exports = {symbol.name for symbol in pe.DIRECTORY_ENTRY_EXPORT.symbols}
    if b"ucnv_open" not in exports:
        icu.unlink()

shutil.copy2(root / "README.md", bundle / "README.md")
guard = SingleInstance()
existing_instance = not guard.acquire()
guard.close()
with tempfile.TemporaryDirectory() as data:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen([str(bundle / "SSH-Jumper.exe")],
        env=dict(os.environ, APPDATA=data), startupinfo=startup, stderr=subprocess.PIPE)
    try:
        if existing_instance:
            if process.wait(timeout=10) != 0:
                raise RuntimeError("Packaged duplicate instance did not exit cleanly")
            print("Packaged duplicate-instance check passed; full startup skipped because SSH Jumper is already running")
            sys.exit(0)
        time.sleep(4)
        if process.poll() is not None:
            raise RuntimeError(f"Packaged app failed: {process.stderr.read()!r}")
        duplicate = subprocess.Popen([str(bundle / "SSH-Jumper.exe")],
            env=dict(os.environ, APPDATA=data), startupinfo=startup)
        try:
            if duplicate.wait(timeout=10) != 0:
                raise RuntimeError("Duplicate instance did not exit cleanly")
            if process.poll() is not None:
                raise RuntimeError("Duplicate launch stopped the original instance")
        finally:
            if duplicate.poll() is None:
                duplicate.terminate()
                duplicate.wait(timeout=10)
        print("Packaged executable startup and duplicate-instance check passed")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
