import os
import json
import copy
import subprocess
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSpinBox, QSplitter, QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget,
    QStyle)
from .models import Forward, Hop, HostEndpoint, Profile, SSHAuth
from .storage import Store
from .tunnel import Tunnel
from .integrations import (IntegrationSettings, SettingsStore,
                           detect_settings, validate_settings)

AUTH_METHODS = ["All local agents", "OpenSSH agent", "Pageant", "Password", "Private key file"]


def spin(value, minimum=1, maximum=65535):
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    return widget


def button(text, callback):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    return widget


def icon(color="#56d6b2"):
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#162333"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 15, 15)
    painter.setPen(QColor(color))
    font = painter.font()
    font.setPixelSize(32)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "⇢")
    painter.end()
    return QIcon(pixmap)


class HopDialog(QDialog):
    def __init__(self, parent, hop=None, allow_alternatives=True):
        super().__init__(parent)
        self.allow_alternatives = allow_alternatives
        self.setWindowTitle("SSH hop" if allow_alternatives else "Alternative SSH host")
        self.setMinimumWidth(460)
        hop = hop or (Hop() if allow_alternatives else HostEndpoint())
        self.alternative_hosts = copy.deepcopy(hop.alternatives) if allow_alternatives else []
        form = QFormLayout(self)
        self.host = QLineEdit(hop.host)
        self.host.setPlaceholderText("bastion.example.com")
        self.port = spin(hop.port)
        self.user = QLineEdit(hop.username)
        self.auth = QComboBox()
        self.auth.addItems([m for m in AUTH_METHODS if os.name == "nt" or m != "Pageant"])
        self.auth.setCurrentText(hop.auth)
        self.secret = QLineEdit(hop.password)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.key = QLineEdit(hop.key_file)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        key_row.addWidget(button("Browse…", self.browse))
        form.addRow("Host", self.host)
        form.addRow("SSH port", self.port)
        self.multiple_hosts = QCheckBox("Multiple hosts · try alternatives if this route fails")
        self.multiple_hosts.setChecked(hop.multiple_hosts if allow_alternatives else False)
        form.addRow(self.multiple_hosts)
        self.multiple_hosts.setVisible(allow_alternatives)
        self.alternatives_panel = QWidget()
        alternative_layout = QVBoxLayout(self.alternatives_panel)
        alternative_layout.setContentsMargins(0, 0, 0, 0)
        self.alternatives = QListWidget()
        self.alternatives.itemDoubleClicked.connect(lambda _: self.edit_alternative())
        self.alternatives.setMinimumHeight(120)
        self.alternatives.setMaximumHeight(160)
        alternative_layout.addWidget(self.alternatives)
        alternative_buttons = QHBoxLayout()
        alternative_buttons.addWidget(button("+ Add host", lambda: self.add_alternative()))
        alternative_buttons.addWidget(button("Edit host", self.edit_alternative))
        alternative_buttons.addWidget(button("Remove host", self.remove_alternative))
        alternative_layout.addLayout(alternative_buttons)
        hint = QLabel("Alternatives have their own username and authentication settings. They are tried in list order; one host must provide a working route.")
        hint.setWordWrap(True)
        alternative_layout.addWidget(hint)
        form.addRow(self.alternatives_panel)
        self.refresh_alternatives()
        self.multiple_hosts.toggled.connect(self.alternatives_panel.setVisible)
        self.alternatives_panel.setVisible(allow_alternatives and hop.multiple_hosts)
        form.addRow("Username", self.user)
        form.addRow("Authentication", self.auth)
        form.addRow("Password / key passphrase", self.secret)
        form.addRow("Private key (OpenSSH / PEM)", key_row)
        agent_hint = ("Load PuTTY .ppk keys into Pageant." if os.name == "nt"
                      else "Load SSH keys into your desktop SSH agent.")
        hint = QLabel("Agent keys stay on this PC. " + agent_hint +
                      "\nSaved passwords and passphrases use your account’s secure credential storage.")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.auth.currentTextChanged.connect(self.update_fields)
        self.update_fields()

    def update_fields(self):
        mode = self.auth.currentText()
        self.secret.setEnabled(mode in ("Password", "Private key file"))
        self.key.setEnabled(mode == "Private key file")

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select private key", str(Path.home() / ".ssh"))
        if path:
            self.key.setText(path)

    def value(self):
        values = (self.host.text().strip(), self.port.value(), self.user.text().strip(),
                  self.auth.currentText(), self.secret.text() if self.secret.isEnabled() else "",
                  self.key.text().strip())
        if not self.allow_alternatives:
            return HostEndpoint(*values)
        return Hop(*values, self.multiple_hosts.isChecked(), copy.deepcopy(self.alternative_hosts))

    def refresh_alternatives(self, selected=0):
        self.alternatives.clear()
        for host in self.alternative_hosts:
            self.alternatives.addItem(f"{host.username}@{host.host}:{host.port}   ·   {host.auth}")
        self.alternatives.setCurrentRow(selected)

    def add_alternative(self):
        dialog = HopDialog(self, allow_alternatives=False)
        if dialog.exec():
            self.alternative_hosts.append(dialog.value())
            self.refresh_alternatives(len(self.alternative_hosts) - 1)

    def edit_alternative(self):
        index = self.alternatives.currentRow()
        if index < 0:
            return
        dialog = HopDialog(self, self.alternative_hosts[index], allow_alternatives=False)
        if dialog.exec():
            self.alternative_hosts[index] = dialog.value()
            self.refresh_alternatives(index)

    def remove_alternative(self):
        index = self.alternatives.currentRow()
        if index >= 0:
            self.alternative_hosts.pop(index)
            self.refresh_alternatives(max(0, index - 1))

    def accept(self):
        try:
            if self.allow_alternatives:
                Profile(hops=[self.value()]).validate()
            else:
                self.value().validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Check this hop", str(exc))
            return
        super().accept()


class ForwardDialog(QDialog):
    def __init__(self, parent, forward=None, used_ports=()):
        super().__init__(parent)
        self.setWindowTitle("TCP forward from last hop")
        self.setMinimumWidth(490)
        self.used_ports = set(used_ports)
        forward = forward or Forward()
        form = QFormLayout(self)
        self.description = QLineEdit(forward.description)
        self.description.setPlaceholderText("Optional, e.g. Office desktop or Router admin")
        self.local_port = spin(forward.local_port)
        self.target_host = QLineEdit(forward.target_host)
        self.target_port = spin(forward.target_port)
        self.health_port = spin(forward.health_port, 0)
        self.health_port.setSpecialValueText("Same as destination")
        form.addRow("Description", self.description)
        form.addRow("Local port · 127.0.0.1", self.local_port)
        form.addRow("Destination host · from last hop", self.target_host)
        form.addRow("Destination TCP port", self.target_port)
        form.addRow("TCP probe port · on this destination", self.health_port)
        self.ssh_enabled = QCheckBox("SSH endpoint - enable terminal and SFTP")
        self.ssh_enabled.setChecked(forward.ssh is not None)
        form.addRow(self.ssh_enabled)
        self.ssh_panel = QWidget()
        auth_form = QFormLayout(self.ssh_panel)
        auth_form.setContentsMargins(0, 0, 0, 0)
        ssh = forward.ssh or SSHAuth()
        self.user = QLineEdit(ssh.username)
        self.auth = QComboBox()
        self.auth.addItems([m for m in AUTH_METHODS if os.name == "nt" or m != "Pageant"])
        self.auth.setCurrentText(ssh.auth)
        self.secret = QLineEdit(ssh.password)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.key = QLineEdit(ssh.key_file)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        key_row.addWidget(button("Browse…", self.browse_key))
        auth_form.addRow("SSH username", self.user)
        auth_form.addRow("Authentication", self.auth)
        auth_form.addRow("Password / key passphrase", self.secret)
        auth_form.addRow("Private key (OpenSSH / PEM)", key_row)
        auth_hint = QLabel("Credentials are saved with account-bound encryption. The native terminal and SFTP browser use these endpoint credentials.")
        auth_hint.setWordWrap(True)
        auth_form.addRow(auth_hint)
        form.addRow(self.ssh_panel)
        self.ssh_enabled.toggled.connect(self.ssh_panel.setVisible)
        self.ssh_panel.setVisible(self.ssh_enabled.isChecked())
        self.auth.currentTextChanged.connect(self.update_auth_fields)
        self.update_auth_fields()
        hint = QLabel("This forward shares the profile's SSH route. Use a different local port for each destination.")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self):
        ssh = SSHAuth(self.user.text().strip(), self.auth.currentText(),
                      self.secret.text() if self.secret.isEnabled() else "", self.key.text().strip()) if self.ssh_enabled.isChecked() else None
        return Forward(self.local_port.value(), self.target_host.text().strip(),
                       self.target_port.value(), self.health_port.value(), self.description.text().strip(), ssh)

    def update_auth_fields(self):
        self.secret.setEnabled(self.auth.currentText() in ("Password", "Private key file"))
        self.key.setEnabled(self.auth.currentText() == "Private key file")

    def browse_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select private key", str(Path.home() / ".ssh"))
        if path:
            self.key.setText(path)

    def accept(self):
        try:
            self.value().validate()
            if self.local_port.value() in self.used_ports:
                raise ValueError("This local port is already used by another forward.")
        except ValueError as exc:
            QMessageBox.warning(self, "Check this forward", str(exc))
            return
        super().accept()


class SettingsDialog(QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle("App settings - External editor")
        self.setMinimumWidth(650)
        form = QFormLayout(self)
        self.editor_path = QLineEdit(settings.editor_path)
        self.editor_path.setPlaceholderText("Select Notepad++, Notepad, or another text editor...")
        row = QHBoxLayout()
        row.addWidget(self.editor_path)
        row.addWidget(button("Browse...", self.browse))
        form.addRow("Editor executable", row)
        self.editor_arguments = QLineEdit(json.dumps(settings.editor_arguments))
        form.addRow("Arguments (JSON list)", self.editor_arguments)
        hint = QLabel('F4 opens files in this editor. Optional arguments are a JSON list, e.g. ["-multiInst"] for Notepad++. The filename is appended automatically. Save remote edits in your editor, then click Upload changes in SSH Jumper. Settings apply to all profiles.')
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select editor", self.editor_path.text(), "Windows executables (*.exe)" if os.name == "nt" else "All files (*)")
        if path:
            self.editor_path.setText(path)

    def value(self):
        return IntegrationSettings(self.editor_path.text().strip(), json.loads(self.editor_arguments.text()))

    def accept(self):
        try:
            validate_settings(self.value())
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Check editor settings", str(exc))
            return
        super().accept()


class Events(QObject):
    log = Signal(str)
    status = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, store=None):
        super().__init__()
        self.store = store or Store()
        self.settings_store = SettingsStore(self.store.path.with_name("settings.json"))
        settings_error = None
        try:
            self.settings = self.settings_store.load()
        except Exception as exc:
            self.settings = detect_settings()
            settings_error = str(exc)
        self.connection_status = "Disconnected"
        self.edit_controls = []
        self.tunnel = None
        self.current = -1
        self.hops = []
        self.forwards = []
        self.quitting = False
        self.terminals = set()
        self.events = Events()
        self.events.log.connect(self.log)
        self.events.status.connect(self.set_status)
        self.setWindowTitle("SSH Jumper")
        self.setWindowIcon(icon())
        self.resize(1050, 800)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("SSH Jumper")
        title.setObjectName("title")
        heading = QHBoxLayout()
        heading.addWidget(title, 1)
        self.settings_button = button("App settings", self.open_settings)
        self.settings_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        heading.addWidget(self.settings_button)
        layout.addLayout(heading)
        layout.addWidget(QLabel("Multiple destinations. One shared SSH route."))
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.addWidget(QLabel("SAVED TUNNELS"))
        self.profiles_list = QListWidget()
        side.addWidget(self.profiles_list)
        self.profile_buttons = QWidget()
        actions = QHBoxLayout(self.profile_buttons)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(button("+ New", self.new_profile))
        actions.addWidget(button("Copy", self.copy_profile))
        actions.addWidget(button("Delete", self.delete_profile))
        side.addWidget(self.profile_buttons)
        splitter.addWidget(sidebar)
        self.editor = QWidget()
        edit = QVBoxLayout(self.editor)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Tunnel name")
        self.name.textEdited.connect(self.update_profile_name)
        self.name.returnPressed.connect(self.save_profile)
        edit.addWidget(self.name)
        edit.addWidget(QLabel("01   SSH ROUTE"))
        self.hop_list = QListWidget()
        self.hop_list.setMinimumHeight(130)
        self.hop_list.itemDoubleClicked.connect(lambda _: self.edit_hop())
        edit.addWidget(self.hop_list, 1)
        row = QHBoxLayout()
        for label, callback in [("+ Add hop", self.add_hop), ("Edit", self.edit_hop),
                                ("Remove", self.remove_hop), ("↑", lambda: self.move_hop(-1)),
                                ("↓", lambda: self.move_hop(1))]:
            control = button(label, callback)
            row.addWidget(control)
            self.edit_controls.append(control)
        edit.addLayout(row)
        edit.addWidget(QLabel("02   TCP FORWARDS · FROM LAST HOP"))
        self.forward_list = QListWidget()
        self.forward_list.setMinimumHeight(110)
        self.forward_list.itemDoubleClicked.connect(lambda _: self.edit_forward())
        edit.addWidget(self.forward_list, 1)
        row = QHBoxLayout()
        for label, callback in [("+ Add forward", self.add_forward), ("Edit", self.edit_forward),
                                ("Remove", self.remove_forward), ("Copy", self.copy_forward)]:
            control = button(label, callback)
            row.addWidget(control)
            self.edit_controls.append(control)
        edit.addLayout(row)
        self.client_buttons = QWidget()
        client_row = QHBoxLayout(self.client_buttons)
        client_row.setContentsMargins(0, 0, 0, 0)
        self.terminal_button = button("Open terminal", self.open_terminal)
        self.terminal_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        client_row.addWidget(self.terminal_button)
        self.web_button = button("Open in web", self.open_web)
        self.web_button.setToolTip("Open the selected forward's local HTTP address in your default browser")
        client_row.addWidget(self.web_button)
        self.mstsc_button = button("Open in MSTSC", self.open_mstsc)
        self.mstsc_button.setToolTip("Open the selected RDP forward in Windows Remote Desktop")
        client_row.addWidget(self.mstsc_button)
        edit.addWidget(self.client_buttons)
        self.forward_list.currentRowChanged.connect(self.update_client_buttons)
        self.update_client_buttons()
        edit.addWidget(QLabel("03   CONNECTION HEALTH"))
        form = QFormLayout()
        self.interval = spin(15, 3, 3600)
        self.interval.setSuffix(" seconds")
        form.addRow("Check all destinations every", self.interval)
        edit.addLayout(form)
        self.reconnect = QCheckBox("Automatically reconnect after connection failure")
        self.reconnect.setChecked(True)
        self.ignore_keys = QCheckBox("Ignore SSH host keys (server identity is not verified)")
        self.ignore_keys.setChecked(True)
        edit.addWidget(self.reconnect)
        edit.addWidget(self.ignore_keys)
        self.save_credentials = QCheckBox("Save passwords and key passphrases")
        self.save_credentials.setChecked(True)
        self.save_credentials.setToolTip(
            "Uncheck to keep credentials only for this session. Saving then removes saved credentials from all profiles.")
        edit.addWidget(self.save_credentials)
        save = button("Save profile", self.save_profile)
        edit.addWidget(save)
        self.edit_controls.extend([self.name, self.hop_list, self.interval, self.reconnect, self.ignore_keys, self.save_credentials, save])
        splitter.addWidget(self.editor)
        splitter.setSizes([250, 700])
        bottom = QHBoxLayout()
        self.status_label = QLabel("●  Disconnected")
        bottom.addWidget(self.status_label, 1)
        self.connect_button = button("Connect tunnel", self.connect_tunnel)
        self.connect_button.setObjectName("primary")
        self.disconnect_button = button("Disconnect", self.disconnect_tunnel)
        self.disconnect_button.setEnabled(False)
        bottom.addWidget(self.connect_button)
        bottom.addWidget(self.disconnect_button)
        layout.addLayout(bottom)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumHeight(145)
        self.logs.document().setMaximumBlockCount(600)
        layout.addWidget(self.logs)
        if settings_error:
            self.log(f"Could not load app settings: {settings_error}. Open App settings to correct them.")
        self.tray = QSystemTrayIcon(icon(), self)
        menu = QMenu(self)
        for text, callback in [("Open SSH Jumper", self.reveal), ("Connect", self.connect_tunnel),
                                ("Disconnect", self.disconnect_tunnel), ("Quit", self.quit_app)]:
            action = QAction(text, self)
            action.triggered.connect(callback)
            menu.addAction(action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.reveal() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.setToolTip("SSH Jumper · Disconnected")
        self.tray.show()
        try:
            self.profiles = self.store.load()
        except Exception as exc:
            self.profiles = []
            QMessageBox.critical(self, "Could not load saved profiles", f"{exc}\nThe original file has been preserved. Restart after fixing it.")
            self.editor.setEnabled(False)
            self.profile_buttons.setEnabled(False)
            self.connect_button.setEnabled(False)
            self.store = None
        self.profiles_list.currentRowChanged.connect(self.select_profile)
        if self.profiles:
            self.refresh_profiles(0)
        elif self.store:
            self.new_profile()

    def open_settings(self):
        dialog = SettingsDialog(self, self.settings)
        if dialog.exec():
            try:
                self.settings_store.save(dialog.value())
                self.settings = dialog.value()
                self.log("External editor settings saved")
            except Exception as exc:
                QMessageBox.warning(self, "Settings could not be saved", str(exc))

    def update_client_buttons(self, *_):
        index = self.forward_list.currentRow()
        selected = 0 <= index < len(self.forwards)
        has_ssh = selected and self.forwards[index].ssh is not None
        self.client_buttons.setVisible(selected)
        self.terminal_button.setVisible(has_ssh)
        self.mstsc_button.setVisible(os.name == "nt" and selected and self.forwards[index].target_port == 3389)
        ready = selected and self.connection_status == "Connected"
        self.client_buttons.setEnabled(ready)
        self.client_buttons.setToolTip("Open the selected endpoint through this tunnel" if ready else "Connect the tunnel first")

    def open_web(self):
        index = self.forward_list.currentRow()
        if self.connection_status != "Connected" or not self.tunnel or not self.tunnel.thread.is_alive():
            QMessageBox.information(self, "Connect first", "Connect this tunnel before opening an endpoint.")
            return
        if not 0 <= index < len(self.tunnel.profile.forwards):
            return
        forward = self.tunnel.profile.forwards[index]
        url = QUrl(f"http://127.0.0.1:{forward.local_port}/")
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "Could not open browser", "Could not open " + url.toString())

    def open_mstsc(self):
        index = self.forward_list.currentRow()
        if self.connection_status != "Connected" or not self.tunnel or not self.tunnel.thread.is_alive():
            QMessageBox.information(self, "Connect first", "Connect this tunnel before opening an endpoint.")
            return
        if not 0 <= index < len(self.tunnel.profile.forwards):
            return
        forward = self.tunnel.profile.forwards[index]
        if forward.target_port != 3389:
            return
        try:
            subprocess.Popen(["mstsc.exe", f"/v:127.0.0.1:{forward.local_port}"])
        except OSError as exc:
            QMessageBox.warning(self, "Could not open Remote Desktop", str(exc))

    def open_terminal(self):
        index = self.forward_list.currentRow()
        if self.connection_status != "Connected" or not self.tunnel or not self.tunnel.thread.is_alive():
            QMessageBox.information(self, "Connect first", "Connect this tunnel before opening an endpoint.")
            return
        if not 0 <= index < len(self.tunnel.profile.forwards):
            return
        # Use the immutable active profile, never an unsaved destination edit.
        forward = self.tunnel.profile.forwards[index]
        try:
            from .terminal import TerminalWindow
            if forward.ssh is None:
                raise ValueError("Enable SSH authentication for this forward first.")
            window = TerminalWindow(forward, self.tunnel.profile.ignore_host_keys, self)
            self.terminals.add(window)
            window.destroyed.connect(lambda _=None, w=window: self.terminals.discard(w))
            window.show()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Could not open client", str(exc))

    def refresh_profiles(self, selected):
        self.profiles_list.blockSignals(True)
        self.profiles_list.clear()
        self.profiles_list.addItems([p.name for p in self.profiles])
        self.profiles_list.setCurrentRow(selected)
        self.profiles_list.blockSignals(False)
        self.current = selected
        self.load_editor()

    def capture(self):
        return Profile(name=self.name.text().strip(), hops=copy.deepcopy(self.hops),
            forwards=copy.deepcopy(self.forwards),
            interval=self.interval.value(), reconnect=self.reconnect.isChecked(),
            ignore_host_keys=self.ignore_keys.isChecked())

    def remember(self):
        if 0 <= self.current < len(self.profiles):
            self.profiles[self.current] = self.capture()
            self.update_profile_name()

    def update_profile_name(self, *_):
        item = self.profiles_list.item(self.current)
        if item is not None:
            item.setText(self.name.text().strip())

    def select_profile(self, row):
        self.remember()
        self.current = row
        self.load_editor()

    def load_editor(self):
        if not 0 <= self.current < len(self.profiles):
            return
        profile = self.profiles[self.current]
        self.name.setText(profile.name)
        self.hops = copy.deepcopy(profile.hops)
        self.forwards = copy.deepcopy(profile.forwards)
        self.interval.setValue(profile.interval)
        self.reconnect.setChecked(profile.reconnect)
        self.ignore_keys.setChecked(profile.ignore_host_keys)
        self.refresh_hops()
        self.refresh_forwards()

    def new_profile(self):
        self.remember()
        self.profiles.append(Profile(name=f"Tunnel {len(self.profiles) + 1}"))
        self.refresh_profiles(len(self.profiles) - 1)

    def copy_profile(self):
        if not self.store or self.connection_status != "Disconnected" or not 0 <= self.current < len(self.profiles):
            return
        self.remember()
        duplicate = copy.deepcopy(self.profiles[self.current])
        base = duplicate.name or "Tunnel"
        names = {profile.name.casefold() for profile in self.profiles}
        duplicate.name = f"{base} (copy)"
        number = 2
        while duplicate.name.casefold() in names:
            duplicate.name = f"{base} (copy {number})"
            number += 1
        self.profiles.append(duplicate)
        self.refresh_profiles(len(self.profiles) - 1)
        self.name.setFocus()
        self.name.selectAll()
        self.log("Profile copied with all hops, forwards, and credentials. Click Save profile to keep it.")

    def delete_profile(self):
        if self.current < 0:
            return
        if QMessageBox.question(self, "Delete profile", "Delete this saved tunnel profile?") != QMessageBox.StandardButton.Yes:
            return
        remaining = [p for i, p in enumerate(self.profiles) if i != self.current]
        try:
            self.store.save(remaining, save_credentials=self.save_credentials.isChecked())
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.profiles = remaining or [Profile()]
        self.refresh_profiles(0)

    def save_profile(self):
        try:
            self.capture().validate()
            self.remember()
            self.store.save(self.profiles, save_credentials=self.save_credentials.isChecked())
            self.profiles_list.item(self.current).setText(self.profiles[self.current].name)
            self.log("Profiles saved; credentials protected with account-bound encryption" if self.save_credentials.isChecked()
                     else "Profiles saved without credentials; passwords remain in memory for this session")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Could not save profile", str(exc))
            return False

    def refresh_hops(self, selected=0):
        self.hop_list.clear()
        for index, hop in enumerate(self.hops):
            alternatives = f"   ·   {len(hop.endpoints())} hosts" if hop.multiple_hosts else ""
            self.hop_list.addItem(f"{index + 1:02}   {hop.username}@{hop.host}:{hop.port}   ·   {hop.auth}{alternatives}")
            self.hop_list.item(index).setToolTip(" → or → ".join(f"{endpoint.host}:{endpoint.port}" for endpoint in hop.endpoints()))
        self.hop_list.setCurrentRow(selected)

    def add_hop(self):
        dialog = HopDialog(self)
        if dialog.exec():
            self.hops.append(dialog.value())
            self.refresh_hops(len(self.hops) - 1)

    def edit_hop(self):
        index = self.hop_list.currentRow()
        if index < 0:
            return
        dialog = HopDialog(self, self.hops[index])
        if dialog.exec():
            self.hops[index] = dialog.value()
            self.refresh_hops(index)

    def remove_hop(self):
        index = self.hop_list.currentRow()
        if index >= 0:
            self.hops.pop(index)
            self.refresh_hops(max(0, index - 1))

    def move_hop(self, delta):
        index = self.hop_list.currentRow()
        target = index + delta
        if 0 <= index < len(self.hops) and 0 <= target < len(self.hops):
            self.hops[index], self.hops[target] = self.hops[target], self.hops[index]
            self.refresh_hops(target)

    def refresh_forwards(self, selected=0):
        self.forward_list.clear()
        for forward in self.forwards:
            description = f"{forward.description}  ·  " if forward.description else ""
            self.forward_list.addItem(
                f"{description}127.0.0.1:{forward.local_port}  →  {forward.target_host}:{forward.target_port}"
                f"   ·   probe {forward.health_port or forward.target_port}")
            self.forward_list.item(self.forward_list.count() - 1).setToolTip(
                self.forward_list.item(self.forward_list.count() - 1).text())
        self.forward_list.setCurrentRow(selected)

    def add_forward(self):
        if self.connection_status != "Disconnected":
            return
        used = {forward.local_port for forward in self.forwards}
        port = next((port for port in range(8080, 65536) if port not in used), None)
        if port is None:
            port = next((port for port in range(1, 8080) if port not in used), None)
        if port is None:
            QMessageBox.warning(self, "No local ports", "All local ports are already assigned in this profile.")
            return
        dialog = ForwardDialog(self, Forward(local_port=port), used)
        if dialog.exec():
            self.forwards.append(dialog.value())
            self.refresh_forwards(len(self.forwards) - 1)

    def copy_forward(self):
        if self.connection_status != "Disconnected":
            return
        index = self.forward_list.currentRow()
        if index < 0:
            return
        used = {forward.local_port for forward in self.forwards}
        dialog = ForwardDialog(self, copy.deepcopy(self.forwards[index]), used)
        if dialog.exec():
            self.forwards.append(dialog.value())
            self.refresh_forwards(len(self.forwards) - 1)

    def edit_forward(self):
        if self.connection_status != "Disconnected":
            return
        index = self.forward_list.currentRow()
        if index < 0:
            return
        used = {forward.local_port for i, forward in enumerate(self.forwards) if i != index}
        dialog = ForwardDialog(self, self.forwards[index], used)
        if dialog.exec():
            self.forwards[index] = dialog.value()
            self.refresh_forwards(index)

    def remove_forward(self):
        if self.connection_status != "Disconnected":
            return
        index = self.forward_list.currentRow()
        if index >= 0:
            self.forwards.pop(index)
            self.refresh_forwards(max(0, index - 1))

    def connect_tunnel(self):
        if not self.store or (self.tunnel and self.tunnel.thread.is_alive()):
            return
        if not self.save_profile():
            return
        self.tunnel = Tunnel(self.capture(), self.events.log.emit, self.events.status.emit)
        self.set_status("Connecting")
        self.tunnel.start()

    def disconnect_tunnel(self):
        if self.tunnel:
            self.set_status("Disconnecting")
            self.disconnect_button.setEnabled(False)
            self.tunnel.disconnect()

    def set_status(self, status):
        self.connection_status = status
        self.status_label.setText(f"●  {status}")
        self.tray.setToolTip(f"SSH Jumper · {status}")
        self.tray.setIcon(icon("#56d6b2" if status == "Connected" else "#f3b568"))
        active = status != "Disconnected"
        for control in self.edit_controls:
            control.setEnabled(not active and self.store is not None)
        self.profiles_list.setEnabled(not active)
        self.profile_buttons.setEnabled(not active and self.store is not None)
        self.connect_button.setEnabled(not active and self.store is not None)
        self.disconnect_button.setEnabled(active)
        self.update_client_buttons()
        if self.quitting and not active:
            QApplication.instance().quit()

    def log(self, text):
        self.logs.append(f"{datetime.now():%H:%M:%S}  {text}".replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
        else:
            event.ignore()
            self.quit_app()

    def quit_app(self):
        for window in list(self.terminals):
            if not window.close():
                return
        self.quitting = True
        if self.tunnel and self.tunnel.thread.is_alive():
            self.disconnect_tunnel()
        else:
            QApplication.instance().quit()


STYLE = """
QWidget { background: #101925; color: #dee8f3; font-family: 'Segoe UI'; font-size: 13px; }
QLabel#title { font-size: 29px; font-weight: 700; color: #ffffff; }
QLabel { background: transparent; padding: 3px 0; }
QLineEdit, QSpinBox, QComboBox, QListWidget, QTextEdit {
    background: #182535; border: 1px solid #304155; border-radius: 6px; padding: 7px;
    selection-background-color: #265c60;
}
QListWidget::item { padding: 8px 4px; }
QListWidget::item:selected { background: #265c60; border-radius: 4px; }
QPushButton { background: #233449; border: 1px solid #3b5067; border-radius: 6px; padding: 8px 13px; }
QPushButton:hover { background: #30465e; }
QPushButton#primary { background: #56d6b2; color: #10241f; font-weight: 700; }
QWidget:disabled { color: #718093; }
QCheckBox { padding: 3px; }
QMenu { border: 1px solid #3b5067; padding: 5px; }
QMenu::item { padding: 8px 24px; }
QMenu::item:selected { background: #265c60; }
"""
