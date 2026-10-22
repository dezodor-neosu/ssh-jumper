"""External editor working copies with conflict-checked SFTP uploads."""
import os
import tempfile
from pathlib import Path
import posixpath
import stat
import uuid

import paramiko
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QPushButton, QLabel, QMessageBox
from .integrations import launch_editor
from .sftp_browser import SftpOperation
from .file_viewer import VIEW_LIMIT


class EditorOperation(SftpOperation):
    def __init__(self, path, transport=None, data=None, original=None):
        super().__init__(transport, 'read' if data is None else 'save', path)
        self.data, self.original = data, original

    def run(self):
        sftp, temporary = None, None
        try:
            self.check()
            if self.transport is not None:
                sftp = self.open_remote()
            path = self.directory
            info = sftp.lstat(path) if sftp else Path(path).lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Only regular files can be edited.')
            if info.st_size > VIEW_LIMIT:
                raise ValueError('Editing is limited to complete files up to 2 MiB. Use F3 to preview this file.')
            def read_current():
                with (sftp.open(path, 'rb') if sftp else Path(path).open('rb')) as stream:
                    return stream.read(VIEW_LIMIT + 1)
            current = read_current()
            self.check()
            if len(current) > VIEW_LIMIT:
                raise ValueError('File exceeds the 2 MiB editing limit.')
            if self.data is None:
                self.result = current
                return
            if len(self.data) > VIEW_LIMIT:
                raise ValueError('Edited content exceeds the 2 MiB limit.')
            if current != self.original:
                raise ValueError('File changed on disk/server. Save refused; your edits remain in the local working copy.')
            name = '.ssh-jumper-' + uuid.uuid4().hex + '.part'
            temporary = posixpath.join(posixpath.dirname(path), name) if sftp else str(Path(path).parent / name)
            with (sftp.open(temporary, 'wx') if sftp else Path(temporary).open('xb')) as stream:
                for offset in range(0, len(self.data), 65536):
                    self.check()
                    stream.write(self.data[offset:offset + 65536])
                if not sftp:
                    stream.flush()
                    os.fsync(stream.fileno())
            if sftp:
                sftp.chmod(temporary, stat.S_IMODE(info.st_mode))
            else:
                os.chmod(temporary, stat.S_IMODE(info.st_mode))
            self.check()
            if read_current() != self.original:
                raise ValueError('File changed during save. Original preserved; your edits remain in the local working copy.')
            if sftp:
                # No destructive truncate fallback on servers without atomic replacement.
                sftp.posix_rename(temporary, path)
            else:
                os.replace(temporary, path)
            temporary = None
            self.result = self.data
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            if temporary is not None:
                try:
                    sftp.remove(temporary) if sftp else Path(temporary).unlink(missing_ok=True)
                except (OSError, EOFError, paramiko.SSHException):
                    pass
            try:
                if sftp:
                    sftp.close()
                elif self.channel:
                    self.channel.close()
            finally:
                self.finished.set()


class ExternalEditor(QMainWindow):
    closed = Signal(object)

    def __init__(self, path, transport, settings, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("External edit - " + path)
        self.resize(700, 230)
        self.path, self.transport, self.settings = path, transport, settings
        self.original = None
        # Keep copies after closing: an editor can retain an open tab or save later.
        folder = Path(tempfile.mkdtemp(prefix="ssh-jumper-edit-"))
        name = posixpath.basename(path) if transport is not None else Path(path).name
        if not name or any(c in name for c in '<>:"/\\|?*') or name.endswith((' ', '.')):
            name = "remote-file.txt"
        self.local_path = folder / name
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)
        instructions = QLabel("Save in your external editor, then click Upload changes. Close the editor tab when finished. Working copies are retained at the path below, including after errors or closing SSH Jumper.")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        label = QLabel(str(self.local_path))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        self.open_button = QPushButton("Open in editor")
        self.open_button.clicked.connect(self.open_editor)
        layout.addWidget(self.open_button)
        self.save_button = QPushButton("Upload changes")
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.save_button)
        self.status = QLabel("Downloading working copy...")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.job = EditorOperation(path, transport)
        self.job.start()
        self.set_busy(True)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def set_busy(self, busy):
        self.save_button.setEnabled(not busy and self.original is not None)
        self.open_button.setEnabled(not busy and self.original is not None)

    def open_editor(self):
        try:
            launch_editor(self.settings, self.local_path)
            self.status.setText("Opened in editor. Save there, then upload changes here.")
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def save(self):
        if self.job is not None or self.original is None:
            return
        try:
            with self.local_path.open('rb') as stream:
                data = stream.read(VIEW_LIMIT + 1)
            if len(data) > VIEW_LIMIT:
                raise ValueError("Edited content exceeds the 2 MiB limit.")
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.job = EditorOperation(self.path, self.transport, data, self.original)
        self.set_busy(True)
        self.status.setText("Uploading changes...")
        self.job.start()

    def poll(self):
        if self.job is None or not self.job.finished.is_set():
            return
        job, self.job = self.job, None
        if job.error:
            self.status.setText(job.error)
        else:
            self.original = job.result
            if job.action == 'read':
                try:
                    self.local_path.write_bytes(self.original)
                except OSError as exc:
                    self.original = None
                    self.status.setText(str(exc))
                else:
                    self.open_editor()
            else:
                self.status.setText("Uploaded. Further saved edits can be uploaded again.")
        self.set_busy(False)

    def closeEvent(self, event):
        if self.job is not None and self.job.action == 'save':
            self.status.setText("Uploading; wait for completion before closing.")
            event.ignore()
            return
        if self.original is not None:
            try:
                changed = self.local_path.read_bytes() != self.original
            except OSError:
                changed = True
            if changed and QMessageBox.question(self, "Changes not uploaded",
                    "Close without uploading? Your local working copy will be retained at:\n" + str(self.local_path),
                    QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Close:
                event.ignore()
                return
        if self.job:
            self.job.cancel()
        self.timer.stop()
        self.closed.emit(self)
        super().closeEvent(event)
