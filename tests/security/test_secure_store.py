"""Tests for authenticated encrypted persistence."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from inverterscout.storage.encrypted import EncryptedStore, SecureJsonPath, SecureStoreError


def make_store(tmp_path):
    return EncryptedStore(
        tmp_path / "state.db",
        tmp_path / "state.key",
        key=Fernet.generate_key(),
    )


def test_round_trip_keeps_plaintext_out_of_database(tmp_path):
    store = make_store(tmp_path)
    secret = "synthetic-secret-value"
    store.set_json("settings", {"token": secret, "enabled": True})

    assert store.get_json("settings") == {"token": secret, "enabled": True}
    assert secret.encode() not in (tmp_path / "state.db").read_bytes()
    assert b"settings" not in (tmp_path / "state.db").read_bytes()


def test_tampered_ciphertext_fails_closed(tmp_path):
    store = make_store(tmp_path)
    store.set_json("settings", {"safe": True})
    with closing(sqlite3.connect(tmp_path / "state.db")) as connection:
        connection.execute("UPDATE secure_records SET ciphertext = ?", (b"tampered",))
        connection.commit()

    with pytest.raises(SecureStoreError):
        store.get_json("settings")


def test_store_closes_every_database_connection(tmp_path):
    real_connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    with patch("inverterscout.storage.encrypted.sqlite3.connect", side_effect=tracked_connect):
        store = make_store(tmp_path)
        store.set_json("settings", {"safe": True})
        assert store.get_json("settings") == {"safe": True}

    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_secure_json_path_compatibility(tmp_path):
    store = make_store(tmp_path)
    path = SecureJsonPath(store, "devices")
    payload = [{"id": "sample", "config": {"password": "example"}}]

    assert not path.exists()
    path.write_text(json.dumps(payload))
    assert path.exists()
    assert json.loads(path.read_text()) == payload
