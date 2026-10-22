from contextlib import contextmanager
import os
import posixpath
from pathlib import Path
import socket
import stat
import threading
import subprocess
from types import SimpleNamespace

import paramiko
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from ssh_jumper.models import Forward, SSHAuth
from ssh_jumper.terminal import TerminalSession, TerminalWidget
from ssh_jumper.sftp_browser import SftpBrowser, SftpOperation, local_name, safe_remote
from test_tunnel import wait_for


class Filesystem(paramiko.SFTPServerInterface):
    def __init__(self, server, root):
        super().__init__(server)
        self.root = root.resolve()

    def canonicalize(self, path):
        return posixpath.normpath("/" + path.lstrip("/"))

    def path(self, name):
        path = (self.root / name.lstrip("/")).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError("Outside test root")
        return path

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def list_folder(self, path):
        try:
            result = []
            for child in self.path(path).iterdir():
                attr = paramiko.SFTPAttributes.from_stat(child.stat())
                attr.filename = child.name
                result.append(attr)
            return result
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        try:
            descriptor = os.open(self.path(path), flags | getattr(os, "O_BINARY", 0), 0o600)
            mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
            stream = os.fdopen(descriptor, mode)
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = handle.writefile = stream
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, old, new):
        try:
            if self.path(new).exists():
                return paramiko.SFTP_FAILURE
            self.path(old).rename(self.path(new))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            self.path(path).unlink()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class Server(paramiko.ServerInterface):
    def __init__(self):
        self.authentications = 0
        self.shell_data = bytearray()

    def check_auth_password(self, username, password):
        self.authentications += 1
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, channel, *args):
        return True

    def check_channel_shell_request(self, channel):
        def echo():
            try:
                while data := channel.recv(65536):
                    self.shell_data.extend(data)
                    channel.sendall(data)
            except (EOFError, OSError):
                pass
        threading.Thread(target=echo, daemon=True).start()
        return True


@contextmanager
def session_with_sftp(root):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    server = Server()
    transports, channels = [], []
    key = paramiko.RSAKey.generate(2048)
    def serve():
        try:
            connection, _ = listener.accept()
            transport = paramiko.Transport(connection)
            transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Filesystem, root=root)
            transport.start_server(server=server)
            while transport.is_active():
                channel = transport.accept(0.1)
                if channel:
                    channels.append(channel)
        except (EOFError, OSError, paramiko.SSHException):
            pass
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    session = TerminalSession(Forward(listener.getsockname()[1], "127.0.0.1", 22,
        ssh=SSHAuth("tester", "Password", "secret")), True)
    try:
        session.start()
        wait_for(lambda: session.status == "Connected")
        yield session, server
    finally:
        session.close()
        session.thread.join(3)
        for transport in transports:
            transport.close()
        listener.close()
        thread.join(3)


def operation(session, action, directory, paths=(), local_directory=None):
    job = SftpOperation(session.channel.get_transport(), action, directory, paths, local_directory)
    job.start()
    assert job.finished.wait(10)
    job.thread.join(1)
    return job


def test_real_sftp_navigation_upload_download_share_authentication(tmp_path):
    root = tmp_path / "remote"
    (root / "sub dir").mkdir(parents=True)
    source = tmp_path / "данные $(echo nope); file.bin"
    data = bytes(range(256)) * 1024
    source.write_bytes(data)
    destination = tmp_path / "downloads"
    destination.mkdir()
    with session_with_sftp(root) as (session, server):
        listed = operation(session, "list", ".")
        assert listed.error is None
        assert listed.result[0] == "/"
        assert listed.result[1][0].filename == "sub dir"
        uploaded = operation(session, "upload", "/sub dir", [source])
        assert uploaded.error is None
        assert (root / "sub dir" / source.name).read_bytes() == data
        listed = operation(session, "list", "/sub dir")
        assert listed.result[1][0].filename == source.name
        downloaded = operation(session, "download", "/sub dir", ["/sub dir/" + source.name], destination)
        assert downloaded.error is None
        assert (destination / source.name).read_bytes() == data
        assert server.authentications == 1
        assert session.send(b"shell still works\r")
        wait_for(lambda: b"shell still works" in server.shell_data)


def test_conflicts_missing_paths_and_cancelled_upload_preserve_files(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    source = tmp_path / "file.txt"
    source.write_text("new")
    (root / source.name).write_text("existing")
    with session_with_sftp(root) as (session, _):
        job = operation(session, "upload", "/", [source])
        assert "Already exists" in job.error
        assert (root / source.name).read_text() == "existing"
        job = operation(session, "download", "/", ["/file.txt"], tmp_path)
        assert "Already exists" in job.error
        assert source.read_text() == "new"
        assert operation(session, "list", "/missing").error
        source = tmp_path / "large.bin"
        source.write_bytes(b"x" * 200000)
        job = SftpOperation(session.channel.get_transport(), "upload", "/", [source])
        def cancelled_copy(source, destination, size, name):
            destination.write(source.read(1024))
            job.cancelled.set()
            job.check()
        job.copy = cancelled_copy
        job.start()
        assert job.finished.wait(10)
        assert job.error == "Operation cancelled"
        assert not (root / source.name).exists()
        assert not list(root.glob("*.part"))
        assert session.status == "Connected"


def test_browser_navigation_transfer_controls_and_close_leave_shell_alive(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "remote"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("remote")
    source = tmp_path / "uploaded.txt"
    source.write_text("local")
    destination = tmp_path / "downloads"
    destination.mkdir()
    with session_with_sftp(root) as (session, _):
        owner = QMainWindow()
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        def finish():
            assert browser.operation.finished.wait(10)
            browser.poll()
        try:
            finish()
            assert browser.directory == "/" and browser.files.topLevelItemCount() == 2
            browser.activate(browser.files.topLevelItem(0), 0)
            finish()
            assert browser.directory == "/sub"
            browser.up.click()
            finish()
            assert browser.directory == "/"
            monkeypatch.setattr("ssh_jumper.sftp_browser.QFileDialog.getOpenFileNames", lambda *a: ([str(source)], ""))
            browser.upload()
            finish()
            finish()  # Automatic refresh after successful upload.
            assert browser.files.topLevelItemCount() == 3
            browser.files.topLevelItem(1).setSelected(True)
            monkeypatch.setattr("ssh_jumper.sftp_browser.QFileDialog.getExistingDirectory", lambda *a: str(destination))
            browser.download()
            finish()
            assert (destination / "a.txt").read_text() == "remote"
            browser.navigate("/missing")
            finish()
            assert browser.directory == "/" and browser.address.text() == "/"
        finally:
            browser.close()
            owner.terminal.close()
            owner.close()
        assert session.send(b"still connected\r")


def test_sync_both_directions_quoting_nonce_timeout_and_tui_guard(monkeypatch):
    app = QApplication.instance() or QApplication([])
    owner = QMainWindow()
    owner.terminal = TerminalWidget()
    sent = []
    owner.session = SimpleNamespace(status="Connected", send=lambda data: sent.append(data) or True)
    monkeypatch.setattr(SftpBrowser, "navigate", lambda self, path: setattr(self, "navigated", path))
    browser = SftpBrowser(owner)
    try:
        browser.sync_from_terminal()
        assert not sent  # No known shell prompt.
        owner.terminal.feed(b"\x1b[?2004h")
        browser.sync_from_terminal()
        token = browser.probe
        assert token and not owner.terminal.isEnabled()
        browser.cwd_received("wrong", "/wrong")
        assert browser.probe == token
        response = f"\x1b]777;ssh-jumper-cwd;{token};/a path;with spaces\x07".encode()
        for byte in response:
            owner.terminal.feed(bytes([byte]))
        assert browser.navigated == "/a path;with spaces"
        assert owner.terminal.isEnabled()
        browser.directory = "/a'b;$(touch nope)"
        browser.sync_to_terminal()
        import shlex
        command = sent[-1].removeprefix(b"\x1b[200~").removesuffix(b"\x1b[201~\r").decode()
        assert shlex.split(command) == ["cd", browser.directory]
        QTest.keyClick(owner.terminal, Qt.Key.Key_I)
        count = len(sent)
        browser.sync_to_terminal()
        assert len(sent) == count
        owner.terminal.input_pending = False
        owner.terminal.feed(b"\x1b[?1049h")
        browser.sync_from_terminal()
        assert len(sent) == count
        owner.terminal.feed(b"\x1b[?1049l")
        browser.sync_from_terminal()
        late = browser.probe
        browser.probe_timeout()
        browser.cwd_received(late, "/late")
        assert browser.navigated != "/late"
        assert owner.terminal.isEnabled()
    finally:
        browser.close()
        owner.terminal.close()
        owner.close()


@pytest.mark.parametrize("value", ["", "/bad\npath", "/bad\x00path"])
def test_remote_control_characters_rejected(value):
    with pytest.raises(ValueError):
        safe_remote(value)


@pytest.mark.parametrize('legacy', [False, True], ids=['bracketed-paste', 'basic-shell'])
def test_sync_commands_in_real_bash(tmp_path, monkeypatch, legacy):
    bash = Path("C:/Program Files/Git/usr/bin/bash.exe")
    if not bash.exists():
        pytest.skip("Git Bash is required for this smoke test")
    app = QApplication.instance() or QApplication([])
    owner = QMainWindow()
    owner.terminal = TerminalWidget()
    sent = []
    owner.session = SimpleNamespace(status="Connected", send=lambda data: sent.append(data) or True)
    monkeypatch.setattr(SftpBrowser, "navigate", lambda self, path: setattr(self, "navigated", path))
    browser = SftpBrowser(owner)
    try:
        owner.terminal.feed(b"device:/ # " if legacy else b"\x1b[?2004h")
        browser.sync_from_terminal()
        def command(data):
            if legacy:
                assert b'\x1b' not in data
            return data.removeprefix(b"\x1b[200~").removesuffix(b"\x1b[201~\r").removesuffix(b'\r').decode()
        output = subprocess.run([str(bash), "-c", 'unset PWD; ' + command(sent[-1])], cwd=tmp_path,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=True)
        owner.terminal.feed(output.stdout)
        assert browser.navigated.endswith(tmp_path.name)
        directory = tmp_path / "a'b $(not-a-command)"
        directory.mkdir()
        browser.directory = browser.navigated + "/" + directory.name
        browser.sync_to_terminal()
        output = subprocess.run([str(bash), "-c", command(sent[-1]) + "; pwd"], cwd=tmp_path,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=True)
        assert output.stdout.decode().strip() == browser.directory
    finally:
        browser.close()
        owner.terminal.close()
        owner.close()


@pytest.mark.parametrize("value", ["/bad:name", "/bad\\name", "/trailing.", "/..", "/"])
def test_local_names_cannot_escape_download_directory(value):
    if os.name != "nt" and value not in ("/..", "/"):
        assert local_name(value) == posixpath.basename(value)
        return
    with pytest.raises(ValueError):
        local_name(value)


@pytest.mark.parametrize('display,pending,ready', [
    (b'root@device:/ # ', False, True),
    (b'/home/user $ ', False, True),
    (b'root@device:/ # ', True, False),
    (b'root@device:/ # unfinished', False, False),
    (b'device> ', False, False),
    (b'working...', False, False),
    (b'root@device:/ # \x1b[?1049h', False, False),
])
def test_basic_shell_sync_prompt_guards(monkeypatch, display, pending, ready):
    app = QApplication.instance() or QApplication([])
    owner = QMainWindow()
    owner.terminal = TerminalWidget()
    sent = []
    owner.session = SimpleNamespace(status='Connected', send=lambda data: sent.append(data) or True)
    monkeypatch.setattr(SftpBrowser, 'navigate', lambda *args: None)
    browser = SftpBrowser(owner)
    try:
        owner.terminal.feed(display)
        owner.terminal.input_pending = pending
        assert browser.shell_ready() == ready
        browser.directory = "/a'b;$(bad)"
        browser.sync_to_terminal()
        assert bool(sent) == ready
        if ready:
            import shlex
            assert b'\x1b' not in sent[0]
            assert shlex.split(sent[0].decode()) == ['cd', browser.directory]
    finally:
        browser.close()
        owner.terminal.close()
        owner.close()
