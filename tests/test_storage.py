from ssh_jumper.models import Forward, Hop, Profile
from ssh_jumper.storage import Store


def test_endpoint_credentials_encrypted_and_v4_upgrade(tmp_path):
    import json
    from ssh_jumper.models import SSHAuth
    store = Store(tmp_path / "profiles.json")
    old = {"version": 4, "profiles": [{"name": "Old", "hops": [{"host": "jump", "username": "user"}],
        "forwards": [{"local_port": 2222, "target_host": "device", "target_port": 22}]}]}
    original = json.dumps(old)
    store.path.write_text(original, encoding="utf-8")
    profiles = store.load()
    assert profiles[0].forwards[0].ssh is None
    profiles[0].forwards[0].ssh = SSHAuth("alice", "Password", "endpoint-secret-Ж")
    store.save(profiles)
    assert store.load() == profiles
    assert "endpoint-secret" not in store.path.read_text()
    assert '"password"' not in store.path.read_text()
    assert store.path.with_suffix(".v4.bak").read_text(encoding="utf-8") == original


def test_dpapi_round_trip(tmp_path):
    store = Store(tmp_path / "profiles.json")
    profile = Profile(name="Lab", hops=[Hop("bastion", username="alice", auth="Password", password="unusual-secret-Ж")])
    store.save([profile])
    text = store.path.read_text()
    assert "unusual-secret" not in text
    assert "protected_password" in text
    assert store.load() == [profile]


def test_corrupt_storage_is_not_overwritten(tmp_path):
    import pytest
    store = Store(tmp_path / "profiles.json")
    store.path.write_text("broken")
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_text() == "broken"


def test_multiple_forwards_round_trip(tmp_path):
    store = Store(tmp_path / "profiles.json")
    profile = Profile(hops=[Hop("jump", username="user")],
        forwards=[Forward(8443, "web.internal", 443, 22), Forward(13389, "desktop.internal", 3389)])
    store.save([profile])
    assert store.load() == [profile]


def test_v1_migrates_without_losing_credentials_and_keeps_backup(tmp_path):
    import base64
    import json
    from ssh_jumper.storage import crypt
    store = Store(tmp_path / "profiles.json")
    legacy = {"version": 1, "profiles": [{"name": "Old tunnel",
        "hops": [{"host": "jump", "username": "user", "auth": "Password",
                  "protected_password": base64.b64encode(crypt(b"old-secret")).decode()}],
        "local_port": 8443, "target_host": "device", "target_port": 443,
        "health_port": 22, "interval": 30, "reconnect": False, "ignore_host_keys": False}]}
    original = json.dumps(legacy)
    store.path.write_text(original, encoding="utf-8")
    profiles = store.load()
    assert profiles[0].forwards == [Forward(8443, "device", 443, 22)]
    assert profiles[0].hops[0].password == "old-secret"
    assert not profiles[0].reconnect and not profiles[0].ignore_host_keys
    assert profiles[0].interval == 30
    assert store.path.read_text(encoding="utf-8") == original
    profiles[0].forwards.append(Forward(9000, "other-device", 9000))
    store.save(profiles)
    assert store.load() == profiles
    assert json.loads(store.path.read_text())["version"] == 5
    assert store.path.with_suffix(".v1.bak").read_text(encoding="utf-8") == original


def test_alternative_hosts_round_trip_and_v2_upgrade(tmp_path):
    import json
    from ssh_jumper.models import HostEndpoint
    store = Store(tmp_path / "profiles.json")
    old = {"version": 2, "profiles": [{"name": "Existing", "hops": [{"host": "first", "username": "user"}],
                                      "forwards": [{"local_port": 8080, "target_host": "device", "target_port": 80}]}]}
    store.path.write_text(json.dumps(old), encoding="utf-8")
    profile = store.load()[0]
    assert not profile.hops[0].multiple_hosts
    assert profile.hops[0].alternatives == []
    profile.hops[0].multiple_hosts = True
    profile.hops[0].alternatives = [HostEndpoint("second", 2222, "alice", "Password", "backup-secret"),
                                    HostEndpoint("third", 22, "bob", "Private key file", "key-passphrase", "id_ed25519")]
    store.save([profile])
    assert store.load() == [profile]
    saved = store.path.read_text()
    assert "backup-secret" not in saved and "key-passphrase" not in saved
    assert '"password"' not in saved
    assert json.loads(store.path.with_suffix(".v2.bak").read_text()) == old


def test_v3_alternatives_inherit_credentials_once_during_migration(tmp_path):
    import base64
    import json
    from ssh_jumper.storage import crypt
    from ssh_jumper.models import HostEndpoint
    store = Store(tmp_path / "profiles.json")
    old = {"version": 3, "profiles": [{"name": "Shared credentials", "hops": [{
        "host": "primary", "username": "alice", "auth": "Private key file", "key_file": "id_rsa",
        "protected_password": base64.b64encode(crypt(b"old-passphrase")).decode(),
        "multiple_hosts": True, "alternatives": [{"host": "backup", "port": 2222}]}],
        "forwards": [{"local_port": 8080, "target_host": "device", "target_port": 80}]}]}
    store.path.write_text(json.dumps(old), encoding="utf-8")
    profiles = store.load()
    backup = profiles[0].hops[0].alternatives[0]
    assert backup == HostEndpoint("backup", 2222, "alice", "Private key file", "old-passphrase", "id_rsa")
    profiles[0].hops[0].username = "changed-parent"
    profiles[0].hops[0].password = "new-parent-secret"
    assert backup.username == "alice" and backup.password == "old-passphrase"
    store.save(profiles)
    assert store.load() == profiles
    assert "old-passphrase" not in store.path.read_text()
    assert json.loads(store.path.with_suffix(".v3.bak").read_text()) == old
