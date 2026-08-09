"""Encrypted local persistence for InverterScout.

Application records are stored as Fernet ciphertexts in SQLite. Record names are
hashed so database inspection does not reveal which features are configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken


class SecureStoreError(RuntimeError):
    """Raised when encrypted persistence cannot be opened or authenticated."""


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


class EncryptedStore:
    """Small encrypted document store backed by SQLite."""

    def __init__(self, database_path: Path, key_path: Path, key: bytes | None = None):
        self.database_path = database_path
        self.key_path = key_path
        self._lock = threading.RLock()
        self._fernet = Fernet(key or self._load_or_create_key())
        self._initialize_database()

    def _load_or_create_key(self) -> bytes:
        configured_key = os.getenv("INVERTERSCOUT_MASTER_KEY", "").strip()
        if configured_key:
            try:
                Fernet(configured_key.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as error:
                raise SecureStoreError(
                    "INVERTERSCOUT_MASTER_KEY must be a valid Fernet key"
                ) from error
            return configured_key.encode("ascii")

        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            try:
                Fernet(key)
            except (ValueError, InvalidToken) as error:
                raise SecureStoreError(f"Invalid encryption key: {self.key_path}") from error
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
            return key

        key = Fernet.generate_key()
        try:
            _private_write(self.key_path, key + b"\n")
        except FileExistsError:
            return self.key_path.read_bytes().strip()
        return key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back a short transaction and always close it."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS secure_records (
                    key_hash TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _key_hash(name: str) -> str:
        return hashlib.sha256(name.encode("utf-8")).hexdigest()

    def contains(self, name: str) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM secure_records WHERE key_hash = ?",
                (self._key_hash(name),),
            ).fetchone()
        return row is not None

    def set_json(self, name: str, value: Any) -> None:
        envelope = json.dumps(
            {"name": name, "value": value},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = self._fernet.encrypt(envelope)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO secure_records(key_hash, ciphertext, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key_hash) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    updated_at = excluded.updated_at
                """,
                (self._key_hash(name), ciphertext, int(time.time())),
            )

    def get_json(self, name: str, default: Any = None) -> Any:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM secure_records WHERE key_hash = ?",
                (self._key_hash(name),),
            ).fetchone()
        if row is None:
            return default
        try:
            envelope = json.loads(self._fernet.decrypt(row[0]).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SecureStoreError(
                "Encrypted data cannot be authenticated. Check the master key."
            ) from error
        if envelope.get("name") != name:
            raise SecureStoreError("Encrypted record identity check failed")
        return envelope.get("value", default)

    def delete(self, name: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM secure_records WHERE key_hash = ?", (self._key_hash(name),)
            )

    def verify(self) -> bool:
        """Authenticate every encrypted record without returning its contents."""
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT ciphertext FROM secure_records").fetchall()
        for (ciphertext,) in rows:
            try:
                self._fernet.decrypt(ciphertext)
            except InvalidToken:
                return False
        return True


class _SecureParent:
    """Compatibility shim for the subset of pathlib used by legacy modules."""

    def mkdir(self, *args, **kwargs) -> None:
        return None


class SecureJsonPath:
    """Path-like JSON record backed by :class:`EncryptedStore`."""

    parent = _SecureParent()

    def __init__(self, store: EncryptedStore, record_name: str):
        self.store = store
        self.record_name = record_name

    def exists(self) -> bool:
        return self.store.contains(self.record_name)

    def read_text(self, *args, **kwargs) -> str:
        value = self.store.get_json(self.record_name)
        if value is None and not self.exists():
            raise FileNotFoundError(self.record_name)
        return json.dumps(value, ensure_ascii=False)

    def write_text(self, text: str, *args, **kwargs) -> int:
        value = json.loads(text)
        self.store.set_json(self.record_name, value)
        return len(text)

    def __str__(self) -> str:
        return f"encrypted-db:{self.record_name}"

    def __repr__(self) -> str:
        return f"SecureJsonPath({self.record_name!r})"


DATA_DIR = Path(os.getenv("INVERTERSCOUT_DATA_DIR", "data"))
DATABASE_PATH = Path(os.getenv("INVERTERSCOUT_DATABASE", str(DATA_DIR / "inverterscout.db")))
KEY_PATH = Path(os.getenv("INVERTERSCOUT_KEY_FILE", str(DATA_DIR / ".master.key")))

_default_store: EncryptedStore | None = None
_default_store_lock = threading.Lock()


def get_store() -> EncryptedStore:
    """Return the process-wide encrypted store."""
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                _default_store = EncryptedStore(DATABASE_PATH, KEY_PATH)
    return _default_store


def secure_json_path(name: str) -> SecureJsonPath:
    return SecureJsonPath(get_store(), name)


def load_settings() -> dict[str, Any]:
    return get_store().get_json("settings", {})


def save_settings(settings: dict[str, Any]) -> None:
    get_store().set_json("settings", settings)


def setup_is_complete() -> bool:
    settings = load_settings()
    if not settings.get("setup_complete"):
        return False
    telegram_mode = settings.get("telegram_mode")
    if telegram_mode not in {"enabled", "disabled"}:
        return False
    if telegram_mode == "enabled":
        return bool(settings.get("telegram_token") and settings.get("admin_chat_id"))
    return True
