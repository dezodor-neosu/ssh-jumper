"""Commander integration tests using a real in-process SSH/SFTP server."""
from pathlib import Path

import paramiko
import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from ssh_jumper.sftp_browser import SftpBrowser
from ssh_jumper.file_viewer import FileViewer, ReadSnapshot, VIEW_LIMIT
from ssh_jumper.terminal import TerminalWidget
from test_sftp_browser import Filesystem, operation, session_with_sftp


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def directory_server(monkeypatch):
    def mkdir(self, path, attr):
        try:
            self.path(path).mkdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
    def rmdir(self, path):
        try:
            self.path(path).rmdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
    monkeypatch.setattr(Filesystem, "mkdir", mkdir)
    monkeypatch.setattr(Filesystem, "rmdir", rmdir)


def test_recursive_move_both_directions_and_conflict_preserves_source(tmp_path, directory_server):
    root = tmp_path / "remote"
    root.mkdir()
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "hello.txt").write_bytes(b"hello")
    (source / "empty").mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    with session_with_sftp(root) as (session, _):
        job = operation(session, "move_upload", "/", [source])
        assert job.error is None
        assert not source.exists()
        assert (root / "tree/nested/hello.txt").read_bytes() == b"hello"
        job = operation(session, "move_download", "/", ["/tree"], destination)
        assert job.error is None
        assert not (root / "tree").exists()
        assert (destination / "tree/nested/hello.txt").read_bytes() == b"hello"
        assert (destination / "tree/empty").is_dir()
        (root / "existing").write_bytes(b"old")
        source = tmp_path / "existing"
        source.write_bytes(b"new")
        assert operation(session, "move_upload", "/", [source]).error
        assert source.read_bytes() == b"new"
        assert (root / "existing").read_bytes() == b"old"
        assert operation(session, "mkdir", "/new folder").error is None
        assert operation(session, "touch", "/new folder/empty.txt").error is None
        assert operation(session, "touch", "/existing").error
        assert (root / "existing").read_bytes() == b"old"


def snapshot(path, transport=None, tail=False):
    job = ReadSnapshot(str(path), transport, tail)
    job.start()
    assert job.finished.wait(10)
    assert job.error is None, job.error
    return job.result


@pytest.mark.parametrize("remote", [False, True])
def test_follow_reads_appends_rotation_truncation_and_missing_name(tmp_path, remote):
    root = tmp_path / "remote"
    root.mkdir()
    path = root / "log.txt"
    path.write_bytes(b"first\n")
    with session_with_sftp(root) as (session, _):
        transport = session.channel.get_transport() if remote else None
        name = "/log.txt" if remote else str(path)
        assert snapshot(name, transport, True)[0] == "first\n"
        with path.open("ab") as stream:
            stream.write(b"second\n")
        assert snapshot(name, transport, True)[0] == "first\nsecond\n"
        path.rename(root / "old.log")
        job = ReadSnapshot(name, transport, True)
        job.run()
        assert job.error
        path.write_bytes(b"replacement\n")
        assert snapshot(name, transport, True)[0] == "replacement\n"
        path.write_bytes(b"short\n")
        assert snapshot(name, transport, True)[0] == "short\n"
        path.write_bytes(b"x" * VIEW_LIMIT + b"tail")
        content, offset, size = snapshot(name, transport, True)
        assert len(content) == VIEW_LIMIT and content.endswith("tail") and offset == 4
        assert snapshot(name, transport)[0] == "x" * VIEW_LIMIT


def drain(browser):
    for _ in range(8):
        jobs = [job for job in (browser.operation, browser.local_job) if job]
        if not jobs:
            return
        for job in jobs:
            assert job.finished.wait(10)
        browser.poll()
    raise AssertionError("Jobs did not settle")


def test_commander_keyboard_fuzzy_and_creation(app, tmp_path, monkeypatch, directory_server):
    root = tmp_path / "remote"
    root.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    (local / "application.log").write_text("line one\nline two\n", encoding="utf-8")
    (local / "other.txt").write_text("other", encoding="utf-8")
    with session_with_sftp(root) as (session, _):
        owner = QMainWindow()
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        browser.show()
        try:
            drain(browser)
            browser.navigate_local(str(local))
            drain(browser)
            browser.activateWindow()
            browser.local_files.setFocus()
            app.processEvents()
            QTest.keyClick(browser.local_files, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClicks(browser.local_filter, "aplg")
            assert not browser.local_files.topLevelItem(1).isHidden()
            assert browser.local_files.topLevelItem(2).isHidden()
            QTest.keyClick(browser.local_filter, Qt.Key.Key_Return)
            QTest.keyClick(browser.local_files, Qt.Key.Key_F5)
            drain(browser)
            assert (root / "application.log").exists()
            QTest.keyClick(browser.local_files, Qt.Key.Key_F3)
            assert len(browser.viewers) == 1
            browser.viewers[0].close()
            browser.activateWindow()
            browser.local_files.setFocus()
            app.processEvents()
            monkeypatch.setattr("ssh_jumper.sftp_browser.QInputDialog.getText", lambda *a: ("created", True))
            QTest.keyClick(browser.local_files, Qt.Key.Key_F7)
            drain(browser)
            assert (local / "created").is_dir()
            QTest.keyClick(browser.local_files, Qt.Key.Key_Tab)
            app.processEvents()
            assert not browser.active_local
            QTest.keyClick(browser.files, Qt.Key.Key_F9)
            drain(browser)
            assert (root / "created").is_file()
            browser.remote_filter.setText("crtd")
            assert browser.files.topLevelItem(0).isHidden()
            assert not browser.files.topLevelItem(1).isHidden()
            browser.local_filter.clear()
            browser.remote_filter.clear()
            browser.grab().save(str(Path("artifacts") / "sftp-commander.png"))
        finally:
            browser.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            owner.terminal.close()
            owner.close()


def test_viewer_vim_search_and_follow(app, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("alpha beta\nsecond\nthird alpha\nlast", encoding="utf-8")
    viewer = FileViewer(str(path))
    viewer.show()
    try:
        assert viewer.job.finished.wait(10)
        viewer.poll()
        QTest.keyClicks(viewer.text, "e")
        assert viewer.text.textCursor().position() == 4
        QTest.keyClicks(viewer.text, "e")
        assert viewer.text.textCursor().position() == 9
        QTest.keyClicks(viewer.text, "b")
        assert viewer.text.textCursor().position() == 6
        QTest.keyClicks(viewer.text, "gg")
        QTest.keyClicks(viewer.text, "2j")
        assert viewer.text.textCursor().blockNumber() == 2
        QTest.keyClicks(viewer.text, "gg")
        assert viewer.text.textCursor().position() == 0
        QTest.keyClicks(viewer.text, "G")
        assert viewer.text.textCursor().blockNumber() == 3
        QTest.keyClicks(viewer.text, "2G")
        assert viewer.text.textCursor().blockNumber() == 1
        QTest.keyClicks(viewer.text, "/")
        QTest.keyClicks(viewer.search, "alpha")
        QTest.keyClick(viewer.search, Qt.Key.Key_Return)
        assert viewer.text.textCursor().blockNumber() == 2
        QTest.keyClicks(viewer.text, "n")
        assert viewer.text.textCursor().blockNumber() == 0
        QTest.keyClicks(viewer.text, "F")
        assert viewer.follow.isChecked()
        assert viewer.job.finished.wait(10)
        viewer.poll()
        assert viewer.text.textCursor().atEnd()
        path.write_text("rotated", encoding="utf-8")
        viewer.reload()
        assert viewer.job.finished.wait(10)
        viewer.poll()
        assert viewer.text.toPlainText() == "rotated"
    finally:
        viewer.close()

def test_failed_and_cancelled_moves_keep_source(tmp_path, monkeypatch):
    from ssh_jumper.sftp_browser import SftpOperation
    root = tmp_path / "remote"
    root.mkdir()
    source = tmp_path / "source.txt"
    source.write_bytes(b"keep me")
    with session_with_sftp(root) as (session, _):
        job = SftpOperation(session.channel.get_transport(), "move_upload", "/", [source])
        def fail_copy(*args):
            job.cancelled.set()
            job.check()
        monkeypatch.setattr(job, "copy", fail_copy)
        job.start()
        assert job.finished.wait(10)
        assert job.error == "Operation cancelled"
        assert source.read_bytes() == b"keep me"
        assert not list(root.iterdir())

        job = SftpOperation(session.channel.get_transport(), "move_upload", "/", [source])
        original = job.upload
        def changed_after_copy(sftp, path):
            original(sftp, path)
            source.write_bytes(b"changed after copy")
        monkeypatch.setattr(job, "upload", changed_after_copy)
        job.start()
        assert job.finished.wait(10)
        assert "Source changed" in job.error
        assert source.read_bytes() == b"changed after copy"
        assert (root / source.name).read_bytes() == b"keep me"

def test_breadcrumbs_and_parent_navigation(app, tmp_path):
    from ssh_jumper.sftp_browser import PARENT_ROLE
    root = tmp_path / "remote"
    (root / "a & b" / "nested").mkdir(parents=True)
    local = tmp_path / "local & files" / "nested"
    local.mkdir(parents=True)
    with session_with_sftp(root) as (session, _):
        owner = QMainWindow()
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        browser.show()
        try:
            drain(browser)
            assert browser.remote_breadcrumb.paths == ["/"]
            assert browser.files.topLevelItem(0).text(0) != ".."
            browser.navigate('/a & b/nested')
            browser.navigate_local(str(local))
            drain(browser)
            assert browser.remote_breadcrumb.paths == ["/", "/a & b", "/a & b/nested"]
            assert "a &amp; b" in browser.remote_breadcrumb.text()
            for tree in (browser.files, browser.local_files):
                item = tree.topLevelItem(0)
                assert item.text(0) == ".." and item.data(0, PARENT_ROLE)
                tree.setCurrentItem(item)
                assert browser.selected_entries(tree) == []
                browser.filter_files(tree, 'no-match')
                assert not item.isHidden()
            browser.set_active(False)
            browser.transfer(True)
            assert browser.operation is None  # Never transfer a synthetic parent.
            browser.download()
            assert browser.operation is None
            browser.remote_breadcrumb.linkActivated.emit('1')
            drain(browser)
            assert browser.directory == '/a & b'
            browser.activate(browser.files.topLevelItem(0), 0)
            drain(browser)
            assert browser.directory == '/'
            browser.local_breadcrumb.linkActivated.emit(str(len(browser.local_breadcrumb.paths) - 2))
            drain(browser)
            assert browser.local_directory == str(local.parent)
            browser.activate_local(browser.local_files.topLevelItem(0), 0)
            drain(browser)
            assert browser.local_directory == str(tmp_path)
            browser.navigate('/a & b/nested')
            drain(browser)
            browser.files.setCurrentItem(browser.files.topLevelItem(0))
            QTest.keyClick(browser.files, Qt.Key.Key_Return)
            drain(browser)
            assert browser.directory == '/a & b'
        finally:
            browser.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            owner.terminal.close()
            owner.close()

@pytest.mark.parametrize('local_source', [True, False])
def test_space_group_selection_and_copy(app, tmp_path, local_source):
    root = tmp_path / 'remote'
    root.mkdir()
    local = tmp_path / 'local'
    local.mkdir()
    source, target = (local, root) if local_source else (root, local)
    for name in ('file1', 'file2', 'file10'):
        (source / name).write_text(name)
    with session_with_sftp(root) as (session, _):
        owner = QMainWindow()
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        browser.show()
        try:
            drain(browser)
            browser.navigate_local(str(local))
            drain(browser)
            tree = browser.local_files if local_source else browser.files
            items = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
            tree.setFocus()
            browser.set_active(local_source)
            tree.setCurrentItem(items['file1'])
            QTest.keyClick(tree, Qt.Key.Key_Space)
            assert tree.currentItem().text(0) == 'file2'
            QTest.keyClick(tree, Qt.Key.Key_Space)
            assert tree.currentItem().text(0) == 'file10'
            assert len(browser.selection()) == 2
            QTest.keyClick(tree, Qt.Key.Key_Up)
            assert len(browser.selection()) == 2
            QTest.keyClick(tree, Qt.Key.Key_Space)
            assert len(browser.selection()) == 1
            QTest.keyClick(tree, Qt.Key.Key_Up)
            QTest.keyClick(tree, Qt.Key.Key_Space)
            assert len(browser.selection()) == 2
            browser.transfer(False)
            drain(browser)
            assert sorted(p.name for p in target.iterdir()) == ['file1', 'file2']
        finally:
            browser.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            owner.terminal.close()
            owner.close()


def test_column_sorting_natural_numeric_and_parent_pinning(app):
    import stat
    from ssh_jumper.sftp_browser import FileTree, FileItem, SORT_ROLE
    tree = FileTree()
    tree.setHeaderLabels(['Name', 'Type', 'Size', 'Modified'])
    tree.resize(700, 400)
    SftpBrowser.add_parent(tree, '/folder')
    for name, kind, size, modified in [('file10', 'File', 2, 100), ('File2', 'Link', 1000, 300),
                                        ('file1', 'File', 10, 200), ('folder', 'Folder', 0, 0)]:
        item = FileItem([name, kind, f'{size:,}', str(modified)])
        item.setData(0, Qt.ItemDataRole.UserRole, ('/' + name, stat.S_IFDIR if kind == 'Folder' else stat.S_IFREG))
        item.setData(2, SORT_ROLE, size)
        item.setData(3, SORT_ROLE, modified)
        tree.addTopLevelItem(item)
    tree.show()
    app.processEvents()
    def names():
        return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    try:
        assert names() == ['..', 'folder', 'file1', 'File2', 'file10']
        header = tree.header()
        from PySide6.QtCore import QPoint
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(header.sectionViewportPosition(0) + 20, header.height() // 2))
        assert names() == ['..', 'folder', 'file10', 'File2', 'file1']
        for column, expected in [(1, ['file1', 'file10', 'File2']),
                                  (2, ['file10', 'file1', 'File2']),
                                  (3, ['file10', 'file1', 'File2'])]:
            tree.sortItems(column, Qt.SortOrder.AscendingOrder)
            assert names() == ['..', 'folder'] + expected
            tree.sortItems(column, Qt.SortOrder.DescendingOrder)
            assert names() == ['..', 'folder'] + expected[::-1]
    finally:
        tree.close()

@pytest.mark.parametrize('remote', [False, True])
def test_external_editor_upload_conflict_and_limits(app, tmp_path, monkeypatch, remote):
    from ssh_jumper.file_editor import ExternalEditor, EditorOperation
    from ssh_jumper.integrations import IntegrationSettings
    from PySide6.QtWidgets import QMessageBox
    launched = []
    monkeypatch.setattr('ssh_jumper.file_editor.launch_editor', lambda settings, path: launched.append(path))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Close)
    def posix_rename(self, old, new):
        self.path(old).replace(self.path(new))
        return paramiko.SFTP_OK
    monkeypatch.setattr(Filesystem, 'posix_rename', posix_rename)
    monkeypatch.setattr(Filesystem, 'chattr', lambda *a: paramiko.SFTP_OK)
    root = tmp_path / 'remote'
    root.mkdir()
    path = root / 'legacy.ini'
    original = b'VALUE=1\r\n; \xc2\xd1\xd1\xd2\r\n'
    path.write_bytes(original)
    with session_with_sftp(root) as (session, _):
        name = '/legacy.ini' if remote else str(path)
        transport = session.channel.get_transport() if remote else None
        editor = ExternalEditor(name, transport, IntegrationSettings())
        try:
            assert editor.job.finished.wait(10)
            editor.poll()
            assert launched == [editor.local_path]
            assert editor.local_path.suffix == '.ini'
            assert editor.local_path.read_bytes() == original
            changed = original.replace(b'VALUE=1', b'VALUE=2')
            editor.local_path.write_bytes(changed)
            editor.save()
            assert editor.job.finished.wait(10)
            editor.poll()
            assert path.read_bytes() == changed
            assert editor.original == changed
            editor.local_path.write_bytes(b'pending')
            path.write_bytes(b'external change')
            editor.save()
            assert editor.job.finished.wait(10)
            editor.poll()
            assert 'changed' in editor.status.text()
            assert path.read_bytes() == b'external change'
            assert editor.local_path.read_bytes() == b'pending'
            editor.local_path.write_bytes(b'x' * (VIEW_LIMIT + 1))
            editor.save()
            assert editor.job is None
            assert 'limit' in editor.status.text()
            monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Cancel)
            assert not editor.close()
            monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Close)
        finally:
            copy = editor.local_path
            editor.close()
            assert copy.exists()
            copy.unlink()
            copy.parent.rmdir()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        path.write_bytes(b'x' * (VIEW_LIMIT + 1))
        job = EditorOperation(name, transport)
        job.start()
        assert job.finished.wait(10)
        assert job.error


@pytest.mark.parametrize('local_source', [True, False])
def test_delete_confirmation_and_recursive_removal(app, tmp_path, monkeypatch, directory_server, local_source):
    from PySide6.QtWidgets import QMessageBox
    root = tmp_path / 'remote'
    root.mkdir()
    local = tmp_path / 'local'
    local.mkdir()
    source = local if local_source else root
    (source / 'folder' / 'nested').mkdir(parents=True)
    (source / 'folder' / 'nested' / 'file').write_text('delete')
    (source / 'keep').write_text('keep')
    with session_with_sftp(root) as (session, _):
        owner = QMainWindow()
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        browser.show()
        try:
            drain(browser)
            browser.navigate_local(str(local))
            drain(browser)
            tree = browser.local_files if local_source else browser.files
            browser.set_active(local_source)
            item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).text(0) == 'folder')
            tree.setCurrentItem(item)
            monkeypatch.setattr(QMessageBox, 'exec', lambda *a: QMessageBox.StandardButton.Cancel)
            browser.delete_selected()
            assert (source / 'folder').exists()
            assert browser.operation is None and browser.local_job is None
            monkeypatch.setattr(QMessageBox, 'exec', lambda *a: QMessageBox.StandardButton.Yes)
            browser.delete_selected()
            drain(browser)
            assert not (source / 'folder').exists()
            assert (source / 'keep').read_text() == 'keep'
        finally:
            browser.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            owner.terminal.close()
            owner.close()


@pytest.mark.parametrize('local', [True, False])
def test_f4_uses_selected_external_editor(app, tmp_path, monkeypatch, local):
    from ssh_jumper.integrations import IntegrationSettings, SettingsStore
    from ssh_jumper.file_editor import ExternalEditor
    root = tmp_path / 'remote'
    root.mkdir()
    (root / 'file.ini').write_bytes(b'original')
    executable = tmp_path / 'editor.exe'
    executable.write_bytes(b'editor')
    executable.chmod(0o700)
    store = SettingsStore(tmp_path / 'settings.json')
    store.save(IntegrationSettings(str(executable), ['--new-window']))
    opened = []
    monkeypatch.setattr('ssh_jumper.integrations.launch_editor', lambda settings, path: opened.append((settings, path)))
    monkeypatch.setattr('ssh_jumper.file_editor.launch_editor', lambda settings, path: opened.append((settings, path)))
    with session_with_sftp(root) as (session, _):
        owner = QMainWindow()
        owner.settings_store = store
        owner.terminal, owner.session = TerminalWidget(), session
        browser = SftpBrowser(owner)
        try:
            drain(browser)
            browser.navigate_local(str(root))
            browser.navigate('/')
            drain(browser)
            browser.set_active(local)
            tree = browser.local_files if local else browser.files
            item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).text(0) == 'file.ini')
            tree.setCurrentItem(item)
            browser.view_file(edit=True)
            if local:
                assert not browser.viewers
                assert opened == [(store.load(), str(root / 'file.ini'))]
            else:
                editor = browser.viewers[0]
                assert isinstance(editor, ExternalEditor)
                assert editor.job.finished.wait(10)
                editor.poll()
                assert opened == [(store.load(), editor.local_path)]
                assert editor.local_path.read_bytes() == b'original'
                copy = editor.local_path
                editor.close()
                copy.unlink()
                copy.parent.rmdir()
        finally:
            browser.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            owner.terminal.close()
            owner.close()
