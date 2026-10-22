import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from ssh_jumper.models import Forward, Hop, HostEndpoint
from ssh_jumper.storage import Store
from ssh_jumper.ui import ForwardDialog, HopDialog, MainWindow


def test_copy_whole_profile_is_independent_and_persists(tmp_path):
    import copy
    from ssh_jumper.models import Profile, SSHAuth
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path / "profiles.json")
    original = Profile(name="Office", hops=[Hop("primary", username="alice", auth="Password", password="primary-secret",
        multiple_hosts=True, alternatives=[HostEndpoint("backup", 2222, "bob", "Password", "backup-secret")])],
        forwards=[Forward(2222, "device", 22, 23, "Device SSH", SSHAuth("carol", "Password", "endpoint-secret"))],
        interval=30, reconnect=False, ignore_host_keys=False)
    store.save([original])
    window = MainWindow(store)
    try:
        window.forwards[0].description = "Unsaved description"
        expected = window.capture()
        window.copy_profile()
        assert window.current == 1 and window.name.text() == "Office (copy)"
        duplicate = window.capture()
        duplicate.name = expected.name
        assert duplicate == expected
        window.hops[0].alternatives[0].password = "changed-backup"
        window.forwards[0].ssh.username = "changed-user"
        assert window.save_profile()
        saved = store.load()
        assert saved[0] == expected
        assert saved[1].hops[0].alternatives[0].password == "changed-backup"
        assert saved[1].forwards[0].ssh.username == "changed-user"
        assert "endpoint-secret" not in store.path.read_text()
        window.profiles_list.setCurrentRow(0)
        window.copy_profile()
        assert window.name.text() == "Office (copy 2)"
        before = copy.deepcopy(window.profiles)
        window.set_status("Connected")
        window.copy_profile()
        assert window.profiles == before
    finally:
        window.tray.hide()
        window.deleteLater()
        app.processEvents()


def test_profile_rename_switch_save_and_restart(tmp_path):
    from ssh_jumper.models import Profile
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path / "profiles.json")
    store.save([Profile(name=name, hops=[Hop("jump", username="alice")]) for name in ("First", "Second")])
    window = MainWindow(store)
    reopened = None
    try:
        window.name.setText("Renamed first")
        window.profiles_list.setCurrentRow(1)
        assert window.profiles_list.item(0).text() == "Renamed first"
        assert window.name.text() == "Second"
        assert window.save_profile()
        reopened = MainWindow(store)
        assert reopened.profiles_list.item(0).text() == "Renamed first"
        assert reopened.name.text() == "Renamed first"
        from PySide6.QtTest import QTest
        reopened.name.selectAll()
        QTest.keyClicks(reopened.name, "Final name")
        assert reopened.profiles_list.item(0).text() == "Final name"
        reopened.name.returnPressed.emit()
        assert store.load()[0].name == "Final name"
        assert reopened.profiles_list.item(0).text() == "Final name"
    finally:
        for item in (window, reopened):
            if item:
                item.tray.hide()
                item.deleteLater()
        app.processEvents()


def test_endpoint_auth_edit_copy_and_launch_controls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ssh_jumper.models import SSHAuth
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Store(tmp_path / "profiles.json"))
    original = Forward(2222, "device", 22, ssh=SSHAuth("alice", "Password", "endpoint-secret"))
    try:
        window.forwards = [original, Forward(8080)]
        window.refresh_forwards()
        assert not window.client_buttons.isHidden()
        assert not window.client_buttons.isEnabled()
        assert not window.terminal_button.icon().isNull()
        def copy_dialog(dialog):
            assert dialog.ssh_enabled.isChecked()
            assert dialog.value().ssh == original.ssh
            dialog.local_port.setValue(2223)
            dialog.user.setText("bob")
            dialog.accept()
            return dialog.result()
        monkeypatch.setattr(ForwardDialog, "exec", copy_dialog)
        window.copy_forward()
        assert window.forwards[2].ssh.username == "bob"
        assert original.ssh.username == "alice"
        active = window.capture()
        window.tunnel = SimpleNamespace(profile=active, thread=SimpleNamespace(is_alive=lambda: True))
        calls = []
        from PySide6.QtWidgets import QMainWindow
        class FakeTerminal(QMainWindow):
            def __init__(self, forward, ignore_host_keys, parent):
                super().__init__(parent)
                calls.append(forward)
        monkeypatch.setattr('ssh_jumper.terminal.TerminalWindow', FakeTerminal)
        window.set_status("Connected")
        assert window.forward_list.isEnabled() and window.client_buttons.isEnabled()
        assert all(not widget.isEnabled() for widget in window.edit_controls)
        window.open_terminal()
        assert calls == [active.forwards[2]]
        window.edit_forward()  # Double-click must not edit a live route.
        assert len(window.forwards) == 3
        window.forward_list.setCurrentRow(1)
        assert not window.client_buttons.isHidden()
        assert window.terminal_button.isHidden()
        assert window.web_button.isEnabled()
        urls = []
        monkeypatch.setattr('ssh_jumper.ui.QDesktopServices.openUrl', lambda url: urls.append(url.toString()) or True)
        window.forwards[1].local_port = 9999  # Launch the active route, not unsaved edits.
        window.web_button.click()
        assert urls == ['http://127.0.0.1:8080/']
        window.forward_list.setCurrentRow(0)
        window.set_status("Reconnecting")
        assert not window.client_buttons.isEnabled()
        window.set_status("Disconnected")
        assert all(widget.isEnabled() for widget in window.edit_controls)
    finally:
        window.tray.hide()
        window.deleteLater()
        app.processEvents()


def test_mstsc_launch_uses_active_rdp_forward(tmp_path, monkeypatch):
    import os
    if os.name != "nt":
        pytest.skip("Windows RDP client")
    from types import SimpleNamespace
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Store(tmp_path / "profiles.json"))
    launches, warnings, notices = [], [], []
    monkeypatch.setattr('ssh_jumper.ui.subprocess.Popen', lambda args: launches.append(args))
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: notices.append(args))
    try:
        window.forwards = [Forward(13389, "desktop", 3389), Forward(3389, "web", 80)]
        window.refresh_forwards()
        assert not window.mstsc_button.isHidden()
        assert not window.mstsc_button.isEnabled()
        window.open_mstsc()
        assert notices and not launches
        active = window.capture()
        thread = SimpleNamespace(is_alive=lambda: True)
        window.tunnel = SimpleNamespace(profile=active, thread=thread)
        window.set_status("Connected")
        window.forwards[0].local_port = 9999
        window.mstsc_button.click()
        assert launches == [["mstsc.exe", "/v:127.0.0.1:13389"]]
        window.forward_list.setCurrentRow(1)
        assert window.mstsc_button.isHidden()
        window.open_mstsc()
        assert len(launches) == 1
        window.forward_list.setCurrentRow(0)
        def missing_client(args):
            raise FileNotFoundError("MSTSC is unavailable")
        monkeypatch.setattr('ssh_jumper.ui.subprocess.Popen', missing_client)
        window.mstsc_button.click()
        assert warnings and "MSTSC is unavailable" in warnings[-1][2]
        thread.is_alive = lambda: False
        window.open_mstsc()
        assert len(notices) == 2
        window.set_status("Reconnecting")
        assert not window.mstsc_button.isEnabled()
        window.forward_list.setCurrentRow(-1)
        assert window.mstsc_button.isHidden()
    finally:
        window.tray.hide()
        window.deleteLater()
        app.processEvents()


def test_forward_editor_add_edit_remove_and_save(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path / "profiles.json")
    window = MainWindow(store)
    try:
        window.hops = [Hop("jump", username="user")]
        # Exercise the real dialog validation and the editor actions without modal waits.
        inputs = iter([(9000, "device-a", 443, 22), (9001, "device-b", 3389, 0)])
        def accept_dialog(dialog):
            local, host, port, health = next(inputs)
            dialog.local_port.setValue(local)
            dialog.target_host.setText(host)
            dialog.target_port.setValue(port)
            dialog.health_port.setValue(health)
            dialog.description.setText("Office desktop")
            dialog.accept()
            return dialog.result()
        monkeypatch.setattr(ForwardDialog, "exec", accept_dialog)
        window.add_forward()
        assert len(window.forwards) == 2
        window.edit_forward()
        assert window.forwards[1] == Forward(9001, "device-b", 3389, description="Office desktop")
        assert "Office desktop" in window.forward_list.item(1).text()
        reopened = ForwardDialog(window, window.forwards[1])
        assert reopened.description.text() == "Office desktop"
        assert window.save_profile()
        assert store.load()[0].forwards == window.forwards
        window.new_profile()
        window.profiles_list.setCurrentRow(0)
        assert window.forward_list.count() == 2
        assert window.forwards[1].target_host == "device-b"
        window.forward_list.setCurrentRow(0)
        window.remove_forward()
        assert window.save_profile()
        assert store.load()[0].forwards == [Forward(9001, "device-b", 3389, description="Office desktop")]
    finally:
        window.tray.hide()
        window.deleteLater()
        app.processEvents()


def test_forward_dialog_rejects_duplicate_local_port(monkeypatch):
    app = QApplication.instance() or QApplication([])
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, message: messages.append(message))
    dialog = ForwardDialog(None, Forward(8080), used_ports=[8080])
    dialog.accept()
    assert dialog.result() == 0
    assert "already used" in messages[0]
    dialog.local_port.setValue(8081)
    dialog.accept()
    assert dialog.result() == 1
    dialog.deleteLater()
    app.processEvents()


def test_alternatives_use_full_host_editor_and_cancel_is_isolated(monkeypatch):
    app = QApplication.instance() or QApplication([])
    original = Hop("primary", username="alice", auth="Password", password="primary-secret", multiple_hosts=True,
                   alternatives=[HostEndpoint("backup", 2222, "bob", "Pageant")])
    editor = HopDialog(None, original)
    def add_host(dialog):
        assert not dialog.allow_alternatives
        assert dialog.user.text() == ""  # new entries don't silently inherit credentials
        dialog.host.setText("third")
        dialog.user.setText("carol")
        dialog.auth.setCurrentText("Private key file")
        dialog.key.setText("backup-key")
        dialog.secret.setText("third-secret")
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(HopDialog, "exec", add_host)
    editor.add_alternative()
    assert editor.value().alternatives[-1] == HostEndpoint("third", 22, "carol", "Private key file", "third-secret", "backup-key")
    def edit_host(dialog):
        assert dialog.auth.currentText() == "Private key file"
        assert dialog.secret.text() == "third-secret"
        dialog.auth.setCurrentText("Password")
        dialog.secret.setText("changed-secret")
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(HopDialog, "exec", edit_host)
    editor.edit_alternative()
    assert editor.value().alternatives[-1].password == "changed-secret"
    assert editor.value().password == "primary-secret"
    assert len(original.alternatives) == 1
    editor.reject()
    assert original.alternatives == [HostEndpoint("backup", 2222, "bob", "Pageant")]
    editor.deleteLater()
    app.processEvents()


def test_session_only_save_keeps_memory_credentials(tmp_path):
    from ssh_jumper.models import Profile
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path / 'profiles.json')
    store.save([Profile(hops=[Hop('jump', username='user', auth='Password', password='session-secret')])])
    window = MainWindow(store)
    try:
        window.save_credentials.setChecked(False)
        assert window.save_profile()
        assert store.load()[0].hops[0].password == ''
        assert window.capture().hops[0].password == 'session-secret'
    finally:
        window.tray.hide()
        window.deleteLater()
        app.processEvents()
