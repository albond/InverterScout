"""JSON-based localization helpers for web and Telegram interfaces."""

from __future__ import annotations

import json
import string
from functools import lru_cache
from pathlib import Path
from typing import Any

from inverterscout.storage.encrypted import load_settings

LOCALES_DIR = Path(__file__).parents[1] / "resources" / "locales"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = {
    "en": {"name": "English", "direction": "ltr"},
    "uk": {"name": "Ukrainian", "direction": "ltr"},
    "es": {"name": "Spanish", "direction": "ltr"},
    "ar": {"name": "Arabic", "direction": "rtl"},
    "de": {"name": "German", "direction": "ltr"},
    "pl": {"name": "Polish", "direction": "ltr"},
    "ro": {"name": "Romanian", "direction": "ltr"},
    "ja": {"name": "Japanese", "direction": "ltr"},
    "ko": {"name": "Korean", "direction": "ltr"},
    "zh": {"name": "Chinese", "direction": "ltr"},
}


@lru_cache(maxsize=None)
def _load_catalog(language: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{language}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Locale catalog must be an object: {path}")
    return {str(key): str(value) for key, value in data.items()}


def current_language() -> str:
    language = str(load_settings().get("language", DEFAULT_LANGUAGE))
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def text_direction(language: str | None = None) -> str:
    code = language or current_language()
    return SUPPORTED_LANGUAGES.get(code, SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE])["direction"]


def translate(key: str, language: str | None = None, **values: Any) -> str:
    code = language or current_language()
    catalog = _load_catalog(code)
    fallback = _load_catalog(DEFAULT_LANGUAGE)
    text = catalog.get(key, fallback.get(key, key))
    try:
        return text.format(**values)
    except (KeyError, ValueError):
        return text


def validate_catalogs() -> dict[str, list[str]]:
    """Return missing keys for each supported non-English catalog."""
    baseline = set(_load_catalog(DEFAULT_LANGUAGE))
    return {
        language: sorted(baseline - set(_load_catalog(language)))
        for language in SUPPORTED_LANGUAGES
        if language != DEFAULT_LANGUAGE
    }


def validate_catalog_placeholders() -> dict[str, list[str]]:
    """Return keys whose format placeholders differ from the English catalog."""
    formatter = string.Formatter()

    def fields(text: str) -> set[str]:
        return {name for _, name, _, _ in formatter.parse(text) if name is not None}

    baseline = _load_catalog(DEFAULT_LANGUAGE)
    issues: dict[str, list[str]] = {}
    for language in SUPPORTED_LANGUAGES:
        if language == DEFAULT_LANGUAGE:
            continue
        catalog = _load_catalog(language)
        issues[language] = sorted(
            key
            for key, english_text in baseline.items()
            if key in catalog and fields(catalog[key]) != fields(english_text)
        )
    return issues
