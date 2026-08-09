"""Runtime configuration, formatting, and subscriber access."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from inverterscout import __version__
from inverterscout.settings.i18n import translate
from inverterscout.storage.encrypted import load_settings, secure_json_path

logger = logging.getLogger(__name__)

APP_VERSION = __version__

_settings = load_settings()


def _setting(name: str, environment_name: str, default: Any = "") -> Any:
    """Read a safe runtime override, then encrypted setup, then a default."""
    environment_value = os.getenv(environment_name)
    if environment_value is not None and environment_value != "":
        return environment_value
    return _settings.get(name, default)


TELEGRAM_MODE = str(_setting("telegram_mode", "TELEGRAM_MODE", "disabled"))
TELEGRAM_TOKEN = str(_setting("telegram_token", "TELEGRAM_TOKEN", ""))
INVERTER_HOST = str(_setting("inverter_host", "INVERTER_HOST", ""))
INVERTER_PORT = int(_setting("inverter_port", "INVERTER_PORT", 8000))
POLL_INTERVAL = int(_setting("poll_interval", "POLL_INTERVAL", 60))
ADMIN_CHAT_ID = int(_setting("admin_chat_id", "ADMIN_CHAT_ID", 0))
WEB_PORT = int(_setting("web_port", "WEB_PORT", 8080))
TIMEZONE = str(_setting("timezone", "TZ", "UTC"))
LANGUAGE = str(_setting("language", "INVERTERSCOUT_LANGUAGE", "en"))
DONGLE_SERIAL = str(_setting("dongle_serial", "DONGLE_SERIAL", ""))
INVERTER_SERIAL = str(_setting("inverter_serial", "INVERTER_SERIAL", ""))

TUYA_ACCESS_ID = str(_setting("tuya_access_id", "TUYA_ACCESS_ID", ""))
TUYA_ACCESS_SECRET = str(_setting("tuya_access_secret", "TUYA_ACCESS_SECRET", ""))
TUYA_REGION = str(_setting("tuya_region", "TUYA_REGION", "eu"))
TAPO_USERNAME = str(_setting("tapo_username", "TAPO_USERNAME", ""))
TAPO_PASSWORD = str(_setting("tapo_password", "TAPO_PASSWORD", ""))

SUBSCRIBERS_FILE = secure_json_path("telegram.subscribers")
PENDING_FILE = secure_json_path("telegram.pending")
BLOCKED_FILE = secure_json_path("telegram.blocked")
USER_NAMES_FILE = secure_json_path("telegram.user_names")


def ts_to_date(timestamp: float) -> str:
    """Convert a Unix timestamp to a locale-neutral date."""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def ts_to_time(timestamp: float) -> str:
    """Convert a Unix timestamp to local time."""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def format_time_human(timestamp: float, language: str | None = None) -> str:
    """Format a timestamp as a compact localized relative value."""
    event_time = datetime.fromtimestamp(timestamp)
    now = datetime.now()
    time_text = event_time.strftime("%H:%M")
    delta_days = (now.date() - event_time.date()).days

    if delta_days == 0:
        when = translate("time.today_at", language=language, time=time_text)
    elif delta_days == 1:
        when = translate("time.yesterday_at", language=language, time=time_text)
    else:
        when = event_time.strftime("%Y-%m-%d %H:%M")

    seconds = max(0, int((now - event_time).total_seconds()))
    if seconds < 30:
        ago = translate("time.just_now", language=language)
    elif seconds < 60:
        ago = translate("time.less_than_minute_ago", language=language)
    elif seconds < 3600:
        ago = translate("time.minutes_ago", language=language, count=seconds // 60)
    else:
        hours, remainder = divmod(seconds // 60, 60)
        ago = translate(
            "time.hours_minutes_ago",
            language=language,
            hours=hours,
            minutes=remainder,
        )
    return f"{when} ({ago})"


def format_duration(seconds: int, language: str | None = None) -> str:
    """Format a duration for the configured interface language."""
    if seconds < 60:
        return translate("duration.less_than_minute", language=language)
    total_minutes = seconds // 60
    if total_minutes < 60:
        return translate("duration.minutes", language=language, count=total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if minutes:
        return translate("duration.hours_minutes", language=language, hours=hours, minutes=minutes)
    return translate("duration.hours", language=language, count=hours)


def estimate_battery_runtime(
    soc: int,
    grid_lost_time: float,
    generator_on: bool = False,
    pre_gen_soc: int = 0,
    pre_gen_time: float = 0,
    language: str | None = None,
) -> dict | None:
    """Estimate remaining battery time from observed discharge since grid loss."""
    now = datetime.now()
    now_timestamp = now.timestamp()

    if grid_lost_time <= 0 or soc >= 100 or soc <= 10:
        return None

    if generator_on:
        if pre_gen_soc <= 0 or pre_gen_soc >= 100 or pre_gen_time <= grid_lost_time:
            return None
        baseline_elapsed = pre_gen_time - grid_lost_time
        if baseline_elapsed < 120:
            return None
        baseline_discharged = 100 - pre_gen_soc
        if baseline_discharged <= 0:
            return None
        rate = baseline_discharged / baseline_elapsed
    else:
        elapsed = now_timestamp - grid_lost_time
        if elapsed < 120:
            return None
        discharged = 100 - soc
        if discharged <= 0:
            return None
        rate = discharged / elapsed

    remaining_seconds = int((soc - 10) / rate)
    depletion = datetime.fromtimestamp(now_timestamp + remaining_seconds)
    day_delta = (depletion.date() - now.date()).days
    depletion_time = depletion.strftime("%H:%M")
    if day_delta == 0:
        depletion_text = translate("time.today_at", language=language, time=depletion_time)
    elif day_delta == 1:
        depletion_text = translate("time.tomorrow_at", language=language, time=depletion_time)
    else:
        depletion_text = depletion.strftime("%Y-%m-%d %H:%M")

    return {
        "remaining_sec": remaining_seconds,
        "remaining_text": format_duration(remaining_seconds, language=language),
        "depletion_time_text": depletion_text,
    }


class SubscriberManager:
    """Manage approved, pending, and blocked Telegram users."""

    def __init__(self):
        self.subscribers: set[int] = set()
        self.pending: list[dict] = []
        self.blocked: set[int] = set()
        self.user_names: dict[int, dict] = {}

    def load_all(self) -> None:
        self.subscribers = self._load_set(SUBSCRIBERS_FILE)
        self.pending = self._load_list(PENDING_FILE)
        self.blocked = self._load_set(BLOCKED_FILE)
        self.user_names = self._load_user_names()

    def save_subscribers(self) -> None:
        self._save_set(SUBSCRIBERS_FILE, self.subscribers)

    def save_pending(self) -> None:
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        PENDING_FILE.write_text(json.dumps(self.pending, ensure_ascii=False, indent=2))

    def save_blocked(self) -> None:
        self._save_set(BLOCKED_FILE, self.blocked)

    def save_user_names(self) -> None:
        USER_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_NAMES_FILE.write_text(
            json.dumps(
                {str(key): value for key, value in self.user_names.items()},
                ensure_ascii=False,
                indent=2,
            )
        )

    def set_user_name(self, chat_id: int, first_name: str = "", username: str = "") -> None:
        self.user_names[chat_id] = {"first_name": first_name, "username": username}
        self.save_user_names()

    def get_display_name(self, chat_id: int) -> str:
        information = self.user_names.get(chat_id, {})
        return information.get("first_name", "") or "—"

    def get_username(self, chat_id: int) -> str:
        information = self.user_names.get(chat_id, {})
        username = information.get("username", "")
        return f"@{username}" if username else "—"

    @staticmethod
    def _load_set(path) -> set[int]:
        if path.exists():
            try:
                return set(json.loads(path.read_text()))
            except (json.JSONDecodeError, TypeError) as error:
                logger.error("Cannot read %s: %s", path, error)
        return set()

    @staticmethod
    def _save_set(path, data: set[int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(data)))

    @staticmethod
    def _load_list(path) -> list[dict]:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, TypeError) as error:
                logger.error("Cannot read %s: %s", path, error)
        return []

    @staticmethod
    def _load_user_names() -> dict[int, dict]:
        if USER_NAMES_FILE.exists():
            try:
                raw = json.loads(USER_NAMES_FILE.read_text())
                return {int(key): value for key, value in raw.items()}
            except (json.JSONDecodeError, TypeError) as error:
                logger.error("Cannot read %s: %s", USER_NAMES_FILE, error)
        return {}


sub_mgr = SubscriberManager()
