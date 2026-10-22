"""Native Qt terminal, with a VT screen and an independently owned SSH session."""
from collections import deque
import codecs
import copy
import queue
import re
import socket
import threading
import time
from functools import lru_cache

import pyte
from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget, QPushButton

from .models import HostEndpoint, Profile
from .tunnel import Tunnel


class TerminalStream(pyte.Stream):
    """Frame xterm extensions before passing supported VT sequences to pyte.

    pyte treats some private/intermediate parameters as ordinary commands, and
    its fixed-arity callbacks can raise on valid extensions used by Vim.
    Unsupported complete sequences are consumed, never rendered as text.
    """
    def __init__(self, screen):
        super().__init__(screen)
        self.use_utf8 = False  # Stream's flag only controls G0/G1 escape handling.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "text"
        self.sequence = ""
        self.string_escape = False
        self.string_kind = ""
        self.overflow = False

    def feed(self, data):
        text = self.decoder.decode(data)
        pos = 0
        while pos < len(text):
            char = text[pos]
            pos += 1
            if self.state == "text":
                if char in "\x1b\x9b\x9d\x90\x9e\x9f":
                    self.sequence = ""
                    if char == "\x1b":
                        self.state = "escape"
                    elif char == "\x9b":
                        self.state = "csi"
                    else:
                        self.begin_string({"\x9d": "]", "\x90": "P", "\x9e": "^", "\x9f": "_"}[char])
                else:
                    end = pos
                    while end < len(text) and text[end] not in "\x1b\x9b\x9d\x90\x9e\x9f":
                        end += 1
                    super().feed(text[pos - 1:end])
                    pos = end
            elif self.state == "escape":
                if char == "[":
                    self.state = "csi"
                elif char in "]P^_X":
                    self.begin_string(char)
                elif char in "()*+%#":
                    self.sequence = char
                    self.state = "charset"
                else:
                    if char in self.escape:
                        super().feed("\x1b" + char)
                    self.state = "text"
            elif self.state == "charset":
                if self.sequence in "()#":
                    super().feed("\x1b" + self.sequence + char)
                self.state = "text"
            elif self.state == "csi":
                if char in "\x18\x1a":
                    self.state = "text"
                elif char == "\x1b":
                    self.state, self.sequence = "escape", ""
                elif "@" <= char <= "~":
                    self.dispatch_csi(self.sequence, char)
                    self.state = "text"
                elif len(self.sequence) < 1024:
                    self.sequence += char
                else:
                    self.state = "discard_csi"
            elif self.state == "discard_csi":
                if "@" <= char <= "~":
                    self.state = "text"
            else:  # OSC/DCS/APC/PM/SOS, including split ST terminators.
                if char == "\x9c" or (self.string_escape and char == "\\") or (char == "\x07" and self.string_kind == "]"):
                    if not self.overflow:
                        self.dispatch_string()
                    self.state = "text"
                    self.string_escape = False
                elif char in "\x18\x1a":
                    self.state = "text"
                elif char == "\x1b":
                    self.string_escape = True
                else:
                    self.string_escape = False
                    if len(self.sequence) < 4096:
                        self.sequence += char
                    else:
                        self.overflow = True

    def begin_string(self, kind):
        self.state, self.string_kind = "string", kind
        self.sequence, self.string_escape, self.overflow = "", False, False

    def dispatch_string(self):
        value = self.sequence
        if self.string_kind == "]":
            number, _, payload = value.partition(";")
            if number in ("10", "11") and payload == "?":
                rgb = "d5d5/dcdc/e5e5" if number == "10" else "1010/1515/1d1d"
                self.listener.write_process_input(f"\x1b]{number};rgb:{rgb}\x1b\\")
            elif number == "777" and payload.startswith("ssh-jumper-cwd;"):
                fields = payload.split(";", 2)
                if len(fields) == 3 and re.fullmatch(r"[0-9a-f]{32}", fields[1]):
                    self.listener.cwd_received(fields[1], fields[2])
        elif self.string_kind == "P":
            if value.startswith("+q"):
                # Negative XTGETTCAP replies keep Vim's normal terminfo keys.
                for capability in value[2:].split(";"):
                    if re.fullmatch(r"[0-9a-fA-F]{2,128}", capability):
                        self.listener.write_process_input(f"\x1bP0+r{capability}\x1b\\")
            elif value.startswith("$q"):
                self.listener.write_process_input("\x1bP0$r\x1b\\")

    def dispatch_csi(self, parameters, final):
        if parameters.startswith(">"):
            if final == "c":
                self.listener.write_process_input("\x1b[>0;0;0c")
            # Extended keyboard protocols are not advertised or enabled.
            return
        if parameters == "?4" and final == "m":
            self.listener.write_process_input("\x1b[>4;0m")
            return
        if parameters.endswith("$") and final == "p":
            mode = parameters[:-1]
            if re.fullmatch(r"\??\d{1,4}", mode):
                self.listener.write_process_input(f"\x1b[{mode};0$y")
            return
        private = parameters.startswith("?")
        raw = parameters[1:] if private else parameters
        if not re.fullmatch(r"[0-9;]*", raw):
            return  # Includes cursor styles, colon SGR and unknown extensions.
        if private and final not in "hlcn":
            return
        parts = raw.split(";")
        if len(parts) > 32 or any(len(p) > 5 for p in parts):
            return
        # Validate arity instead of allowing pyte callbacks to throw TypeError.
        limit = 32 if final in "hlm" else 2 if final in "Hfr" else 1
        if len(parts) > limit:
            return
        if final in "ST" and not private:
            cursor = copy.copy(self.listener.cursor)
            top, bottom = self.listener.margins or (0, self.listener.lines - 1)
            self.listener.cursor.y = bottom if final == "S" else top
            for _ in range(min(int(raw or 1) or 1, self.listener.lines)):
                (self.listener.index if final == "S" else self.listener.reverse_index)()
            self.listener.cursor = cursor
        elif final == "s" and not raw:
            self.listener.save_cursor()
        elif final == "u" and not raw:
            self.listener.restore_cursor()
        elif final in self.csi:
            super().feed("\x1b[" + parameters + final)


class TerminalScreen(pyte.Screen):
    """Add xterm alternate buffers and bounded scrollback to pyte's VT parser."""
    def __init__(self, columns=80, lines=24, reply=lambda data: None, cwd_received=lambda token, path: None):
        self.reply = reply
        self.cwd_received = cwd_received
        self.history = deque(maxlen=2000)
        self.primary = None
        super().__init__(columns, lines)

    def write_process_input(self, data):
        self.reply(data.encode("utf-8"))

    def report_device_status(self, mode, **kwargs):
        if mode == 5 and not kwargs.get("private"):
            self.write_process_input("\x1b[0n")
        elif mode == 6:
            y = self.cursor.y + 1
            if 6 << 5 in self.mode and self.margins is not None:
                y -= self.margins.top
            prefix = "?" if kwargs.get("private") else ""
            self.write_process_input(f"\x1b[{prefix}{y};{min(self.cursor.x + 1, self.columns)}R")

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            if 1048 in modes:
                self.save_cursor()
            if any(mode in modes for mode in (47, 1047, 1049)) and self.primary is None:
                self.primary = (self.buffer, copy.deepcopy(self.cursor), self.margins, self.savepoints)
                fresh = pyte.Screen(self.columns, self.lines)
                self.buffer, self.cursor = fresh.buffer, fresh.cursor
                self.margins, self.savepoints = None, []
                self.dirty.update(range(self.lines))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private"):
            if any(mode in modes for mode in (47, 1047, 1049)) and self.primary is not None:
                self.buffer, self.cursor, self.margins, self.savepoints = self.primary
                self.primary = None
                self.cursor.x = min(self.cursor.x, self.columns - 1)
                self.cursor.y = min(self.cursor.y, self.lines - 1)
                self.margins = None
                self.dirty.update(range(self.lines))
            if 1048 in modes:
                self.restore_cursor()
        super().reset_mode(*modes, **kwargs)

    def index(self):
        if self.primary is None and self.margins is None and self.cursor.y == self.lines - 1:
            self.history.append(copy.copy(self.buffer[0]))
        super().index()

    def resize(self, lines=None, columns=None):
        if columns is not None and columns > self.columns:
            self.tabstops.update(x for x in range(self.columns, columns) if x % 8 == 0)
        if self.primary is not None:
            alternate = (self.buffer, self.cursor, self.margins, self.savepoints)
            old_size = (self.lines, self.columns)
            self.buffer, self.cursor, self.margins, self.savepoints = self.primary
            super().resize(lines=lines, columns=columns)
            self.cursor.x = min(self.cursor.x, self.columns - 1)
            self.cursor.y = min(self.cursor.y, self.lines - 1)
            self.primary = (self.buffer, self.cursor, self.margins, self.savepoints)
            self.buffer, self.cursor, self.margins, self.savepoints = alternate
            self.lines, self.columns = old_size
        super().resize(lines=lines, columns=columns)
        self.cursor.x = min(self.cursor.x, self.columns - 1)
        self.cursor.y = min(self.cursor.y, self.lines - 1)

    def reset(self):
        self.primary = None
        self.history.clear()
        super().reset()


class TerminalSession:
    """Network work never runs on Qt's GUI thread; queues bound pending output."""
    def __init__(self, forward, ignore_host_keys, columns=80, lines=24):
        forward.validate()
        if forward.ssh is None:
            raise ValueError("Enable SSH authentication for this forward first.")
        auth = forward.ssh
        self.endpoint = HostEndpoint(forward.target_host, forward.target_port,
                                     auth.username, auth.auth, auth.password, auth.key_file)
        self.port = forward.local_port
        self.connection = Tunnel(Profile(ignore_host_keys=ignore_host_keys))
        self.output = queue.Queue(maxsize=128)
        self.input = queue.Queue(maxsize=128)
        self.size = (columns, lines)
        self.status = "Connecting…"
        self.channel = None
        self.writer_error = None
        self.writer = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def send(self, data):
        if self.status != "Connected":
            return False
        try:
            self.input.put_nowait(data)
            return True
        except queue.Full:
            return False

    def resize(self, columns, lines):
        self.size = (columns, lines)

    def close(self):
        self.connection.disconnect()

    def write(self, channel, size):
        """Send keys independently of incoming data and renderer backpressure."""
        pending = b""
        try:
            while not self.connection.stop.is_set():
                if size != self.size:
                    size = self.size
                    channel.resize_pty(width=size[0], height=size[1])
                if not pending:
                    try:
                        pending = self.input.get(timeout=0.01)
                    except queue.Empty:
                        continue
                    # Coalesce fast typing without delaying the first keystroke.
                    while len(pending) < 16384:
                        try:
                            pending += self.input.get_nowait()
                        except queue.Empty:
                            break
                try:
                    count = channel.send(pending[:16384])
                except socket.timeout:
                    continue
                if not count:
                    break
                pending = pending[count:]
        except Exception as exc:
            if not self.connection.stop.is_set() and not channel.closed:
                self.writer_error = exc
                self.connection.disconnect()

    def run(self):
        try:
            transport = self.connection.connect_hop(self.endpoint, ("127.0.0.1", self.port))
            channel = self.channel = transport.open_session(timeout=10)
            size = self.size
            channel.get_pty(term="xterm-256color", width=size[0], height=size[1])
            channel.invoke_shell()
            channel.settimeout(0.1)
            self.status = "Connected"
            self.writer = threading.Thread(target=self.write, args=(channel, size), daemon=True)
            self.writer.start()
            while not self.connection.stop.is_set():
                try:
                    data = channel.recv(32768)
                except socket.timeout:
                    continue
                if not data:
                    break
                while not self.connection.stop.is_set():
                    try:
                        self.output.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        pass
            self.status = (f"Connection failed: {type(self.writer_error).__name__}: {self.writer_error}"
                           if self.writer_error else "Disconnected — reopen the terminal to reconnect")
        except Exception as exc:
            self.status = ("Disconnected" if self.connection.stop.is_set()
                           else f"Connection failed: {type(exc).__name__}: {exc}")
        finally:
            self.connection.stop.set()
            self.connection.cleanup()
            if self.writer:
                self.writer.join(timeout=1)


COLORS = dict(zip(
    ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
     "brightblack", "brightred", "brightgreen", "brightbrown", "brightblue",
     "brightmagenta", "brightcyan", "brightwhite"),
    ("#161b22", "#f47067", "#70d18c", "#eac55f", "#69a9ff", "#c895f5", "#61d6d6", "#d5dce5",
     "#77808d", "#ff938a", "#a0efb0", "#ffe589", "#a1c8ff", "#e1b9ff", "#9df0f0", "#ffffff")))


class TerminalWidget(QWidget):
    data = Signal(bytes)
    dimensions = Signal(int, int)
    cwd_received = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.font_base = QFont("Consolas", 11)
        self.font_base.setStyleHint(QFont.StyleHint.Monospace)
        self.fonts = {}
        for flags in range(16):
            font = QFont(self.font_base)
            font.setBold(bool(flags & 1))
            font.setItalic(bool(flags & 2))
            font.setUnderline(bool(flags & 4))
            font.setStrikeOut(bool(flags & 8))
            self.fonts[flags] = font
        metrics = QFontMetrics(self.font_base)
        self.cell_width = metrics.horizontalAdvance("M")
        self.cell_height = metrics.height()
        self.ascent = metrics.ascent()
        self.screen = TerminalScreen(reply=self.data.emit, cwd_received=self.cwd_received.emit)
        self.stream = TerminalStream(self.screen)
        self.offset = 0
        self.selection = None
        self.input_pending = False
        self.setMinimumSize(self.cell_width * 20, self.cell_height * 5)

    def feed(self, data):
        old_y = self.screen.cursor.y
        self.stream.feed(data)
        self.offset = min(self.offset, len(self.screen.history))
        if self.offset:
            self.update()
        else:
            for y in self.screen.dirty | {old_y, self.screen.cursor.y}:
                self.update(QRect(0, y * self.cell_height, self.width(), self.cell_height))
        self.screen.dirty.clear()

    def resizeEvent(self, event):
        columns = max(2, self.width() // self.cell_width)
        lines = max(2, self.height() // self.cell_height)
        self.screen.resize(lines=lines, columns=columns)
        self.selection = None
        self.dimensions.emit(columns, lines)
        super().resizeEvent(event)

    def rows(self):
        screen = self.screen
        if not self.offset or screen.primary is not None:
            return [screen.buffer[y] for y in range(screen.lines)]
        history = list(screen.history) if screen.primary is None else []
        offset = min(self.offset, len(history))
        rows = history + [screen.buffer[y] for y in range(screen.lines)]
        end = len(rows) - offset
        return rows[end - screen.lines:end]

    @staticmethod
    @lru_cache(maxsize=512)
    def color(name, default):
        return QColor(COLORS.get(name, "#" + name) if name != "default" else default)

    def selected(self, x, y):
        if self.selection is None:
            return False
        start, end = sorted(self.selection, key=lambda pos: (pos[1], pos[0]))
        return (start[1], start[0]) <= (y, x) <= (end[1], end[0])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.color("default", "#10151d"))
        for y, row in enumerate(self.rows()):
            if not event.region().intersects(QRect(0, y * self.cell_height, self.width(), self.cell_height)):
                continue
            # Paint backgrounds first: a wide glyph spans the following cell.
            for x in range(self.screen.columns):
                char = row.get(x, self.screen.default_char)
                fg, bg = self.color(char.fg, "#d5dce5"), self.color(char.bg, "#10151d")
                if char.reverse:
                    fg, bg = bg, fg
                if self.selected(x, y):
                    bg = QColor("#365b80")
                painter.fillRect(x * self.cell_width, y * self.cell_height,
                                 self.cell_width, self.cell_height, bg)
            for x in range(self.screen.columns):
                char = row.get(x, self.screen.default_char)
                fg = self.color(char.bg, "#10151d") if char.reverse else self.color(char.fg, "#d5dce5")
                if not char.data or (char.data == " " and not char.underscore and not char.strikethrough):
                    continue
                flags = int(char.bold) | int(char.italics) << 1 | int(char.underscore) << 2 | int(char.strikethrough) << 3
                painter.setFont(self.fonts[flags])
                painter.setPen(fg)
                painter.drawText(x * self.cell_width, y * self.cell_height + self.ascent, char.data)
        cursor = self.screen.cursor
        if not self.offset and not cursor.hidden:
            painter.setPen(QColor("#d5dce5"))
            painter.drawRect(min(cursor.x, self.screen.columns - 1) * self.cell_width,
                             cursor.y * self.cell_height, self.cell_width - 1, self.cell_height - 1)
        painter.end()

    def event(self, event):
        # QWidget normally consumes Tab for focus navigation.
        if event.type() == event.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if ctrl and shift and key == Qt.Key.Key_C:
            self.copy_selection()
            return
        if (ctrl and shift and key == Qt.Key.Key_V) or (shift and key == Qt.Key.Key_Insert):
            self.paste()
            return
        if shift and key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.scroll(self.screen.lines * (1 if key == Qt.Key.Key_PageUp else -1))
            return
        arrows = {Qt.Key.Key_Up: "A", Qt.Key.Key_Down: "B", Qt.Key.Key_Right: "C",
                  Qt.Key.Key_Left: "D", Qt.Key.Key_Home: "H", Qt.Key.Key_End: "F"}
        tilde = {Qt.Key.Key_Insert: 2, Qt.Key.Key_Delete: 3, Qt.Key.Key_PageUp: 5, Qt.Key.Key_PageDown: 6}
        function_codes = (11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24)
        modifier = 1 + int(shift) + 2 * int(alt) + 4 * int(ctrl)
        text = ""
        if key in arrows:
            text = (f"\x1b[1;{modifier}" if modifier > 1 else
                    ("\x1bO" if 1 << 5 in self.screen.mode else "\x1b[")) + arrows[key]
        elif key in tilde:
            text = f"\x1b[{tilde[key]}" + (f";{modifier}" if modifier > 1 else "") + "~"
        elif Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12:
            number = key - Qt.Key.Key_F1
            if number < 4:
                text = (f"\x1b[1;{modifier}" if modifier > 1 else "\x1bO") + "PQRS"[number]
            else:
                text = f"\x1b[{function_codes[number]}" + (f";{modifier}" if modifier > 1 else "") + "~"
        else:
            text = {Qt.Key.Key_Return: "\r", Qt.Key.Key_Enter: "\r", Qt.Key.Key_Backspace: "\x7f",
                    Qt.Key.Key_Tab: "\t", Qt.Key.Key_Backtab: "\x1b[Z", Qt.Key.Key_Escape: "\x1b"}.get(key, "")
            altgr = ctrl and alt and event.text() and event.text().isprintable()
            if altgr:
                text = event.text()
            elif ctrl and (Qt.Key.Key_A <= key <= Qt.Key.Key_Z or key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_Backslash, Qt.Key.Key_BracketRight)):
                text = chr(key & 31)
            elif ctrl and key in (Qt.Key.Key_Space, Qt.Key.Key_At):
                text = "\x00"
            elif ctrl and key in (Qt.Key.Key_AsciiCircum, Qt.Key.Key_6):
                text = "\x1e"
            elif ctrl and key in (Qt.Key.Key_Underscore, Qt.Key.Key_Minus):
                text = "\x1f"
            elif not text:
                text = event.text()
            if alt and text and not altgr:
                text = "\x1b" + text
        if text:
            self.input_pending = not (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                                      or (ctrl and key in (Qt.Key.Key_C, Qt.Key.Key_U)))
            needs_repaint = self.offset or self.selection is not None
            self.offset = 0
            self.selection = None
            self.data.emit(text.encode("utf-8"))
            if needs_repaint:
                self.update()
        event.accept()

    def inputMethodEvent(self, event):
        if event.commitString():
            self.input_pending = True
            self.data.emit(event.commitString().encode("utf-8"))
        event.accept()

    def paste(self):
        text = QApplication.clipboard().text().replace("\r\n", "\n").replace("\r", "\n")
        # Clipboard text must not terminate bracketed paste or inject escape sequences.
        text = "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))
        if not text:
            return
        if len(text.encode("utf-8")) > 1024 * 1024:
            QApplication.beep()
            return
        text = text.replace("\n", "\r")
        if 2004 << 5 in self.screen.mode:
            text = "\x1b[200~" + text + "\x1b[201~"
        self.offset = 0
        self.input_pending = True
        self.data.emit(text.encode("utf-8"))
        self.update()

    def cell_at(self, event):
        return (max(0, min(self.screen.columns - 1, int(event.position().x()) // self.cell_width)),
                max(0, min(self.screen.lines - 1, int(event.position().y()) // self.cell_height)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selection = (self.cell_at(event), self.cell_at(event))
            self.setFocus()
            self.update()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.selection:
            self.selection = (self.selection[0], self.cell_at(event))
            self.update()

    def copy_selection(self):
        if self.selection is None:
            return
        start, end = sorted(self.selection, key=lambda pos: (pos[1], pos[0]))
        lines = []
        for y, row in enumerate(self.rows()):
            if start[1] <= y <= end[1]:
                left = start[0] if y == start[1] else 0
                right = end[0] + 1 if y == end[1] else self.screen.columns
                lines.append("".join(row.get(x, self.screen.default_char).data for x in range(left, right)).rstrip())
        QApplication.clipboard().setText("\n".join(lines))

    def scroll(self, amount):
        if self.screen.primary is None:
            self.offset = max(0, min(len(self.screen.history), self.offset + amount))
            self.selection = None
            self.update()

    def wheelEvent(self, event):
        self.scroll(3 if event.angleDelta().y() > 0 else -3)


class TerminalWindow(QMainWindow):
    def __init__(self, forward, ignore_host_keys, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{forward.ssh.username}@{forward.target_host} · Native SSH")
        self.terminal = TerminalWidget()
        self.status = QLabel("Connecting…")
        help_text = QLabel("Ctrl+Shift+C copy selection · Ctrl+Shift+V paste · Shift+PgUp/PgDn or wheel: scrollback")
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.terminal, 1)
        layout.addWidget(self.status)
        layout.addWidget(help_text)
        self.setCentralWidget(container)
        self.resize(1000, 650)
        self.session = TerminalSession(copy.deepcopy(forward), ignore_host_keys)
        self.browser = None
        self.files_button = QPushButton("SFTP files…")
        self.files_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.files_button.setEnabled(False)
        self.files_button.clicked.connect(self.open_files)
        layout.addWidget(self.files_button)
        self.pending_output = b""
        self.terminal.data.connect(self.send)
        self.terminal.dimensions.connect(self.session.resize)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(16)
        self.session.start()
        self.terminal.setFocus()

    def send(self, data):
        if not self.session.send(data):
            QApplication.beep()

    def open_files(self):
        if self.session.status != "Connected":
            return
        if self.browser is None:
            from .sftp_browser import SftpBrowser
            self.browser = SftpBrowser(self)
            self.browser.destroyed.connect(lambda: setattr(self, "browser", None))
        self.browser.showNormal()
        self.browser.raise_()
        self.browser.activateWindow()

    def poll(self):
        self.files_button.setEnabled(self.session.status == "Connected")
        # Budget CPU time as well as bytes; large bursts must not starve input.
        deadline = time.perf_counter() + 0.008
        while time.perf_counter() < deadline:
            try:
                if not self.pending_output:
                    self.pending_output = self.session.output.get_nowait()
                chunk, self.pending_output = self.pending_output[:4096], self.pending_output[4096:]
                self.terminal.feed(chunk)
            except queue.Empty:
                break
            except (ValueError, TypeError, AssertionError) as exc:
                self.timer.stop()
                self.session.close()
                self.status.setText(f"Unsupported terminal sequence: {type(exc).__name__}. Reopen the terminal to reconnect.")
                return
        if self.status.text() != self.session.status:
            self.status.setText(self.session.status)
        if not self.session.thread.is_alive() and self.session.output.empty() and not self.pending_output:
            self.timer.stop()

    def closeEvent(self, event):
        if self.browser is not None and not self.browser.close():
            event.ignore()
            return
        self.timer.stop()
        self.session.close()
        super().closeEvent(event)
