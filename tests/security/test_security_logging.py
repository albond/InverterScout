"""Tests for credential-safe process logging."""

import logging

from inverterscout.security.logging import RedactingFormatter, sensitive_values


def test_redacting_formatter_removes_secrets_from_message_and_exception(monkeypatch):
    secret = "tuya-secret-value"
    formatter = RedactingFormatter("%(levelname)s %(message)s", [secret])
    try:
        raise RuntimeError(f"upstream returned {secret}")
    except RuntimeError:
        record = logging.getLogger("test").makeRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request failed with %s",
            (secret,),
            __import__("sys").exc_info(),
        )

    rendered = formatter.format(record)
    assert secret not in rendered
    assert rendered.count("[REDACTED]") >= 2


def test_sensitive_values_collects_account_credentials(monkeypatch):
    monkeypatch.setenv("TAPO_USERNAME", "private-account-id")
    monkeypatch.setenv("TUYA_ACCESS_SECRET", "developer-secret")
    monkeypatch.setenv("INVERTERSCOUT_MASTER_KEY", "external-master-key")

    values = sensitive_values({"telegram_token": "123456:private-token"})

    assert "private-account-id" in values
    assert "developer-secret" in values
    assert "external-master-key" in values
    assert "123456:private-token" in values
