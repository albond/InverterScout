"""First-run setup server for InverterScout."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp_jinja2
import jinja2
from aiohttp import web

from inverterscout.settings.i18n import SUPPORTED_LANGUAGES, text_direction, translate
from inverterscout.settings.timezones import IANA_TIMEZONES
from inverterscout.storage.encrypted import save_settings, setup_is_complete

logger = logging.getLogger(__name__)

_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)
_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9]{10}$")
_TELEGRAM_TOKEN_PATTERN = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{20,}$")


def _is_valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOST_PATTERN.fullmatch(value))


def _integer(value: str, minimum: int, maximum: int) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def validate_setup_form(form: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate and normalize first-run settings without network side effects."""
    errors: dict[str, str] = {}
    language = form.get("language", "en")
    if language not in SUPPORTED_LANGUAGES:
        errors["language"] = "setup.error_language"
        language = "en"

    timezone = form.get("timezone", "").strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors["timezone"] = "setup.error_timezone"

    inverter_host = form.get("inverter_host", "").strip()
    if not _is_valid_host(inverter_host):
        errors["inverter_host"] = "setup.error_host"

    inverter_port = _integer(form.get("inverter_port", ""), 1, 65535)
    if inverter_port is None:
        errors["inverter_port"] = "setup.error_port"

    web_port = _integer(os.getenv("WEB_PORT", "8080"), 1, 65535) or 8080

    poll_interval = _integer(form.get("poll_interval", ""), 10, 3600)
    if poll_interval is None:
        errors["poll_interval"] = "setup.error_poll_interval"

    dongle_serial = form.get("dongle_serial", "").strip()
    if not _SERIAL_PATTERN.fullmatch(dongle_serial):
        errors["dongle_serial"] = "setup.error_serial"

    inverter_serial = form.get("inverter_serial", "").strip()
    if not _SERIAL_PATTERN.fullmatch(inverter_serial):
        errors["inverter_serial"] = "setup.error_serial"

    telegram_mode = form.get("telegram_mode", "")
    telegram_token = form.get("telegram_token", "").strip()
    admin_chat_id_text = form.get("admin_chat_id", "").strip()
    admin_chat_id = None
    if telegram_mode not in {"enabled", "disabled"}:
        errors["telegram_mode"] = "setup.telegram_required"
    elif telegram_mode == "enabled":
        try:
            admin_chat_id = int(admin_chat_id_text)
        except ValueError:
            admin_chat_id = None
        if not _TELEGRAM_TOKEN_PATTERN.fullmatch(telegram_token) or not admin_chat_id:
            errors["telegram"] = "setup.error_telegram"
    else:
        telegram_token = ""

    settings = {
        "setup_complete": not errors,
        "language": language,
        "timezone": timezone,
        "inverter_host": inverter_host,
        "inverter_port": inverter_port or 8000,
        "dongle_serial": dongle_serial,
        "inverter_serial": inverter_serial,
        "poll_interval": poll_interval or 60,
        "telegram_mode": telegram_mode,
        "telegram_token": telegram_token,
        "admin_chat_id": admin_chat_id or 0,
        "web_port": web_port,
        "tapo_username": "",
        "tapo_password": "",
        "tuya_access_id": "",
        "tuya_access_secret": "",
        "tuya_region": "eu",
    }
    return settings, errors


def _default_form(language: str) -> dict[str, str]:
    return {
        "language": language,
        "timezone": os.getenv("TZ", "UTC"),
        "inverter_host": "",
        "inverter_port": "8000",
        "dongle_serial": "",
        "inverter_serial": "",
        "poll_interval": "60",
        "telegram_mode": "",
        "telegram_token": "",
        "admin_chat_id": "",
    }


async def run_setup_wizard() -> None:
    """Block startup until a valid setup form has been saved."""
    if setup_is_complete():
        return

    bind_host = os.getenv("SETUP_BIND_HOST", "127.0.0.1")
    setup_port = _integer(os.getenv("WEB_PORT", "8080"), 1, 65535) or 8080
    csrf_token = secrets.token_urlsafe(32)
    completed = asyncio.Event()

    app = web.Application(client_max_size=64 * 1024)
    template_dir = Path(__file__).parents[1] / "resources" / "templates"
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(template_dir))

    @web.middleware
    async def security_headers(request: web.Request, handler):
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.middlewares.append(security_headers)

    static_dir = Path(__file__).parents[1] / "resources" / "static"
    app.router.add_static("/static", static_dir)

    def context(form: dict[str, str], errors: dict[str, str] | None = None) -> dict:
        language = form.get("language", "en")
        if language not in SUPPORTED_LANGUAGES:
            language = "en"

        def local_translate(key: str) -> str:
            return translate(key, language=language)

        return {
            "t": local_translate,
            "language": language,
            "direction": text_direction(language),
            "languages": SUPPORTED_LANGUAGES,
            "timezones": IANA_TIMEZONES,
            "form": form,
            "errors": errors or {},
            "csrf_token": csrf_token,
        }

    async def setup_get(request: web.Request) -> web.Response:
        requested_language = request.query.get("lang", "en")
        if requested_language not in SUPPORTED_LANGUAGES:
            requested_language = "en"
        response = aiohttp_jinja2.render_template(
            "setup.html", request, context(_default_form(requested_language))
        )
        return response

    async def setup_post(request: web.Request) -> web.Response:
        posted = await request.post()
        form = {key: str(value) for key, value in posted.items()}
        language = form.get("language", "en")
        if not secrets.compare_digest(form.get("csrf_token", ""), csrf_token):
            raise web.HTTPForbidden(text="Invalid setup session")

        settings, errors = validate_setup_form(form)
        if errors:
            response = aiohttp_jinja2.render_template(
                "setup.html", request, context(form, errors), status=400
            )
            return response

        save_settings(settings)
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, completed.set)
        response = aiohttp_jinja2.render_template(
            "setup_complete.html",
            request,
            {
                "t": lambda key: translate(key, language=language),
                "language": language,
                "direction": text_direction(language),
            },
        )
        return response

    app.router.add_get("/", setup_get)
    app.router.add_post("/", setup_post)
    app.router.add_get("/setup", setup_get)
    app.router.add_post("/setup", setup_post)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, bind_host, setup_port)
    await site.start()
    visible_host = "localhost" if bind_host in {"0.0.0.0", "127.0.0.1"} else bind_host
    logger.warning("Setup required: open http://%s:%d", visible_host, setup_port)
    try:
        await completed.wait()
    finally:
        await runner.cleanup()
