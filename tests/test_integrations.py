from pathlib import Path
from types import SimpleNamespace
import pytest

from ssh_jumper.integrations import ClientLauncher, IntegrationSettings, SettingsStore, SecretFiles, client_arguments
from ssh_jumper.models import Forward, SSHAuth


def client(tmp_path, features=b""):
    path = tmp_path / "client.exe"
    path.write_bytes(features)
    return str(path)


def test_settings_round_trip_and_invalid_path_preserves_existing(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = IntegrationSettings(terminal="KiTTY", kitty_path=client(tmp_path))
    store.save(settings)
    assert store.load() == settings
    with pytest.raises(ValueError):
        store.save(IntegrationSettings(putty_path="missing.exe"))
    assert store.load() == settings


@pytest.mark.parametrize("kind", ["terminal", "winscp"])
def test_saved_password_handoff_never_in_command_line(tmp_path, monkeypatch, kind):
    path = client(tmp_path, b"-pwfile passwordsfromfiles")
    secret = "secret-Ж & $(no-shell)"
    forward = Forward(12222, "remote.internal", 22, ssh=SSHAuth("alice", "Password", secret))
    files = SecretFiles(tmp_path / "secrets")
    launched = []
    def popen(args, **kwargs):
        assert secret not in " ".join(args)
        assert "remote.internal" not in " ".join(args)
        assert kwargs["shell"] is False
        handoff = next(files.root.glob("launch-*/credential.txt"))
        assert handoff.read_text(encoding="utf-8") == secret
        launched.append(args)
        return SimpleNamespace(wait=lambda timeout: None)
    monkeypatch.setattr("ssh_jumper.integrations.subprocess.Popen", popen)
    monkeypatch.setattr(files, "expire", lambda folder, process: files.remove(folder))
    ClientLauncher(files).launch(kind, IntegrationSettings(putty_path=path, winscp_path=path), forward)
    assert launched and not list(files.root.iterdir())


def test_failed_launch_removes_handoff(tmp_path, monkeypatch):
    path = client(tmp_path, b"-pwfile")
    files = SecretFiles(tmp_path / "secrets")
    def fail(*args, **kwargs):
        raise OSError("Cannot start")
    monkeypatch.setattr("ssh_jumper.integrations.subprocess.Popen", fail)
    with pytest.raises(OSError):
        ClientLauncher(files).launch("terminal", IntegrationSettings(putty_path=path),
                                    Forward(ssh=SSHAuth("alice", "Password", "secret")))
    assert not list(files.root.iterdir())


def test_old_clients_prompt_and_do_not_expose_password(tmp_path):
    path = client(tmp_path)
    for kind in ("terminal", "winscp"):
        args, notes = client_arguments(kind, path, Forward(ssh=SSHAuth("alice", "Password", "secret")), "handoff")
        assert "secret" not in " ".join(args) and "handoff" not in " ".join(args)
        assert any("prompt" in note for note in notes)


def test_agent_compatibility_and_key_passphrase(tmp_path):
    path = client(tmp_path, b"passwordsfromfiles AuthAgent")
    forward = Forward(ssh=SSHAuth("alice", "OpenSSH agent"))
    with pytest.raises(ValueError, match="Pageant"):
        client_arguments("terminal", path, forward)
    args, _ = client_arguments("winscp", path, forward)
    assert "Interface\\AuthAgent=1" in args and "TryAgent=1" in args
    key = tmp_path / "key.ppk"
    key.write_text("PuTTY-User-Key-File-3: ssh-ed25519\n")
    forward.ssh = SSHAuth("alice", "Private key file", "passphrase", str(key))
    args, _ = client_arguments("winscp", path, forward, "protected-file")
    assert "/passphrase=protected-file" in args and "TryAgent=0" in args
    assert "/privatekey=" + str(key.resolve()) in args
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(ValueError, match="PuTTYgen"):
        client_arguments("winscp", path, forward)


def test_secret_directory_has_restricted_windows_acl(tmp_path):
    import ctypes
    from ctypes import wintypes
    files = SecretFiles(tmp_path / "secrets")
    folder = files.create()
    try:
        # Python 3.13's 0700 directory must not inherit broad parent access.
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        assert security.GetNamedSecurityInfoW(str(folder), 1, 4, None, None, None, None, ctypes.byref(descriptor)) == 0
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            assert security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
            assert control.value & 0x1000  # SE_DACL_PROTECTED
        finally:
            ctypes.WinDLL("kernel32").LocalFree(descriptor)
    finally:
        files.close()
