"""Exercise fallback against a real SSH server and legacy OpenSSH SCP process."""
from pathlib import Path
import shutil
import subprocess
import threading

import paramiko
import pytest

from ssh_jumper.file_editor import EditorOperation
from ssh_jumper.file_viewer import ReadSnapshot
from ssh_jumper.scp_files import quote
from ssh_jumper.scp_files import ScpReader, acknowledgement
from ssh_jumper.sftp_browser import SftpOperation
from test_sftp_browser import Server, session_with_sftp, operation


@pytest.fixture
def shell_server(tmp_path, monkeypatch, request):
    bash = shutil.which('bash') or r'C:\Program Files\Git\bin\bash.exe'
    if not Path(bash).exists():
        pytest.skip('POSIX shell required for legacy SCP integration test')
    probe = subprocess.run([bash, '-c', 'command -v scp; command -v stat'], capture_output=True)
    if probe.returncode:
        pytest.skip('SCP and stat required')
    processes = []

    def execute(server, channel, command):
        def serve():
            prefix = "stat() { printf 'sh: stat: not found\\n' >&2; return 127; }; " if getattr(request, 'param', False) else ''
            process = subprocess.Popen([bash, '-c', prefix + command.decode()], cwd=tmp_path,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(process)
            def input_pipe():
                try:
                    while data := channel.recv(65536):
                        process.stdin.write(data)
                        process.stdin.flush()
                except (OSError, EOFError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
            def output_pipe(stream, send):
                try:
                    while data := stream.read1(65536):
                        send(data)
                except (OSError, EOFError):
                    pass
            threading.Thread(target=input_pipe, daemon=True).start()
            errors = threading.Thread(target=output_pipe, args=(process.stderr, channel.sendall_stderr), daemon=True)
            errors.start()
            output_pipe(process.stdout, channel.sendall)
            errors.join(3)
            channel.send_exit_status(process.wait(5))
            channel.close()
        threading.Thread(target=serve, daemon=True).start()
        return True

    monkeypatch.setattr(Server, 'check_channel_subsystem_request', lambda *args: False)
    monkeypatch.setattr(Server, 'check_channel_exec_request', execute)
    yield tmp_path
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(5)


def finish(job):
    job.start()
    assert job.finished.wait(20), job.progress
    assert job.error is None, job.error
    return job


@pytest.mark.parametrize('shell_server', [False, True], indirect=True, ids=['stat', 'no-stat'])
def test_scp_fallback_browse_transfer_edit_and_shell_survives(shell_server, tmp_path):
    root = shell_server
    (root / 'remote').mkdir()
    (root / 'remote' / '.hidden').write_bytes(b'hidden')
    (root / 'remote' / 'folder').mkdir()
    source = root / "quoted ' $(echo injected); данные.bin"
    data = bytes(range(256)) * 600
    source.write_bytes(data)
    downloads = root / 'downloads'
    downloads.mkdir()
    with session_with_sftp(root) as (session, server):
        listed = operation(session, 'list', './remote')
        assert listed.error is None, listed.error
        assert listed.protocol == 'SCP' and listed.fallback_reason
        directory, entries = listed.result
        assert {e.filename for e in entries} == {'.hidden', 'folder'}
        uploaded = operation(session, 'upload', directory, [source])
        assert uploaded.error is None, uploaded.error
        remote = directory + '/' + source.name
        assert (root / 'remote' / source.name).read_bytes() == data
        from ssh_jumper.scp_files import ScpFiles
        backend = ScpFiles(SftpOperation(session.channel.get_transport(), 'list', directory))
        with pytest.raises(OSError):
            backend.rename(remote, directory + '/folder')
        assert (root / 'remote' / source.name).exists()
        assert not list((root / 'remote' / 'folder').iterdir())
        conflict = operation(session, 'upload', directory, [source])
        assert 'Already exists' in conflict.error
        downloaded = operation(session, 'download', directory, [remote], downloads)
        assert downloaded.error is None, downloaded.error
        assert (downloads / source.name).read_bytes() == data
        transport = session.channel.get_transport()
        assert finish(ReadSnapshot(remote, transport)).result[2] == len(data)
        assert finish(EditorOperation(remote, transport)).result == data
        finish(EditorOperation(remote, transport, b'changed', data))
        assert (root / 'remote' / source.name).read_bytes() == b'changed'
        rejected = EditorOperation(remote, transport, b'overwrite', data)
        rejected.start()
        assert rejected.finished.wait(20)
        assert 'changed' in rejected.error
        assert operation(session, 'touch', directory + '/empty').error is None
        assert (root / 'remote' / 'empty').read_bytes() == b''
        assert operation(session, 'mkdir', directory + '/new').error is None
        assert operation(session, 'delete', directory, [directory + '/new', remote]).error is None
        assert not (root / 'remote' / source.name).exists()
        assert server.authentications == 1
        assert session.send(b'shell alive\r')


def test_scp_cancel_and_missing_commands(shell_server, monkeypatch):
    root = shell_server
    source = root / 'source'
    source.write_bytes(b'x' * 100000)
    (root / 'remote').mkdir()
    with session_with_sftp(root) as (session, _):
        listed = operation(session, 'list', './remote')
        assert listed.error is None, listed.error
        directory = listed.result[0]
        job = SftpOperation(session.channel.get_transport(), 'upload', directory, [source])
        def cancel(source_stream, destination, size, name):
            destination.write(source_stream.read(1024))
            job.cancelled.set()
            job.check()
        job.copy = cancel
        job.start()
        assert job.finished.wait(20)
        assert job.error == 'Operation cancelled'
        assert not (root / 'remote' / 'source').exists()
        assert not list((root / 'remote').glob('.ssh-jumper-*.part'))
        # A rejected exec request must produce a useful combined diagnostic.
        monkeypatch.setattr(Server, 'check_channel_exec_request', lambda *args: False)
        failed = operation(session, 'list', '.')
        assert 'SFTP unavailable' in failed.error and 'SCP/shell fallback failed' in failed.error


@pytest.mark.parametrize('path', ['-option', "a' b;$(bad)", '/absolute'])
def test_quote_operands(path):
    import shlex
    assert shlex.split(quote(path)) == [path if path.startswith('/') else './' + path]


def test_sftp_operation_failure_does_not_trigger_scp(tmp_path):
    with session_with_sftp(tmp_path) as (session, _):
        job = operation(session, 'list', '/missing')
        assert job.error
        assert job.protocol == 'SFTP' and job.fallback_reason is None


class ProtocolChannel:
    def __init__(self, data):
        self.data, self.sent, self.closed = data, bytearray(), False

    def recv(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


def test_scp_rejects_server_errors_and_truncated_transfers():
    from types import SimpleNamespace
    with pytest.raises(OSError, match='Permission denied'):
        acknowledgement(ProtocolChannel(b'\x01Permission denied\n'))
    channel = ProtocolChannel(b'C0644 5 unexpected-name\nabc')
    client = SimpleNamespace(channel=lambda *args: channel,
                             operation=SimpleNamespace(check=lambda: None))
    with ScpReader(client, '/requested-name') as stream:
        with pytest.raises(EOFError, match='before the transfer completed'):
            stream.read()
    assert channel.closed


@pytest.mark.parametrize('shell_server', [False, True], indirect=True, ids=['stat', 'no-stat'])
def test_scp_viewer_tail_and_recursive_copy(shell_server):
    root = shell_server
    (root / 'remote').mkdir()
    folder = root / 'tree'
    folder.mkdir()
    (folder / 'nested').mkdir()
    (folder / 'nested' / 'text').write_bytes(b'abc\n')
    downloads = root / 'downloads'
    downloads.mkdir()
    with session_with_sftp(root) as (session, _):
        listing = operation(session, 'list', './remote')
        assert listing.error is None, listing.error
        directory = listing.result[0]
        assert operation(session, 'upload', directory, [folder]).error is None
        assert operation(session, 'move_download', directory, [directory + '/tree'], downloads).error is None
        assert (downloads / 'tree' / 'nested' / 'text').read_bytes() == b'abc\n'
        assert not (root / 'remote' / 'tree').exists()
        transport = session.channel.get_transport()
        (root / 'remote' / 'log').write_bytes(b'x' * (2 * 1024 * 1024) + b'tail')
        tail = finish(ReadSnapshot(directory + '/log', transport, tail=True))
        assert tail.result[0].endswith('tail') and tail.result[1] == 4


def test_browser_displays_scp_fallback(shell_server):
    from PySide6.QtWidgets import QApplication, QMainWindow
    from ssh_jumper.sftp_browser import SftpBrowser
    from ssh_jumper.terminal import TerminalWidget
    app = QApplication.instance() or QApplication([])
    with session_with_sftp(shell_server) as (session, _):
        owner = QMainWindow()
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        try:
            assert browser.operation.finished.wait(20)
            browser.poll()
            assert browser.directory
            assert 'SCP' in browser.remote_label.text()
            assert 'SFTP unavailable' in browser.remote_label.toolTip()
            browser.set_active(True)
            assert browser.remote_label.text() == 'Remote · SCP'
        finally:
            browser.close()
            owner.terminal.close()
            owner.close()


@pytest.mark.parametrize('listing,expected_mode,expected_size', [
    ('-rw-r----- 1 0 0 123 Jan  2 2025 ignored name', 0o100640, 123),
    ('drwxr-xr-x 2 0 0 4096 Oct 18 12:34 directory', 0o040755, 4096),
    ('lrwxrwxrwx 1 0 0 7 Oct 18 12:34 name -> target', 0o120777, 7),
    ('-rwsr-Sr-T+ 1 0 0 3 Oct 18 12:34 executable', 0o107744, 3),
    ('crw-rw-rw- 1 0 0 1, 3 Jan  2 2025 device', 0o020666, 0),
])
def test_ls_metadata_formats(listing, expected_mode, expected_size):
    from ssh_jumper.scp_files import ScpFiles
    backend = ScpFiles(None)
    backend._metadata = 'ls'
    info = backend.parse_metadata(listing, "./actual ' name -> literal")
    assert info.filename == "actual ' name -> literal"
    assert info.st_mode == expected_mode
    assert info.st_size == expected_size
    assert info.st_mtime > 0


def test_ls_metadata_rejects_unknown_format():
    from ssh_jumper.scp_files import ScpFiles
    backend = ScpFiles(None)
    backend._metadata = 'ls'
    with pytest.raises(OSError, match='Cannot read remote ls metadata'):
        backend.parse_metadata('vendor command: access denied', './file')
