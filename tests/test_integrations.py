import json
from pathlib import Path
import pytest
from ssh_jumper.integrations import IntegrationSettings, SettingsStore, launch_editor


def test_editor_settings_roundtrip_and_migration(tmp_path):
    editor = tmp_path / "My Editor.exe"
    editor.write_bytes(b"editor")
    editor.chmod(0o700)
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load() == IntegrationSettings()
    store.path.write_text(json.dumps({"version": 1, "integrations": {"terminal": "KiTTY", "putty_path": "missing.exe"}}))
    assert store.load() == IntegrationSettings()
    settings = IntegrationSettings(str(editor), ["-multiInst"])
    store.save(settings)
    assert store.load() == settings
    assert json.loads(store.path.read_text())["version"] == 2
    with pytest.raises(ValueError):
        store.save(IntegrationSettings("missing.exe"))
    with pytest.raises(ValueError):
        store.save(IntegrationSettings(str(editor), "invalid"))


def test_launch_editor_keeps_arguments_separate(tmp_path, monkeypatch):
    editor = tmp_path / "My Editor.exe"
    editor.write_bytes(b"editor")
    editor.chmod(0o700)
    file = tmp_path / "some file & name.ini"
    file.write_bytes(b"test")
    calls = []
    monkeypatch.setattr("ssh_jumper.integrations.subprocess.Popen", lambda *a, **kw: calls.append((a, kw)))
    launch_editor(IntegrationSettings(str(editor), ["--new-window"]), file)
    args, kwargs = calls[0]
    assert args[0] == [str(editor.resolve()), "--new-window", str(file.resolve())]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == str(tmp_path)
    with pytest.raises(ValueError, match="App settings"):
        launch_editor(IntegrationSettings(), file)
