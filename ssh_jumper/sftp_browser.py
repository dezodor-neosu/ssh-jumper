"""Native SFTP browser. Each operation uses a channel on the terminal transport."""
from datetime import datetime
import os
from pathlib import Path, PurePosixPath
from html import escape
import posixpath
import re
import shlex
import stat
import threading
import uuid

import paramiko
from PySide6.QtCore import Qt, QTimer, QEvent, Signal, QItemSelectionModel
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QSplitter, QInputDialog, QMessageBox, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)


def safe_remote(path):
    if not path or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path):
        raise ValueError("Enter a remote path without control characters.")
    return path


def local_name(path):
    name = posixpath.basename(path)
    if (not name or name in (".", "..") or any(c in name for c in '<>:"/\\|?*')
            or name.endswith((".", " ")) or any(ord(c) < 32 for c in name)
            or getattr(os.path, "isreserved", lambda _: False)(name)):
        raise ValueError(f"Cannot save this filename on Windows: {name!r}")
    return name


PARENT_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2


def natural_key(name):
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r"(\d+)", name) if part)


class FileItem(QTreeWidgetItem):
    def __lt__(self, other):
        tree = self.treeWidget()
        descending = tree.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        def group(item):
            if item.data(0, PARENT_ROLE):
                return 0
            data = item.data(0, Qt.ItemDataRole.UserRole)
            return 1 if data and stat.S_ISDIR(data[1]) else 2
        left_group, right_group = group(self), group(other)
        if left_group != right_group:
            # Qt reverses the comparator in descending order; pin parent/folders.
            return left_group > right_group if descending else left_group < right_group
        column = tree.sortColumn()
        left = self.data(column, SORT_ROLE)
        right = other.data(column, SORT_ROLE)
        if column == 0:
            left, right = natural_key(self.text(0)), natural_key(other.text(0))
        elif column == 1:
            left, right = self.text(1).casefold(), other.text(1).casefold()
        else:
            left, right = left or 0, right or 0
        return (left, natural_key(self.text(0)), self.text(0)) < (right, natural_key(other.text(0)), other.text(0))


class FileTree(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.space_selection = False
        self.setSortingEnabled(True)
        self.sortItems(0, Qt.SortOrder.AscendingOrder)

    def clear(self):
        self.space_selection = False
        super().clear()

    def selectionCommand(self, index, event=None):
        if (self.space_selection and event is not None and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home,
                                    Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            return QItemSelectionModel.SelectionFlag.NoUpdate
        return super().selectionCommand(index, event)

    def mousePressEvent(self, event):
        self.space_selection = False
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            item = self.currentItem()
            if item is None:
                return
            if not self.space_selection and len(self.selectedItems()) <= 1:
                self.clearSelection()
            self.space_selection = True
            if not item.data(0, PARENT_ROLE):
                item.setSelected(not item.isSelected())
            following = self.itemBelow(item)
            while following is not None and following.isHidden():
                following = self.itemBelow(following)
            if following is not None:
                self.setCurrentItem(following, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.scrollToItem(following)
            event.accept()
            return
        super().keyPressEvent(event)


class PathBreadcrumb(QLabel):
    """Clickable ancestors; numeric link IDs keep filenames out of URLs."""
    pathActivated = Signal(str)

    def __init__(self):
        super().__init__()
        self.paths = []
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse |
                                     Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.linkActivated.connect(self.activate_part)

    def activate_part(self, link):
        if link.isdigit() and int(link) < len(self.paths):
            self.pathActivated.emit(self.paths[int(link)])

    def set_path(self, path, local=False):
        parsed = Path(path) if local else PurePosixPath(path)
        self.paths = []
        links = []
        current = type(parsed)()
        for index, part in enumerate(parsed.parts):
            current /= part
            self.paths.append(str(current))
            links.append(f'<a href="{index}">{escape(part)}</a>')
        separator = escape(os.sep if local else "/")
        # The root/drive anchor already contains its trailing separator.
        self.setText((links[0] + " " + (f" {separator} ").join(links[1:])) if links else "")
        self.setToolTip(str(parsed))


class SftpOperation:
    """No network or disk I/O on the GUI thread. One operation per window."""
    def __init__(self, transport, action, directory, paths=(), local_directory=None):
        self.transport, self.action = transport, action
        self.directory, self.paths = directory, list(paths)
        self.local_directory = local_directory
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.channel = None
        self.result = None
        self.error = None
        self.completed = []
        self.protocol = "SFTP"
        self.fallback_reason = None
        self.progress = "Opening SFTP…"
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled.set()
        channel = self.channel
        if channel is not None:
            threading.Thread(target=channel.close, daemon=True).start()

    def check(self):
        if self.cancelled.is_set():
            raise InterruptedError("Operation cancelled")

    def open_remote(self):
        """Fallback only during protocol negotiation; never replay a file operation."""
        self.channel = self.transport.open_session(timeout=10)
        self.channel.settimeout(15)
        self.check()
        try:
            self.channel.invoke_subsystem("sftp")
            return paramiko.SFTPClient(self.channel)
        except (paramiko.SSHException, EOFError, OSError) as exc:
            self.channel.close()
            self.check()
            if not self.transport.is_active():
                raise
            self.fallback_reason = str(exc) or type(exc).__name__
            self.protocol = "SCP"
            self.progress = "SFTP unavailable; trying SCP and remote shell…"
            from .scp_files import ScpFiles
            return ScpFiles(self)

    def copy(self, source, destination, size, name):
        count = 0
        while True:
            self.check()
            block = source.read(65536)
            if not block:
                break
            destination.write(block)
            count += len(block)
            self.progress = f"{name}: {count:,} / {size:,} bytes"
        self.check()
        return count

    def upload(self, sftp, path):
        local = Path(path)
        target = posixpath.join(self.directory, safe_remote(local.name))
        try:
            sftp.lstat(target)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"Already exists (not overwritten): {target}")
        temporary = posixpath.join(self.directory, f".ssh-jumper-{uuid.uuid4().hex}.part")
        created = False
        try:
            with local.open("rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Only regular files can be uploaded.")
                self.check()
                with sftp.open(temporary, "wx") as destination:
                    created = True
                    destination.set_pipelined(True)
                    count = self.copy(source, destination, info.st_size, local.name)
            if count != info.st_size or sftp.stat(temporary).st_size != count:
                raise OSError("Uploaded file size verification failed")
            self.check()
            # Standard SFTP rename fails if the destination already exists.
            sftp.rename(temporary, target)
            created = False
            self.completed.append(target)
        finally:
            if created:
                try:
                    sftp.remove(temporary)
                except (OSError, EOFError, paramiko.SSHException):
                    pass  # A dropped connection can leave a .part file.

    def download(self, sftp, remote):
        remote = safe_remote(remote)
        target = Path(self.local_directory) / local_name(remote)
        if target.exists():
            raise FileExistsError(f"Already exists (not overwritten): {target}")
        info = sftp.lstat(remote)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular files can be downloaded.")
        temporary = target.parent / f".ssh-jumper-{uuid.uuid4().hex}.part"
        try:
            with sftp.open(remote, "rb") as source, temporary.open("xb") as destination:
                count = self.copy(source, destination, info.st_size, target.name)
            if count != info.st_size:
                raise OSError("Source changed during download")
            self.check()
            if os.name == "nt":
                temporary.rename(target)  # Windows rename does not replace existing files.
            else:
                os.link(temporary, target)  # Atomic no-replace publication on POSIX.
                temporary.unlink()
            self.completed.append(str(target))
        finally:
            temporary.unlink(missing_ok=True)

    def transfer_tree(self, sftp, path, upload, move):
        self.check()
        info = Path(path).lstat() if upload else sftp.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symbolic links are not transferred; select their target instead.")
        if stat.S_ISDIR(info.st_mode):
            old_remote, old_local = self.directory, self.local_directory
            try:
                if upload:
                    target = posixpath.join(self.directory, safe_remote(Path(path).name))
                    sftp.mkdir(target)
                    self.directory = target
                    children = list(Path(path).iterdir())
                else:
                    target = Path(self.local_directory) / local_name(path)
                    target.mkdir()
                    self.local_directory = str(target)
                    children = []
                    for entry in sftp.listdir_iter(path):
                        self.check()
                        if entry.filename in (".", "..") or "/" in entry.filename:
                            continue
                        children.append(posixpath.join(path, entry.filename))
                for child in children:
                    self.transfer_tree(sftp, child, upload, move)
                self.check()
                if move:
                    Path(path).rmdir() if upload else sftp.rmdir(path)
            finally:
                self.directory, self.local_directory = old_remote, old_local
        elif stat.S_ISREG(info.st_mode):
            if upload:
                self.upload(sftp, path)
            else:
                self.download(sftp, path)
            self.check()
            if move:
                current = Path(path).lstat() if upload else sftp.lstat(path)
                if (current.st_size, current.st_mtime) != (info.st_size, info.st_mtime):
                    raise OSError("Source changed; copied file retained but source not removed")
                Path(path).unlink() if upload else sftp.remove(path)
        else:
            raise ValueError("Only regular files and directories can be transferred.")

    def delete_tree(self, path, sftp=None):
        self.check()
        info = sftp.lstat(path) if sftp else Path(path).lstat()
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if sftp:
                entries = list(sftp.listdir_iter(path))
                for entry in entries:
                    if entry.filename in (".", "..") or "/" in entry.filename:
                        continue
                    self.delete_tree(posixpath.join(path, entry.filename), sftp)
                self.check()
                sftp.rmdir(path)
            else:
                # Windows junctions must be removed as links, never traversed.
                if not Path(path).is_junction():
                    for child in Path(path).iterdir():
                        self.delete_tree(child)
                self.check()
                Path(path).rmdir()
        else:
            sftp.remove(path) if sftp else Path(path).unlink()
        self.completed.append(str(path))
        self.progress = f"{len(self.completed)} item(s) permanently deleted"

    def run(self):
        sftp = None
        try:
            self.check()
            if self.action.startswith("local_"):
                if self.action == "local_list":
                    directory = Path(self.directory).expanduser().resolve()
                    entries = []
                    for child in directory.iterdir():
                        self.check()
                        info = paramiko.SFTPAttributes.from_stat(child.lstat())
                        info.filename = child.name
                        entries.append(info)
                    entries.sort(key=lambda e: (not stat.S_ISDIR(e.st_mode), e.filename.casefold()))
                    self.result = (str(directory), entries)
                elif self.action == "local_delete":
                    for path in self.paths:
                        self.delete_tree(path)
                elif self.action == "local_mkdir":
                    Path(self.directory).mkdir()
                elif self.action == "local_touch":
                    with Path(self.directory).open("xb"):
                        pass
                if self.action != "local_delete":
                    self.progress = "Done"
                return
            sftp = self.open_remote()
            if self.action == "list":
                directory = sftp.normalize(safe_remote(self.directory))
                entries = []
                for entry in sftp.listdir_iter(directory):
                    self.check()
                    # Entries must be basenames, even if a server misbehaves.
                    if entry.filename not in (".", "..") and "/" not in entry.filename:
                        entries.append(entry)
                entries.sort(key=lambda entry: (not stat.S_ISDIR(entry.st_mode or 0), entry.filename.casefold()))
                self.result = (directory, entries)
            elif self.action == "delete":
                for path in self.paths:
                    self.delete_tree(path, sftp)
            elif self.action == "mkdir":
                sftp.mkdir(safe_remote(self.directory))
            elif self.action == "touch":
                with sftp.open(safe_remote(self.directory), "wx"):
                    pass
            else:
                for path in self.paths:
                    self.check()
                    if self.action in ("upload", "move_upload"):
                        self.transfer_tree(sftp, path, True, self.action == "move_upload")
                    elif self.action in ("download", "move_download"):
                        self.transfer_tree(sftp, path, False, self.action == "move_download")
                    else:
                        raise ValueError("Unknown SFTP operation")
            if self.action in ("mkdir", "touch"):
                self.progress = "Created " + self.directory
            elif self.action != "delete":
                self.progress = f"{len(self.completed)} file(s) transferred" if self.action != "list" else "Folder loaded"
        except Exception as exc:
            self.error = "Operation cancelled" if self.cancelled.is_set() else str(exc) or type(exc).__name__
            if self.fallback_reason and not self.cancelled.is_set():
                self.error = (f"SFTP unavailable ({self.fallback_reason}); SCP/shell fallback failed: {self.error}. "
                              "Fallback requires a POSIX shell, stat or long-format ls, and scp on the device.")
            self.progress = self.error
            if self.completed:
                self.progress += f"; {len(self.completed)} file(s) already completed"
        finally:
            try:
                if sftp is not None:
                    sftp.close()
                elif self.channel is not None:
                    self.channel.close()
            except (OSError, EOFError, paramiko.SSHException):
                pass
            finally:
                self.finished.set()


class SftpBrowser(QMainWindow):
    def __init__(self, terminal_window):
        super().__init__(terminal_window, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.owner = terminal_window
        self.terminal, self.session = terminal_window.terminal, terminal_window.session
        self.directory = None
        self.operation = None
        self.probe = None
        self.after_listing = None
        self.setWindowTitle("SFTP files · " + terminal_window.windowTitle())
        self.resize(1200, 650)
        self.local_directory = str(Path.home())
        self.local_job = None
        self.active_local = False
        self.remote_protocol = "SFTP"
        self.viewers = []
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)
        navigation = QHBoxLayout()
        self.up = QPushButton("Up")
        self.address = QLineEdit()
        self.address.setPlaceholderText("Remote folder")
        self.go = QPushButton("Go")
        self.refresh = QPushButton("Refresh")
        for widget in (self.up, self.address, self.go, self.refresh):
            navigation.addWidget(widget)
        splitter = QSplitter()
        local_panel = QWidget()
        local_layout = QVBoxLayout(local_panel)
        self.local_label = QLabel("Local")
        local_layout.addWidget(self.local_label)
        local_nav = QHBoxLayout()
        self.local_address = QLineEdit(self.local_directory)
        self.local_up = QPushButton("Up")
        self.local_refresh = QPushButton("Refresh")
        for widget in (self.local_up, self.local_address, self.local_refresh):
            local_nav.addWidget(widget)
        local_layout.addLayout(local_nav)
        self.local_breadcrumb = PathBreadcrumb()
        self.local_breadcrumb.pathActivated.connect(lambda path: self.breadcrumb_navigate(path, True))
        local_layout.addWidget(self.local_breadcrumb)
        self.local_files = FileTree()
        self.local_files.setHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.local_files.setRootIsDecorated(False)
        self.local_files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_files.setColumnWidth(0, 260)
        self.local_filter = QLineEdit()
        self.local_filter.setPlaceholderText("Fuzzy find in this folder · Ctrl+P")
        local_layout.addWidget(self.local_filter)
        local_layout.addWidget(self.local_files)
        remote_panel = QWidget()
        remote_layout = QVBoxLayout(remote_panel)
        self.remote_label = QLabel("Remote · SFTP · active")
        remote_layout.addWidget(self.remote_label)
        remote_layout.addLayout(navigation)
        self.remote_breadcrumb = PathBreadcrumb()
        self.remote_breadcrumb.pathActivated.connect(lambda path: self.breadcrumb_navigate(path, False))
        remote_layout.addWidget(self.remote_breadcrumb)
        splitter.addWidget(local_panel)
        splitter.addWidget(remote_panel)
        splitter.setSizes([600, 600])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        self.files = FileTree()
        self.files.setHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.files.setRootIsDecorated(False)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.setColumnWidth(0, 400)
        self.remote_filter = QLineEdit()
        self.remote_filter.setPlaceholderText("Fuzzy find in this folder · Ctrl+P")
        remote_layout.addWidget(self.remote_filter)
        remote_layout.addWidget(self.files, 1)
        self.files.setColumnWidth(0, 210)
        self.local_files.setColumnWidth(0, 210)
        for tree in (self.files, self.local_files):
            tree.setColumnWidth(1, 65)
            tree.setColumnWidth(2, 80)
        actions = QHBoxLayout()
        self.upload_button = QPushButton("Upload files…")
        self.download_button = QPushButton("Download selected…")
        self.from_terminal = QPushButton("Terminal → Files")
        self.to_terminal = QPushButton("Files → Terminal")
        self.cancel_button = QPushButton("Cancel")
        for widget in (self.upload_button, self.download_button, self.from_terminal, self.to_terminal, self.cancel_button):
            actions.addWidget(widget)
        layout.addLayout(actions)
        commands = QHBoxLayout()
        self.command_buttons = []
        for key, title, callback in (("F3", "View", self.view_file), ("F4", "Edit", lambda: self.view_file(edit=True)),
                ("F5", "Copy", lambda: self.transfer(False)), ("F6", "Move", lambda: self.transfer(True)),
                ("F7", "Mkdir", lambda: self.create_entry(True)), ("F8", "Delete", self.delete_selected),
                ("F9", "New file", lambda: self.create_entry(False))):
            button = QPushButton(f"{key} {title}")
            button.clicked.connect(callback)
            commands.addWidget(button)
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.command_buttons.append(button)
        layout.addLayout(commands)
        self.local_filter.textChanged.connect(lambda text: self.filter_files(self.local_files, text))
        self.remote_filter.textChanged.connect(lambda text: self.filter_files(self.files, text))
        self.local_filter.installEventFilter(self)
        self.remote_filter.installEventFilter(self)
        finder = QShortcut(QKeySequence("Ctrl+P"), self)
        finder.activated.connect(lambda: (self.local_filter if self.active_local else self.remote_filter).setFocus())
        self.local_files.installEventFilter(self)
        self.files.installEventFilter(self)
        self.local_files.itemDoubleClicked.connect(self.activate_local)
        self.local_address.returnPressed.connect(lambda: self.navigate_local(self.local_address.text()))
        self.local_up.clicked.connect(lambda: self.navigate_local(str(Path(self.local_directory).parent)))
        self.local_refresh.clicked.connect(lambda: self.navigate_local(self.local_directory))
        hint = QLabel("CWD sync: use an empty shell prompt. Basic device shells ending in $ or # are supported. Files use the original SSH login, even after sudo or nested ssh. Existing files are not overwritten.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.status = QLabel("Connecting…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.go.clicked.connect(lambda: self.navigate(self.address.text()))
        self.address.returnPressed.connect(lambda: self.navigate(self.address.text()))
        self.refresh.clicked.connect(lambda: self.navigate(self.directory or "."))
        self.up.clicked.connect(lambda: self.navigate(posixpath.dirname(self.directory or "/")))
        self.files.itemDoubleClicked.connect(self.activate)
        self.files.itemSelectionChanged.connect(self.update_controls)
        self.upload_button.clicked.connect(self.upload)
        self.download_button.clicked.connect(self.download)
        self.from_terminal.clicked.connect(self.sync_from_terminal)
        self.to_terminal.clicked.connect(self.sync_to_terminal)
        self.cancel_button.clicked.connect(self.cancel)
        self.terminal.cwd_received.connect(self.cwd_received)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.probe_timer = QTimer(self)
        self.probe_timer.setSingleShot(True)
        self.probe_timer.timeout.connect(self.probe_timeout)
        self.navigate_local(self.local_directory)
        self.navigate(".")

    def update_controls(self):
        available = self.operation is None and self.probe is None and self.session.status == "Connected"
        for control in (self.up, self.address, self.go, self.refresh, self.files, self.from_terminal, self.remote_breadcrumb):
            control.setEnabled(available)
        for control in (self.upload_button, self.to_terminal):
            control.setEnabled(available and self.directory is not None)
        self.download_button.setEnabled(available and bool(self.selected_entries(self.files)))
        self.cancel_button.setEnabled(self.operation is not None or self.probe is not None or self.local_job is not None)
        for button in self.command_buttons:
            button.setEnabled(available and self.local_job is None)
        for widget in (self.local_files, self.local_address, self.local_up, self.local_refresh, self.local_breadcrumb, self.local_filter, self.remote_filter):
            widget.setEnabled(self.operation is None and self.local_job is None)

    def start(self, action, directory, paths=(), local_directory=None):
        if self.operation is not None or self.probe is not None:
            return
        if self.session.status != "Connected" or self.session.channel is None:
            self.status.setText("SSH connection is closed. Reopen the terminal to reconnect.")
            self.update_controls()
            return
        self.operation = SftpOperation(self.session.channel.get_transport(), action, directory, paths, local_directory)
        self.status.setText(self.operation.progress)
        self.operation.start()
        self.update_controls()

    def navigate(self, path):
        try:
            path = safe_remote(path)
            if self.directory and not path.startswith("/"):
                path = posixpath.join(self.directory, path)
            self.start("list", path)
        except ValueError as exc:
            self.status.setText(str(exc))

    def activate(self, item, column):
        path, mode = item.data(0, Qt.ItemDataRole.UserRole)
        if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            self.navigate(path)
        else:
            self.set_active(False)
            self.view_file()

    def upload(self):
        if self.operation is not None or self.probe is not None or not self.directory:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        if paths:
            self.start("upload", self.directory, paths)

    def download(self):
        if self.operation is not None or self.probe is not None:
            return
        selected = self.selected_entries(self.files)
        if not selected:
            return
        directory = QFileDialog.getExistingDirectory(self, "Download selected files into")
        if directory:
            self.start("download", self.directory, [path for path, _ in selected], directory)

    def set_active(self, local):
        self.active_local = local
        self.local_label.setText("Local" + (" · active" if local else ""))
        self.remote_label.setText("Remote · " + self.remote_protocol + (" · active" if not local else ""))

    @staticmethod
    def filter_files(tree, query):
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            letters = iter(item.text(0).casefold())
            matched = bool(item.data(0, PARENT_ROLE)) or all(letter in letters for letter in query.casefold())
            item.setHidden(not matched)
            if not matched:
                item.setSelected(False)
        if tree.currentItem() is None or tree.currentItem().isHidden() or (query and tree.currentItem().data(0, PARENT_ROLE)):
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                if not item.isHidden() and not (query and item.data(0, PARENT_ROLE)):
                    tree.setCurrentItem(item)
                    break

    def eventFilter(self, watched, event):
        if watched in (self.local_filter, self.remote_filter):
            if event.type() == QEvent.Type.FocusIn:
                self.set_active(watched is self.local_filter)
            if event.type() == QEvent.Type.KeyPress:
                tree = self.local_files if watched is self.local_filter else self.files
                if event.key() == Qt.Key.Key_Escape:
                    watched.clear()
                    tree.setFocus()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Down):
                    tree.setFocus()
                    return True
        if watched in (self.local_files, self.files):
            if event.type() == QEvent.Type.FocusIn:
                self.set_active(watched is self.local_files)
            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Tab:
                    tree = self.files if watched is self.local_files else self.local_files
                    tree.setFocus()
                    if tree.currentItem() is None and tree.topLevelItemCount():
                        tree.setCurrentItem(tree.topLevelItem(0))
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and watched.currentItem():
                    if watched is self.local_files:
                        self.activate_local(watched.currentItem(), 0)
                    else:
                        self.activate(watched.currentItem(), 0)
                    return True
                if event.key() == Qt.Key.Key_Backspace:
                    if watched is self.local_files:
                        self.navigate_local(str(Path(self.local_directory).parent))
                    else:
                        self.navigate(posixpath.dirname(self.directory or "/"))
                    return True
        return super().eventFilter(watched, event)

    def breadcrumb_navigate(self, path, local):
        self.set_active(local)
        if local:
            self.navigate_local(path)
        else:
            self.navigate(path)

    @staticmethod
    def add_parent(tree, directory, local=False):
        parent = str(Path(directory).parent) if local else posixpath.dirname(directory)
        if parent == directory:
            return
        item = FileItem(["..", "Parent folder", "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, (parent, stat.S_IFDIR))
        item.setData(0, PARENT_ROLE, True)
        tree.addTopLevelItem(item)

    @staticmethod
    def selected_entries(tree):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in tree.selectedItems()
                if not item.isHidden() and not item.data(0, PARENT_ROLE)]

    def selection(self):
        tree = self.local_files if self.active_local else self.files
        return self.selected_entries(tree)

    def navigate_local(self, path):
        if self.local_job is not None:
            return
        path = str(Path(self.local_directory) / Path(path).expanduser())
        self.local_job = SftpOperation(None, "local_list", path)
        self.local_job.start()

    def activate_local(self, item, column):
        self.set_active(True)
        path, mode = item.data(0, Qt.ItemDataRole.UserRole)
        if stat.S_ISDIR(mode):
            self.navigate_local(path)
        else:
            self.view_file()

    def transfer(self, move):
        if self.operation or self.local_job or self.probe or not self.directory:
            return
        selected = self.selection()
        if not selected:
            self.status.setText("Select files or folders in the source panel first.")
            return
        action = "upload" if self.active_local else "download"
        self.start(("move_" if move else "") + action, self.directory,
                   [path for path, _ in selected], self.local_directory)

    def create_entry(self, folder):
        if self.operation or self.local_job or self.probe:
            return
        local = self.active_local
        name, accepted = QInputDialog.getText(self, "Create folder" if folder else "Create file", "Name:")
        if not accepted:
            return
        try:
            safe_remote(name)
            if name in (".", "..") or "/" in name or "\\" in name:
                raise ValueError("Enter a single filename.")
            if local:
                local_name(name)
                self.local_job = SftpOperation(None, "local_mkdir" if folder else "local_touch", str(Path(self.local_directory) / name))
                self.local_job.start()
            elif self.directory:
                self.start("mkdir" if folder else "touch", posixpath.join(self.directory, name))
        except ValueError as exc:
            self.status.setText(str(exc))

    def delete_selected(self):
        if self.operation or self.local_job or self.probe:
            return
        selected = self.selection()
        if not selected:
            self.status.setText("Select files or folders to delete.")
            return
        local = self.active_local
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Permanently delete")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"Permanently delete {len(selected)} selected item(s) from {'Local' if local else 'Remote'}?\nFolders and their contents will be removed. This cannot be undone.")
        dialog.setDetailedText("\n".join(path for path, _ in selected))
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            return
        paths = [path for path, _ in selected]
        if local:
            self.local_job = SftpOperation(None, "local_delete", self.local_directory, paths)
            self.local_job.start()
            self.update_controls()
        else:
            self.start("delete", self.directory, paths)

    def view_file(self, edit=False):
        selected = self.selection()
        if len(selected) != 1 or not stat.S_ISREG(selected[0][1]):
            self.status.setText("Select one regular file to view.")
            return
        if not self.active_local and (self.session.status != "Connected" or self.session.channel is None):
            self.status.setText("SSH connection is closed.")
            return
        from .file_viewer import FileViewer
        transport = None if self.active_local else self.session.channel.get_transport()
        if edit:
            from .file_editor import ExternalEditor
            from .integrations import IntegrationSettings, executable, launch_editor
            owner = self.owner
            while owner is not None and not hasattr(owner, 'settings_store'):
                owner = owner.parent()
            try:
                settings = owner.settings_store.load() if owner else IntegrationSettings()
                executable(settings.editor_path)
                if self.active_local:
                    launch_editor(settings, selected[0][0])
                    self.status.setText("Opened in external editor.")
                    return
                viewer = ExternalEditor(selected[0][0], transport, settings, self)
            except (ValueError, OSError, TypeError) as exc:
                self.status.setText(str(exc))
                return
        else:
            viewer = FileViewer(selected[0][0], transport, self)
        self.viewers.append(viewer)
        viewer.closed.connect(self.viewers.remove)
        viewer.show()

    def shell_ready(self):
        if self.operation is not None or self.probe is not None or self.session.status != "Connected":
            return False
        screen = self.terminal.screen
        row = screen.buffer[screen.cursor.y]
        before = ''.join(row[x].data for x in range(min(screen.cursor.x, screen.columns))).rstrip()
        after = ''.join(row[x].data for x in range(screen.cursor.x, screen.columns)).strip()
        basic_prompt = before.endswith(('$', '#')) and not after
        if (screen.primary is not None or self.terminal.input_pending
                or (2004 << 5 not in screen.mode and not basic_prompt)):
            self.status.setText("Return to an empty shell prompt first (basic prompts must end in $ or #). You can also enter the folder in the address bar.")
            return False
        return True

    def send_shell_command(self, command):
        if 2004 << 5 in self.terminal.screen.mode:
            command = "\x1b[200~" + command + "\x1b[201~"
        return self.session.send((command + "\r").encode())

    def sync_from_terminal(self):
        if not self.shell_ready():
            return
        self.probe = uuid.uuid4().hex
        command = f" printf '\\033]777;ssh-jumper-cwd;{self.probe};%s\\007' \"$(pwd)\""
        if not self.send_shell_command(command):
            self.probe = None
            self.status.setText("Could not send the directory query.")
            return
        self.terminal.setEnabled(False)
        self.status.setText("Reading terminal working directory…")
        self.probe_timer.start(5000)
        self.update_controls()

    def clear_probe(self):
        self.probe_timer.stop()
        self.probe = None
        self.terminal.setEnabled(True)
        self.update_controls()

    def cwd_received(self, token, path):
        if self.probe is None or token != self.probe:
            return
        self.clear_probe()
        if not path.startswith("/"):
            self.status.setText("The shell did not report an absolute directory.")
            return
        self.navigate(path)

    def probe_timeout(self):
        self.clear_probe()
        self.status.setText("No directory reply from the shell. Enter the remote folder in the address bar.")

    def sync_to_terminal(self):
        if not self.directory or not self.shell_ready():
            return
        try:
            directory = safe_remote(self.directory)
            command = " cd " + shlex.quote('./' + directory if directory.startswith('-') else directory)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if self.send_shell_command(command):
            self.status.setText("Sent cd to terminal; check the shell prompt for the result.")
        else:
            self.status.setText("Could not send cd to the terminal.")

    def cancel(self):
        if self.local_job is not None:
            self.local_job.cancel()
        if self.probe is not None:
            self.clear_probe()
            self.status.setText("Directory sync cancelled.")
        if self.operation is not None:
            self.operation.cancel()

    def poll(self):
        if self.local_job is not None and self.local_job.finished.is_set():
            job, self.local_job = self.local_job, None
            if job.error:
                self.status.setText(job.error)
                self.local_address.setText(self.local_directory)
                if job.action == "local_delete":
                    self.navigate_local(self.local_directory)
            elif job.action == "local_list":
                self.local_directory, entries = job.result
                self.local_address.setText(self.local_directory)
                self.local_breadcrumb.set_path(self.local_directory, local=True)
                self.local_files.clear()
                self.add_parent(self.local_files, self.local_directory, local=True)
                for entry in entries:
                    item = FileItem([entry.filename, "Folder" if stat.S_ISDIR(entry.st_mode) else "File",
                        str(entry.st_size), datetime.fromtimestamp(entry.st_mtime).strftime("%Y-%m-%d %H:%M")])
                    item.setData(0, Qt.ItemDataRole.UserRole, (str(Path(self.local_directory) / entry.filename), entry.st_mode))
                    item.setData(2, SORT_ROLE, entry.st_size or 0)
                    item.setData(3, SORT_ROLE, entry.st_mtime or 0)
                    self.local_files.addTopLevelItem(item)
                self.filter_files(self.local_files, self.local_filter.text())
            else:
                self.status.setText(job.progress)
                self.navigate_local(self.local_directory)
        self.update_controls()
        job = self.operation
        if job is None:
            if self.session.status != "Connected":
                self.status.setText("SSH connection is closed. Reopen the terminal to reconnect.")
            return
        self.status.setText(job.progress)
        if not job.finished.is_set():
            return
        self.remote_protocol = job.protocol
        self.set_active(self.active_local)
        self.remote_label.setToolTip("SFTP unavailable: " + job.fallback_reason if job.fallback_reason else "")
        self.operation = None
        if job.action == "list":
            if not job.error:
                self.directory, entries = job.result
                self.address.setText(self.directory)
                self.remote_breadcrumb.set_path(self.directory)
                self.files.clear()
                self.add_parent(self.files, self.directory)
                for entry in entries:
                    mode = entry.st_mode or 0
                    kind = "Folder" if stat.S_ISDIR(mode) else "Link" if stat.S_ISLNK(mode) else "File"
                    modified = ""
                    try:
                        if entry.st_mtime is not None:
                            modified = datetime.fromtimestamp(entry.st_mtime).strftime("%Y-%m-%d %H:%M")
                    except (OSError, ValueError, OverflowError):
                        pass
                    item = FileItem([entry.filename, kind, "" if kind == "Folder" else f"{entry.st_size or 0:,}", modified])
                    item.setData(0, Qt.ItemDataRole.UserRole, (posixpath.join(self.directory, entry.filename), mode))
                    item.setData(2, SORT_ROLE, entry.st_size or 0)
                    item.setData(3, SORT_ROLE, entry.st_mtime or 0)
                    self.files.addTopLevelItem(item)
                self.filter_files(self.files, self.remote_filter.text())
                self.status.setText(self.after_listing or f"{len(entries)} items")
            else:
                self.address.setText(self.directory or "")
            self.after_listing = None
        elif job.action != "list":
            self.navigate_local(self.local_directory)
            self.after_listing = job.progress
            self.navigate(self.directory)
        self.update_controls()

    def closeEvent(self, event):
        for viewer in list(self.viewers):
            if not viewer.close():
                event.ignore()
                return
        for widget in (self.local_files, self.files, self.local_filter, self.remote_filter):
            widget.removeEventFilter(self)
        self.cancel()
        self.timer.stop()
        self.probe_timer.stop()
        super().closeEvent(event)
