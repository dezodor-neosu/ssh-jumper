"""Unit tests use an isolated keyring; desktop integration is checked separately."""
import os
import pytest


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    if os.name == "nt":
        return
    from keyring.backends.SecretService import Keyring
    secrets = {}
    monkeypatch.setattr(Keyring, "get_password", lambda self, service, user: secrets.get((service, user)))
    monkeypatch.setattr(Keyring, "set_password", lambda self, service, user, value: secrets.__setitem__((service, user), value))
