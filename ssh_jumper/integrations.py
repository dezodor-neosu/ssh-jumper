"""External GUI clients connect only to an existing local forward."""
from dataclasses import asdict, dataclass
import atexit
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time


@dataclass
class IntegrationSettings:
    terminal: str = "PuTTY"
    putty_path: str = ""
    kitty_path: str = ""
    winscp_path: str = ""

    def terminal_path(self):
        return self.kitty_path if self.terminal == "KiTTY" else self.putty_path


def detect_settings():
    roots = [Path(os.environ.get(name, "C:/Program Files")) for name in ("ProgramFiles", "ProgramFiles(x86)")]
    def find(names, relative):
        candidates = [shutil.which(name) for name in names]
        candidates += [str(root / path) for root in roots for path in relative]
        return next((path for path in candidates if path and Path(path).is_file()), "")
    return IntegrationSettings(
        putty_path=find(["putty.exe"], ["PuTTY/putty.exe"]),
        kitty_path=find(["kitty.exe", "kitty_portable.exe"], ["KiTTY/kitty.exe", "PuTTY/kitty.exe", "PuTTY/kitty_portable.exe"]),
        winscp_path=find(["WinSCP.exe"], ["WinSCP/WinSCP.exe"]))


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return detect_settings()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("Unsupported app settings version")
        return IntegrationSettings(**data["integrations"])

    def save(self, settings):
        if settings.terminal not in ("PuTTY", "KiTTY"):
            raise ValueError("Select PuTTY or KiTTY as the terminal client.")
        for value in (settings.putty_path, settings.kitty_path, settings.winscp_path):
            if value:
                executable(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "integrations": asdict(settings)}, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def executable(value):
    path = Path(os.path.expandvars(value)).expanduser()
    if not value or not path.is_file() or path.suffix.lower() != ".exe":
        raise ValueError("Choose an existing .exe file in App settings.")
    return str(path.resolve())


def supports_option(path, option):
    # Feature detection also handles renamed PuTTY builds and KiTTY forks.
    data = Path(path).read_bytes()
    return option.encode("ascii") in data or option.encode("utf-16le") in data


class SecretFiles:
    """Short-lived handoff files in a Python 3.13 Windows ACL-protected directory.

    They are never used as persistent storage. Windows mkdir(mode=0o700)
    restricts access to the current user and administrators.
    """
    def __init__(self, root=None):
        self.root = Path(root or Path(os.environ["LOCALAPPDATA"]) / "SSH Jumper" / "launch-secrets")
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions = set()
        self.lock = threading.Lock()
        # Remove only our expired handoff files, never recurse into directories.
        for folder in self.root.glob("launch-*"):
            if folder.is_dir() and not folder.is_symlink() and time.time() - folder.stat().st_mtime > 300:
                self.remove(folder)
        atexit.register(self.close)

    def create(self):
        path = Path(tempfile.mkdtemp(prefix="launch-", dir=self.root))
        with self.lock:
            self.sessions.add(path)
        return path

    def write(self, folder, text):
        path = folder / "credential.txt"
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
        return str(path)

    def remove(self, folder):
        # No recursive deletion; restrict deletion to the single file we create.
        try:
            (folder / "credential.txt").unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            return
        with self.lock:
            self.sessions.discard(folder)

    def expire(self, folder, process):
        def cleanup():
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                pass
            finally:
                self.remove(folder)
        threading.Thread(target=cleanup, daemon=True).start()

    def close(self):
        with self.lock:
            sessions = list(self.sessions)
        for folder in sessions:
            self.remove(folder)


def client_arguments(kind, path, forward, secret_file=None):
    """Return arguments and user-visible compatibility notes; never a password."""
    forward.validate()
    auth = forward.ssh
    if auth is None:
        raise ValueError("Enable SSH authentication in the forward editor first.")
    notes = []
    if any("\x00" in value for value in (auth.username, auth.key_file)):
        raise ValueError("SSH fields cannot contain NUL characters.")
    key = ""
    if auth.auth == "Private key file":
        key_path = Path(os.path.expandvars(auth.key_file)).expanduser()
        if not key_path.is_file():
            raise ValueError("The endpoint private-key file does not exist.")
        with key_path.open("rb") as stream:
            is_ppk = stream.read(64).startswith(b"PuTTY-User-Key-File-")
        if not is_ppk:
            raise ValueError("PuTTY/KiTTY and WinSCP need a PuTTY .ppk key. Convert the key with PuTTYgen, or load it into Pageant and choose Pageant authentication.")
        key = str(key_path.resolve())
    agent = auth.auth in ("All local agents", "OpenSSH agent", "Pageant")
    if kind == "terminal":
        if auth.auth == "OpenSSH agent":
            raise ValueError("PuTTY/KiTTY use Pageant, not the Windows OpenSSH agent. Load this endpoint's key into Pageant and select Pageant authentication.")
        args = [path, "-ssh", "-P", str(forward.local_port), "-l", auth.username,
                "-agent" if agent else "-noagent", "-a"]
        if auth.auth == "All local agents":
            notes.append("This terminal uses Pageant for agent authentication.")
        if key:
            args += ["-i", key]
            if auth.password:
                notes.append("Enter the key passphrase in the terminal, or load the key into Pageant.")
        elif auth.auth == "Password" and auth.password:
            if supports_option(path, "-pwfile") and not any(c in auth.password for c in "\r\n\x00"):
                if secret_file:
                    args += ["-pwfile", secret_file]
            else:
                notes.append("This terminal will prompt for the password; secure password-file input requires PuTTY 0.77+ or a compatible KiTTY build.")
        # Fixed loopback destination is last, never a remote host or shell command.
        args += ["127.0.0.1"]
    elif kind == "winscp":
        args = [path, f"sftp://127.0.0.1:{forward.local_port}/", "/newinstance", "/ini=nul",
                f"/username={auth.username}", "/rawsettings", f"TryAgent={int(agent)}", "AgentFwd=0"]
        if auth.auth == "OpenSSH agent":
            if not supports_option(path, "AuthAgent"):
                raise ValueError("This WinSCP build does not support the Windows OpenSSH agent. Choose Pageant or configure a WinSCP build with OpenSSH-agent support.")
            args += ["/rawconfig", "Interface\\AuthAgent=1"]
        elif auth.auth == "All local agents":
            notes.append("WinSCP uses Pageant for this session. Select OpenSSH agent explicitly for a compatible WinSCP build.")
        if key:
            args += [f"/privatekey={key}"]
        if auth.password and auth.auth in ("Password", "Private key file"):
            if supports_option(path, "passwordsfromfiles"):
                if secret_file:
                    option = "passphrase" if key else "password"
                    args += ["/passwordsfromfiles", f"/{option}={secret_file}"]
            else:
                notes.append("This WinSCP build will prompt for credentials; update it to support password-file input.")
    else:
        raise ValueError("Unknown integration")
    return args, notes


class ClientLauncher:
    def __init__(self, secrets=None):
        self.secrets = secrets or SecretFiles()

    def launch(self, kind, settings, forward):
        path = executable(settings.terminal_path() if kind == "terminal" else settings.winscp_path)
        # First pass validates before creating any plaintext handoff file.
        args, notes = client_arguments(kind, path, forward)
        marker = "__SSH_JUMPER_SECRET_FILE__"
        with_secret, _ = client_arguments(kind, path, forward, marker)
        folder = None
        try:
            if any(marker in arg for arg in with_secret):
                folder = self.secrets.create()
                secret_file = self.secrets.write(folder, forward.ssh.password)
                args, _ = client_arguments(kind, path, forward, secret_file)
            # GUI apps are intentionally visible; argument lists avoid shell parsing.
            process = subprocess.Popen(args, cwd=str(Path(path).parent), shell=False)
        except Exception:
            if folder:
                self.secrets.remove(folder)
            raise
        if folder:
            self.secrets.expire(folder, process)
        return process, notes
