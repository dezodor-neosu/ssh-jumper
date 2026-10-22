"""Read-only Vim-style viewer with bounded, name-following log snapshots."""
from pathlib import Path
import stat
import re

import paramiko
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QTextCursor, QTextDocument
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                              QPlainTextEdit, QLineEdit, QLabel, QPushButton)

from .sftp_browser import SftpOperation

VIEW_LIMIT = 2 * 1024 * 1024


class ReadSnapshot(SftpOperation):
    def __init__(self, path, transport, tail=False):
        super().__init__(transport, "read", path)
        self.tail = tail

    def run(self):
        sftp = None
        try:
            self.check()
            if self.transport is not None:
                sftp = self.open_remote()
                info = sftp.stat(self.directory)
                stream = lambda: sftp.open(self.directory, "rb")
            else:
                info = Path(self.directory).stat()
                stream = lambda: Path(self.directory).open("rb")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Only regular files can be viewed.")
            start = max(0, info.st_size - VIEW_LIMIT) if self.tail else 0
            with stream() as source:
                source.seek(start)
                blocks, remaining = [], VIEW_LIMIT
                while remaining:
                    self.check()
                    block = source.read(min(65536, remaining))
                    if not block:
                        break
                    blocks.append(block)
                    remaining -= len(block)
            data = b"".join(blocks)
            self.result = (data.decode("utf-8-sig", errors="replace"), start, info.st_size)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            try:
                if sftp is not None:
                    sftp.close()
                elif self.channel is not None:
                    self.channel.close()
            finally:
                self.finished.set()


class VimText(QPlainTextEdit):
    search_requested = Signal(bool)
    search_next = Signal(bool)
    follow_requested = Signal()
    close_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.count = ""
        self.pending_g = False

    def keyPressEvent(self, event):
        text = event.text()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        cursor = self.textCursor()
        moves = {"h": QTextCursor.MoveOperation.Left, "j": QTextCursor.MoveOperation.Down,
                 "k": QTextCursor.MoveOperation.Up, "l": QTextCursor.MoveOperation.Right,
                 "w": QTextCursor.MoveOperation.NextWord, "b": QTextCursor.MoveOperation.PreviousWord,
                 "e": QTextCursor.MoveOperation.EndOfWord, "0": QTextCursor.MoveOperation.StartOfLine,
                 "$": QTextCursor.MoveOperation.EndOfLine}
        if event.key() == Qt.Key.Key_Escape:
            self.count, self.pending_g = "", False
            return
        if not ctrl and text and text in "0123456789" and (text != "0" or self.count):
            self.count = (self.count + text)[-5:]
            return
        count = min(int(self.count or "1"), 10000)
        had_count = bool(self.count)
        if not ctrl and text == "g" and not self.pending_g:
            self.pending_g = True
            return
        if not ctrl and (text == "G" or (text == "g" and self.pending_g)):
            if had_count or text == "g":
                block = self.document().findBlockByNumber(count - 1)
                cursor.setPosition(block.position() if block.isValid() else self.document().characterCount() - 1)
            else:
                cursor.movePosition(QTextCursor.MoveOperation.End)
        elif not ctrl and text in ("w", "b", "e"):
            content = self.toPlainText()
            # QTextCursor uses UTF-16 offsets, while Python strings use code points.
            encoded = content.encode("utf-16-le")
            position = len(encoded[:cursor.position() * 2].decode("utf-16-le", errors="ignore"))
            tokens = list(re.finditer(r"\w+|[^\w\s]+", content))
            if text == "b":
                targets = [match.start() for match in tokens if match.start() < position][::-1]
                target = targets[min(count, len(targets)) - 1] if targets else 0
            else:
                targets = [match.start() if text == "w" else match.end() - 1 for match in tokens]
                targets = [target for target in targets if target > position]
                target = targets[min(count, len(targets)) - 1] if targets else len(content)
            cursor.setPosition(len(content[:target].encode("utf-16-le")) // 2)
        elif not ctrl and text in ("h", "l"):
            start = cursor.block().position()
            end = start + len(cursor.block().text().encode("utf-16-le")) // 2
            cursor.setPosition(max(start, cursor.position() - count) if text == "h" else min(end, cursor.position() + count))
        elif not ctrl and text in moves:
            cursor.movePosition(moves[text], n=count)
        elif not ctrl and text == "^":
            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
            line = cursor.block().text()
            cursor.movePosition(QTextCursor.MoveOperation.Right, n=len(line) - len(line.lstrip()))
        elif ctrl and event.key() in (Qt.Key.Key_D, Qt.Key.Key_U, Qt.Key.Key_F, Qt.Key.Key_B):
            rows = max(1, self.viewport().height() // self.fontMetrics().lineSpacing() - 1)
            if event.key() in (Qt.Key.Key_D, Qt.Key.Key_U):
                rows = max(1, rows // 2)
            down = event.key() in (Qt.Key.Key_D, Qt.Key.Key_F)
            cursor.movePosition(QTextCursor.MoveOperation.Down if down else QTextCursor.MoveOperation.Up, n=rows * count)
        elif not ctrl and text in ("/", "?"):
            self.search_requested.emit(text == "?")
        elif not ctrl and text in ("n", "N"):
            self.count, self.pending_g = "", False
            self.search_next.emit(text == "N")
            return
        elif not ctrl and text == "F":
            self.follow_requested.emit()
        elif not ctrl and text == "q":
            self.close_requested.emit()
            return
        else:
            self.count, self.pending_g = "", False
            super().keyPressEvent(event)
            return
        self.count, self.pending_g = "", False
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class FileViewer(QMainWindow):
    closed = Signal(object)

    def __init__(self, path, transport=None, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.path, self.transport = path, transport
        self.job = None
        self.search_backwards = False
        self.setWindowTitle("View · " + path)
        self.resize(1000, 700)
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)
        toolbar = QHBoxLayout()
        self.follow = QPushButton("F · Follow name (tail -F)")
        self.follow.setCheckable(True)
        self.reload_button = QPushButton("Reload")
        toolbar.addWidget(self.follow)
        toolbar.addWidget(self.reload_button)
        layout.addLayout(toolbar)
        self.text = VimText()
        layout.addWidget(self.text, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search text · Enter finds · Escape returns to viewer")
        self.search.hide()
        layout.addWidget(self.search)
        self.status = QLabel("Loading…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        help_text = QLabel("h j k l · w b e · 0 ^ $ · gg / G / 42G · Ctrl+d/u/f/b · / ? search · n N · F follow · q close | UTF-8, 2 MiB preview")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.text.search_requested.connect(self.begin_search)
        self.text.search_next.connect(self.find_next)
        self.text.follow_requested.connect(self.follow.toggle)
        self.text.close_requested.connect(self.close)
        self.search.returnPressed.connect(lambda: self.find_next(False))
        self.search.installEventFilter(self)
        self.follow.toggled.connect(self.reload)
        self.reload_button.clicked.connect(self.reload)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.follow_timer = QTimer(self)
        self.follow_timer.setInterval(1000)
        self.follow_timer.timeout.connect(lambda: self.reload() if self.follow.isChecked() else None)
        self.follow_timer.start()
        self.reload()
        self.text.setFocus()

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if watched is self.search and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.search.hide()
            self.text.setFocus()
            return True
        return super().eventFilter(watched, event)

    def begin_search(self, backwards):
        self.search_backwards = backwards
        self.search.show()
        self.search.setFocus()
        self.search.selectAll()

    def find_next(self, reverse=False):
        query = self.search.text()
        if not query:
            self.begin_search(False)
            return
        backward = self.search_backwards != reverse
        flags = QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        original = self.text.textCursor()
        if not self.text.find(query, flags):
            self.text.moveCursor(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
            if not self.text.find(query, flags):
                self.text.setTextCursor(original)
                self.status.setText("No match: " + query)
        self.text.setFocus()

    def reload(self):
        if self.job is None:
            self.job = ReadSnapshot(self.path, self.transport, self.follow.isChecked())
            self.job.start()

    def poll(self):
        if self.job is None or not self.job.finished.is_set():
            return
        job, self.job = self.job, None
        if job.error:
            self.status.setText(job.error + (" · following name; retrying…" if self.follow.isChecked() else ""))
            return
        content, start, size = job.result
        if content != self.text.toPlainText():
            position = self.text.textCursor().position()
            self.text.setPlainText(content)
            cursor = self.text.textCursor()
            cursor.setPosition(min(position, self.text.document().characterCount() - 1))
            self.text.setTextCursor(cursor)
        if self.follow.isChecked():
            self.text.moveCursor(QTextCursor.MoveOperation.End)
        self.status.setText(f"{'FOLLOW · ' if self.follow.isChecked() else ''}{size:,} bytes · "
                            + (f"preview from byte {start:,} (limited to 2 MiB)" if size > VIEW_LIMIT else "UTF-8"))
        if job.tail != self.follow.isChecked():
            self.reload()

    def closeEvent(self, event):
        self.timer.stop()
        self.follow_timer.stop()
        if self.job is not None:
            self.job.cancel()
        self.search.removeEventFilter(self)
        self.closed.emit(self)
        super().closeEvent(event)
