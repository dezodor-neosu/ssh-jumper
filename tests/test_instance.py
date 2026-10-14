import subprocess
import sys
import uuid

from ssh_jumper.instance import SingleInstance


def test_second_process_exits_and_lock_recovers_after_crash():
    name = "Local\\SSH-Jumper-test-" + uuid.uuid4().hex
    code = """
import os, sys
from ssh_jumper.instance import SingleInstance
guard = SingleInstance(sys.argv[1])
if not guard.acquire():
    sys.exit(23)
print('ready', flush=True)
sys.stdin.readline()
os._exit(7)
"""
    first = subprocess.Popen([sys.executable, "-c", code, name], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    guard = SingleInstance(name)
    try:
        assert first.stdout.readline().strip() == "ready"
        second = subprocess.run([sys.executable, "-c", code, name], timeout=5)
        assert second.returncode == 23
        assert first.poll() is None
        assert not guard.acquire()
        first.communicate("crash\n", timeout=5)
        assert first.returncode == 7
        assert guard.acquire()  # No stale lock file after forced termination.
    finally:
        guard.close()
        if first.poll() is None:
            first.communicate("crash\n", timeout=5)
        first.wait(timeout=5)


def test_main_exits_before_loading_ui_when_already_running(monkeypatch):
    import main
    monkeypatch.setattr(main, "SingleInstance", lambda: type("Busy", (), {"acquire": lambda self: False})())
    assert main.main() == 0
