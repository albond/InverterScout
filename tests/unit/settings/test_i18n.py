"""Locale catalog contract tests."""

from inverterscout.settings.i18n import (
    SUPPORTED_LANGUAGES,
    text_direction,
    translate,
    validate_catalog_placeholders,
    validate_catalogs,
)


def test_all_supported_catalogs_have_the_english_keys():
    assert validate_catalogs() == {
        language: [] for language in SUPPORTED_LANGUAGES if language != "en"
    }


def test_translations_preserve_every_format_placeholder():
    assert validate_catalog_placeholders() == {
        language: [] for language in SUPPORTED_LANGUAGES if language != "en"
    }


def test_arabic_uses_rtl_and_other_languages_use_ltr():
    assert text_direction("ar") == "rtl"
    assert all(text_direction(code) == "ltr" for code in SUPPORTED_LANGUAGES if code != "ar")


def test_unknown_key_is_visible_for_translator_debugging():
    assert translate("missing.translation.key", language="en") == "missing.translation.key"


def test_telegram_help_keeps_stable_command_names_in_every_language():
    command_names = (
        "start",
        "stop",
        "battery",
        "help",
        "devices",
        "device_on",
        "device_off",
    )
    for language in SUPPORTED_LANGUAGES:
        help_text = translate("telegram.help_full", language=language)
        assert all(f"/{command}" in help_text for command in command_names)


def test_non_english_telegram_statuses_have_no_known_english_placeholders():
    forbidden_fragments = ("LOW VOLTAGE", "UNAVAILABLE", "RUNNING", "10 seconds")
    keys = (
        "telegram.alert_no_consumption",
        "telegram.source_low_voltage",
        "telegram.source_no_grid",
        "telegram.generator_running",
    )
    for language in SUPPORTED_LANGUAGES:
        if language == "en":
            continue
        for key in keys:
            text = translate(key, language=language)
            assert not any(fragment in text for fragment in forbidden_fragments)
