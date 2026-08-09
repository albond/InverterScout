"""Validation and rendering tests for the first-run configuration gate."""

from pathlib import Path

from inverterscout.settings.timezones import IANA_TIMEZONES
from inverterscout.settings.wizard import validate_setup_form


def valid_form():
    return {
        "language": "en",
        "timezone": "Europe/Warsaw",
        "inverter_host": "192.0.2.50",
        "inverter_port": "8000",
        "dongle_serial": "AB12345678",
        "inverter_serial": "CD12345678",
        "poll_interval": "60",
        "telegram_mode": "disabled",
        "telegram_token": "",
        "admin_chat_id": "",
    }


def test_setup_accepts_explicit_no_telegram_mode():
    settings, errors = validate_setup_form(valid_form())
    assert errors == {}
    assert settings["setup_complete"] is True
    assert settings["telegram_mode"] == "disabled"


def test_setup_requires_explicit_telegram_choice():
    form = valid_form()
    form["telegram_mode"] = ""
    settings, errors = validate_setup_form(form)
    assert "telegram_mode" in errors
    assert settings["setup_complete"] is False


def test_setup_requires_credentials_when_telegram_enabled():
    form = valid_form()
    form["telegram_mode"] = "enabled"
    settings, errors = validate_setup_form(form)
    assert "telegram" in errors
    assert settings["setup_complete"] is False


def test_setup_rejects_invalid_serial_and_timezone():
    form = valid_form()
    form["dongle_serial"] = "too-short"
    form["timezone"] = "Not/AZone"
    _, errors = validate_setup_form(form)
    assert errors.keys() >= {"dongle_serial", "timezone"}


def test_timezone_autocomplete_uses_the_full_sorted_iana_catalog():
    assert IANA_TIMEZONES == tuple(sorted(IANA_TIMEZONES))
    assert len(IANA_TIMEZONES) > 400
    assert {"UTC", "America/New_York", "Asia/Tokyo", "Europe/Kyiv"} <= set(IANA_TIMEZONES)


def test_setup_timezone_field_uses_autocomplete_catalog():
    project_root = Path(__file__).parents[3]
    template = (
        project_root / "src" / "inverterscout" / "resources" / "templates" / "setup.html"
    ).read_text()
    assert 'name="timezone"' in template
    assert 'list="timezones"' in template
    assert '<datalist id="timezones"' in template
    assert "{% for timezone in timezones %}" in template


def test_setup_template_never_echoes_a_posted_telegram_token():
    project_root = Path(__file__).parents[3]
    template = (
        project_root / "src" / "inverterscout" / "resources" / "templates" / "setup.html"
    ).read_text()
    assert "form.telegram_token" not in template
    assert 'name="telegram_token" value=""' in template
