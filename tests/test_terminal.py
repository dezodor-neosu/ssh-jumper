from contextlib import contextmanager
import socket
import threading
import queue
import time
import subprocess
import shutil
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ssh_jumper.integrations import IntegrationSettings, SettingsStore
from ssh_jumper.models import Forward, Hop, Profile, SSHAuth
from ssh_jumper.terminal import TerminalScreen, TerminalSession, TerminalStream, TerminalWidget
from ssh_jumper.tunnel import Tunnel
from test_tunnel import ssh_server, wait_for


def test_tui_screen_colors_cursor_regions_and_restore():
    replies = []
    screen = TerminalScreen(80, 24, replies.append)
    stream = TerminalStream(screen)
    stream.feed(b"shell$ \x1b[?1049h\x1b[?25l\x1b[H\x1b[2J")
    stream.feed(b"\x1b[32mCPU [||||    ]\x1b[0m\x1b[3;1H\x1b[7m PID USER   CPU% COMMAND\x1b[0m")
    stream.feed(b"\x1b[4;1H\x1b[38;5;196m 123 tester 99.0 htop\x1b[0m")
    stream.feed(b"\x1b[24;1HF1Help F2Setup F10Quit")
    assert screen.display[0].startswith("CPU [||||")
    assert screen.buffer[0][0].fg == "green"
    assert screen.buffer[2][0].reverse
    assert screen.buffer[3][0].fg == "ff0000"
    assert screen.cursor.hidden
    stream.feed(b"\x1b[4;22r\x1b[22;1H\n")
    assert screen.display[2].startswith(" PID USER")
    assert screen.display[23].startswith("F1Help")
    assert not screen.history
    stream.feed(b"\x1b[6n")
    assert replies[-1] == b"\x1b[22;1R"
    stream.feed(b"\x1b[?1049l\x1b[?25h")
    assert screen.display[0].startswith("shell$ ")
    assert (screen.cursor.x, screen.cursor.y) == (7, 0)
    assert not screen.cursor.hidden


def test_fragmented_utf8_acs_and_resize_both_buffers():
    screen = TerminalScreen(30, 8)
    stream = TerminalStream(screen)
    for byte in "Привет 界 é".encode():
        stream.feed(bytes([byte]))
    assert screen.display[0].startswith("Привет 界 é")
    stream.feed(b"\r\n\x1b(0lqqk\x1b(B")
    assert screen.display[1].startswith("┌──┐")
    stream.feed(b"\x1b[?1049h\x1b[8;30HX")
    screen.resize(lines=5, columns=20)
    stream.feed(b"\x1b[?1049l")
    assert len(screen.display) == 5
    assert all(len(row) <= 20 for row in screen.display)
    assert screen.cursor.y < 5
    assert screen.cursor.x < 20


def test_scrollback_is_bounded_and_alternate_screen_does_not_pollute_it():
    screen = TerminalScreen(20, 3)
    stream = TerminalStream(screen)
    stream.feed(b"line\r\n" * 2100)
    assert len(screen.history) == 2000
    before = list(screen.history)
    stream.feed(b"\x1b[?1049h" + b"TUI\r\n" * 30 + b"\x1b[?1049l")
    assert list(screen.history) == before


def test_device_reports_origin_and_expanded_tabstops():
    replies = []
    screen = TerminalScreen(20, 5, replies.append)
    stream = TerminalStream(screen)
    stream.feed(b"\x1b[?6h\x1b[?6n\x1b[5n\x1b[c")
    assert replies == [b"\x1b[?1;1R", b"\x1b[0n", b"\x1b[?6c"]
    screen.resize(columns=100)
    stream.feed(b"\x1b[1;81H\tX")
    assert screen.buffer[0][88].data == "X"


@pytest.mark.parametrize("split", [1, 7, 4096])
def test_vim_extensions_are_consumed_without_crashing_or_corrupting_screen(split):
    replies = []
    screen = TerminalScreen(reply=replies.append)
    stream = TerminalStream(screen)
    sequences = (b"\x1b[?1049h\x1b[Hhello"
                 b"\x1b[>4;2m\x1b[>0;10;1c\x1b[?4m\x1b[?12$p"
                 b"\x1bP+q436f;6b75\x1b\\\x1bP$qm\x1b\\"
                 b"\x1b]11;?\x07\x1b[2 q\x1b[?1;2J"
                 b"\x1b[1;2;3H\x1b[38:2::255:0:0m"
                 b"\x1b_Gignored image payload\x1b\\ world")
    for pos in range(0, len(sequences), split):
        stream.feed(sequences[pos:pos + split])
    assert screen.display[0].startswith("hello world")
    assert not screen.buffer[0][5].underscore  # >4;2m is not SGR underline.
    assert b"\x1b[>0;0;0c" in replies
    assert b"\x1b[>4;0m" in replies
    assert b"\x1bP0+r436f\x1b\\" in replies
    assert b"\x1b]11;rgb:1010/1515/1d1d\x1b\\" in replies


def test_control_strings_are_bounded_and_recover_after_terminator():
    screen = TerminalScreen()
    stream = TerminalStream(screen)
    stream.feed(b"\x1bP" + b"x" * 100000)
    assert len(stream.sequence) <= 4096
    stream.feed(b"\x1b\\OK")
    assert screen.display[0].startswith("OK")


def test_input_writer_does_not_wait_for_remote_output_and_preserves_partial_sends():
    session = TerminalSession(Forward(2222, "host", 22, ssh=SSHAuth("user", "Password", "secret")), True)
    session.status = "Connected"
    received = bytearray()
    complete = threading.Event()
    payload = b"a" * 20000 + b"\x1b:wq\r"
    class Channel:
        closed = False
        def send(self, data):
            count = min(7, len(data))
            received.extend(data[:count])
            if len(received) == len(payload):
                complete.set()
            return count
        def resize_pty(self, **kwargs):
            pass
        def recv(self, count):
            raise AssertionError("The writer must not call recv")
    thread = threading.Thread(target=session.write, args=(Channel(), (80, 24)))
    thread.start()
    try:
        for data in (payload[:100], payload[100:20000], payload[20000:]):
            assert session.send(data)
        assert complete.wait(1), "Input stalled waiting for output"
        assert received == payload
    finally:
        session.connection.stop.set()
        thread.join(1)
    assert not thread.is_alive()


def test_incremental_render_updates_only_changed_rows():
    app = QApplication.instance() or QApplication([])
    class Widget(TerminalWidget):
        def update(self, *args):
            self.updates.append(args)
    widget = Widget()
    widget.updates = []
    widget.feed(b"initial")
    widget.updates.clear()
    widget.feed(b"\x1b[3;1Hx")
    rows = {args[0].y() // widget.cell_height for args in widget.updates}
    assert rows == {0, 2}
    widget.updates.clear()
    QTest.keyClick(widget, Qt.Key.Key_I)
    assert not widget.updates  # Remote echo, not local input, invalidates the grid.
    widget.close()


def test_real_vim_start_edit_save_and_exit(tmp_path):
    vim = shutil.which("vim")
    if vim is None:
        bundled = Path("C:/Program Files/Git/usr/bin/vim.exe")
        if not bundled.exists():
            pytest.skip("A local Vim executable is needed for this smoke test")
        vim = str(bundled)
    document = tmp_path / "sample.txt"
    document.write_text("first line\nsecond line\n")
    process = subprocess.Popen([vim, "--not-a-term", "-T", "xterm-256color", "-u", "NONE", "-i", "NONE", "-n",
                                "-c", "set lines=24 columns=80 noswapfile", document.name],
                               cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = queue.Queue()
    def read():
        while data := process.stdout.read1(65536):
            output.put(data)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    def send(data):
        if process.poll() is None:
            process.stdin.write(data)
            process.stdin.flush()
    screen = TerminalScreen(reply=send)
    stream = TerminalStream(screen)
    def until(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                stream.feed(output.get(timeout=0.05))
            except queue.Empty:
                pass
            if predicate():
                return
        raise AssertionError("Vim did not reach expected state: " + repr(screen.display))
    try:
        until(lambda: "first line" in "\n".join(screen.display))
        send(b"iEDITED \x1b")
        until(lambda: "EDITED first line" in "\n".join(screen.display))
        send(b":wq\r")
        until(lambda: process.poll() is not None)
        assert process.returncode == 0
        assert document.read_text().startswith("EDITED first line")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        reader.join(1)
        process.stdin.close()
        process.stdout.close()


def test_keyboard_paste_selection_and_render(tmp_path):
    app = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    sent = []
    widget.data.connect(sent.append)
    widget.resize(800, 480)
    widget.show()
    app.processEvents()
    try:
        QTest.keyClick(widget, Qt.Key.Key_Tab)
        QTest.keyClick(widget, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(widget, Qt.Key.Key_Up)
        widget.feed(b"\x1b[?1h")
        QTest.keyClick(widget, Qt.Key.Key_Up)
        QTest.keyClick(widget, Qt.Key.Key_F10)
        assert sent == [b"\t", b"\x03", b"\x1b[A", b"\x1bOA", b"\x1b[21~"]
        widget.feed(b"\x1b[?2004h")
        app.clipboard().setText("one\r\ntwo\x1b[201~")
        widget.paste()
        assert sent[-1] == b"\x1b[200~one\rtwo[201~\x1b[201~"
        widget.feed("hello 界\r\n\x1b[31mred\x1b[0m".encode())
        widget.selection = ((0, 0), (4, 0))
        widget.copy_selection()
        assert app.clipboard().text() == "hello"
        assert widget.grab().save(str(tmp_path / "terminal.png"))
    finally:
        widget.close()


class ShellServer(paramiko.ServerInterface):
    def __init__(self):
        self.sizes = []
        self.shell = threading.Event()
        self.received = bytearray()

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ("tester", "secret") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, *args):
        self.term = term
        self.sizes.append((width, height))
        return True

    def check_channel_window_change_request(self, channel, width, height, *args):
        self.sizes.append((width, height))
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True


@contextmanager
def shell_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    server = ShellServer()
    key = paramiko.RSAKey.generate(2048)
    transports = []
    def serve():
        try:
            client, _ = listener.accept()
            transport = paramiko.Transport(client)
            transports.append(transport)
            transport.add_server_key(key)
            transport.start_server(server=server)
            channel = transport.accept(5)
            if channel is None or not server.shell.wait(5):
                return
            channel.sendall(b"\x1b[?1049h\x1b[2J\x1b[H\x1b[32mCPU [||||]\x1b[0m")
            while data := channel.recv(65536):
                server.received.extend(data)
                if data == b"exit\r":
                    channel.shutdown_write()
                    channel.close()
                    break
                channel.sendall(data)
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            for transport in transports:
                transport.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], server, key
    finally:
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(6)


def test_real_ssh_pty_through_jump_resize_input_and_cleanup():
    with shell_server() as (port, server, _), ssh_server() as (jump_port, _):
        # Use an ephemeral listener, owned by the real tunnel forwarding engine.
        tunnel = Tunnel(Profile(hops=[Hop("127.0.0.1", jump_port, "tester", "Password", "secret")]))
        transport = tunnel.connect_hop(tunnel.profile.hops[0], ("127.0.0.1", jump_port))
        local = tunnel.add_forward(transport, ("127.0.0.1", port))
        forward = Forward(local.port, "127.0.0.1", port, ssh=SSHAuth("tester", "Password", "secret"))
        session = TerminalSession(forward, True)
        try:
            session.start()
            wait_for(lambda: session.status == "Connected")
            assert server.term == b"xterm-256color"
            assert server.sizes[0] == (80, 24)
            wait_for(lambda: not session.output.empty())
            screen = TerminalScreen()
            TerminalStream(screen).feed(session.output.get_nowait())
            assert screen.display[0].startswith("CPU [||||]")
            assert session.send(b"\x1b[A\x03hello\r")
            wait_for(lambda: b"hello\r" in server.received)
            session.resize(132, 43)
            wait_for(lambda: (132, 43) in server.sizes)
        finally:
            session.close()
            session.thread.join(5)
            tunnel.cleanup()
        assert not session.thread.is_alive()
        assert not session.connection.clients


@pytest.mark.parametrize("strict,password", [(False, "wrong"), (True, "secret")])
def test_authentication_and_unknown_host_fail_closed(monkeypatch, tmp_path, strict, password):
    monkeypatch.setattr("ssh_jumper.tunnel.Path.home", lambda: tmp_path)
    with shell_server() as (port, server, _):
        session = TerminalSession(Forward(port, "endpoint.test", 22,
            ssh=SSHAuth("tester", "Password", password)), not strict)
        session.start()
        session.thread.join(6)
        assert not session.thread.is_alive()
        assert session.status.startswith("Connection failed:")
        assert not server.shell.is_set()
        assert password not in session.status


def test_trusted_real_endpoint_identity_and_remote_eof(monkeypatch, tmp_path):
    monkeypatch.setattr("ssh_jumper.tunnel.Path.home", lambda: tmp_path)
    with shell_server() as (port, server, key):
        known = tmp_path / ".ssh" / "known_hosts"
        known.parent.mkdir()
        known.write_text(f"[endpoint.test]:2200 {key.get_name()} {key.get_base64()}\n")
        session = TerminalSession(Forward(port, "endpoint.test", 2200,
            ssh=SSHAuth("tester", "Password", "secret")), False)
        try:
            session.start()
            wait_for(lambda: session.status == "Connected")
            assert server.shell.is_set()
            assert session.send(b"exit\r")
            session.thread.join(5)
            assert not session.thread.is_alive()
            assert session.status.startswith("Disconnected")
            assert not session.send(b"data after EOF")
        finally:
            session.close()


def test_native_settings_and_ui_dispatch(tmp_path, monkeypatch):
    from ssh_jumper.storage import Store
    from ssh_jumper.ui import MainWindow, SettingsDialog
    store = SettingsStore(tmp_path / "settings.json")
    store.save(IntegrationSettings())
    assert store.load() == IntegrationSettings()
    app = QApplication.instance() or QApplication([])
    profile_store = Store(tmp_path / "profiles.json")
    forward = Forward(2222, "endpoint.test", 22, ssh=SSHAuth("tester", "Password", "secret"))
    profile = Profile(hops=[Hop("jump", username="tester")], forwards=[forward], ignore_host_keys=False)
    profile_store.save([profile])
    window = MainWindow(profile_store)
    started = []
    monkeypatch.setattr(TerminalSession, "start", lambda self: started.append(self))
    try:
        dialog = SettingsDialog(window, store.load())
        assert dialog.editor_path.text() == ""
        window.tunnel = SimpleNamespace(profile=profile, thread=SimpleNamespace(is_alive=lambda: True))
        window.connection_status = "Connected"
        window.forward_list.setCurrentRow(0)
        window.open_terminal()
        assert len(window.terminals) == 1
        assert started[0].endpoint.host == "endpoint.test"
        assert not started[0].connection.profile.ignore_host_keys
    finally:
        for terminal in list(window.terminals):
            terminal.close()
        window.tray.hide()
        window.deleteLater()
        app.processEvents()
