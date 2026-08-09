"""Logging helpers that keep configured credentials out of process output."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable

SENSITIVE_ENVIRONMENT_NAMES = (
    "INVERTERSCOUT_MASTER_KEY",
    "TELEGRAM_TOKEN",
    "ADMIN_CHAT_ID",
    "TAPO_USERNAME",
    "TAPO_PASSWORD",
    "TUYA_ACCESS_ID",
    "TUYA_ACCESS_SECRET",
    "DONGLE_SERIAL",
    "INVERTER_SERIAL",
    "INVERTER_HOST",
)

SENSITIVE_SETTING_NAMES = (
    "telegram_token",
    "admin_chat_id",
    "tapo_username",
    "tapo_password",
    "tuya_access_id",
    "tuya_access_secret",
    "dongle_serial",
    "inverter_serial",
    "inverter_host",
)


def sensitive_values(settings: dict | None = None) -> tuple[str, ...]:
    """Collect non-trivial secret values without logging their names or values."""
    values: set[str] = set()
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        value = os.getenv(name, "").strip()
        if len(value) >= 4:
            values.add(value)
    for name in SENSITIVE_SETTING_NAMES:
        value = str((settings or {}).get(name, "")).strip()
        if len(value) >= 4:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


class RedactingFormatter(logging.Formatter):
    """Replace known credentials after the complete log record is formatted."""

    def __init__(self, format_string: str, secrets: Iterable[str] = ()):
        super().__init__(format_string)
        self._secrets = tuple(value for value in secrets if len(value) >= 4)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


def stdout_handler(format_string: str, secrets: Iterable[str] = ()) -> logging.Handler:
    """Build a stdout handler that redacts credentials, including tracebacks."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(format_string, secrets))
    return handler
