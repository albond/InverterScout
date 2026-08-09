"""Local-only web status and administration interface for InverterScout."""

import asyncio
import json
import logging
import secrets
import socket
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import aiohttp_jinja2
    import jinja2
    from aiohttp import web

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

from inverterscout.core.state import Event, StateManager
from inverterscout.devices.manager import DeviceManager
from inverterscout.devices.tuya import (
    SUPPORTED_TUYA_CLOUD_REGIONS,
    SUPPORTED_TUYA_PROTOCOL_VERSIONS,
)
from inverterscout.settings.i18n import (
    SUPPORTED_LANGUAGES,
    current_language,
    text_direction,
)
from inverterscout.settings.i18n import (
    translate as translate_catalog,
)
from inverterscout.settings.runtime import (
    APP_VERSION,
    INVERTER_HOST,
    INVERTER_PORT,
    TAPO_PASSWORD,
    TAPO_USERNAME,
    TELEGRAM_MODE,
    TUYA_ACCESS_ID,
    TUYA_ACCESS_SECRET,
    TUYA_REGION,
    WEB_PORT,
    estimate_battery_runtime,
    format_time_human,
    sub_mgr,
)
from inverterscout.settings.timezones import IANA_TIMEZONES
from inverterscout.storage.encrypted import load_settings, save_settings

logger = logging.getLogger(__name__)


async def _sse_send(resp: "web.StreamResponse", data: str) -> None:
    """Send an SSE event and drain the buffer."""
    await resp.write(f"data: {data}\n\n".encode())
    await resp.drain()


# ──────────────────────────────────────────────
# Navigation Constants
# ──────────────────────────────────────────────
NAV_LINKS = [
    ("/", "nav.status"),
    ("/devices", "nav.devices"),
    ("/device-log", "nav.device_log"),
    ("/admin", "nav.access"),
    ("/settings", "nav.settings"),
]

_TRIGGER_LABELS = {
    "on_battery": "status.no_grid",
    "off_battery": "status.grid_ok",
    "grid_lost": "status.no_grid",
    "grid_restored": "status.grid_ok",
    "battery_low": "status.battery",
    "battery_critical": "status.battery",
}


def _test_tcp_connect(host: str, port: int, timeout: float = 5.0) -> str:
    """Test TCP connection to host (synchronous)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return "OK"
    except Exception as e:
        return f"ERROR: {e}"


def _build_status_json(status: dict, subscriber_mgr=None, language: str = "en") -> dict:
    """Build a JSON dictionary from get_status for SSE."""
    d = status.get("last_data")
    grid = status.get("grid")
    generator = status.get("generator")
    last_data_time = status.get("last_data_time", 0)

    if not d:
        return {"no_data": True}

    now = time.time()
    age_s = int(now - last_data_time) if last_data_time > 0 else 0

    src_text = {
        "grid": translate_catalog("status.grid_ok", language=language),
        "low_voltage": translate_catalog("status.low_voltage", language=language),
        "no_grid": translate_catalog("status.no_grid", language=language),
    }
    src_color = {"grid": "#0f0", "low_voltage": "#ff0", "no_grid": "#f44"}
    ps = d.power_source

    soc = d.soc
    if soc >= 50:
        soc_color = "#0f0"
    elif soc >= 20:
        soc_color = "#ff0"
    else:
        soc_color = "#f44"

    if age_s < 180:
        age_color = "#0f0"
    elif age_s < 600:
        age_color = "#ff0"
    else:
        age_color = "#f44"

    event_text = ""
    event_color = ""
    if d.on_battery:
        if grid and grid.grid_lost_time > 0:
            event_text = format_time_human(grid.grid_lost_time, language=language)
            event_color = "#f44"
    else:
        if grid and grid.grid_restored_time > 0:
            event_text = format_time_human(grid.grid_restored_time, language=language)
            event_color = "#0f0"
        elif grid and grid.grid_lost_time > 0:
            event_text = format_time_human(grid.grid_lost_time, language=language)
            event_color = ""

    gen_text = ""
    gen_color = ""
    if generator and generator.status == "gen_on":
        gen_text = translate_catalog("status.running", language=language)
        if d.gen_voltage > 0:
            gen_text += f" ({d.gen_voltage:.0f}V, {d.gen_power}W)"
        gen_color = "#fa0"
    elif generator and generator.status == "gen_off":
        gen_text = translate_catalog("status.stopped", language=language)
        gen_color = "#666"

    bat_flow = ""
    if d.battery_charge > 0:
        bat_flow = f"↑ {d.battery_charge}W"
    elif d.battery_discharge > 0:
        bat_flow = f"↓ {d.battery_discharge}W"
    else:
        bat_flow = "0W"

    # Battery life estimate
    bat_estimate_text = ""
    if d.on_battery and grid and grid.grid_lost_time > 0:
        est = estimate_battery_runtime(
            soc,
            grid.grid_lost_time,
            generator_on=d.generator_on,
            pre_gen_soc=getattr(grid, "pre_gen_soc", 0),
            pre_gen_time=getattr(grid, "pre_gen_time", 0),
            language=language,
        )
        if est:
            bat_estimate_text = f"~{est['remaining_text']} ({est['depletion_time_text']})"

    return {
        "power_source": src_text.get(ps, ps),
        "power_source_color": src_color.get(ps, "#fff"),
        "power_source_state": ps,
        "grid_voltage": f"{d.grid_voltage:.0f}V / {d.grid_frequency:.1f}Hz",
        "soc": soc,
        "soc_color": soc_color,
        "battery_voltage": f"{d.battery_voltage:.1f}V",
        "house_power": d.house_power,
        "grid_import": d.grid_power_import,
        "generator": gen_text,
        "generator_color": gen_color,
        "event_text": event_text,
        "event_color": event_color,
        "event_label": (
            translate_catalog("status.no_grid", language=language)
            if d.on_battery
            else translate_catalog("status.grid_ok", language=language)
        ),
        "on_battery": d.on_battery,
        "bat_estimate": bat_estimate_text,
        "age_s": age_s,
        "age_color": age_color,
        "total_pv_power": d.total_pv_power,
        "bat_flow": bat_flow,
        "grid_export": d.grid_power_export,
        "poll_count": status.get("poll_count", 0),
        "error_count": status.get("error_count", 0),
        "subscribers": len((subscriber_mgr or sub_mgr).subscribers),
        "time": datetime.now().strftime("%H:%M:%S"),
    }


async def start_web_server(
    event_bus,
    state_mgr: StateManager,
    telegram_app,
    device_mgr=None,
    scenario_engine=None,
    consumption_monitor=None,
    subscriber_mgr=None,
    telegram_mode: str | None = None,
    settings_loader=None,
    settings_saver=None,
    tcp_tester=None,
    language: str | None = None,
    showcase_mode: bool = False,
    start_site: bool = True,
):
    """Build the local Web UI and optionally bind its TCP listener."""
    if not HAS_AIOHTTP:
        logger.warning("aiohttp is unavailable; the web interface is disabled")
        return

    csrf_token = secrets.token_urlsafe(32)
    active_subscriber_mgr = subscriber_mgr or sub_mgr
    active_telegram_mode = TELEGRAM_MODE if telegram_mode is None else telegram_mode
    active_tcp_tester = tcp_tester or _test_tcp_connect
    ui_language = language or current_language()

    def translate(key: str, language: str | None = None, **values) -> str:
        """Translate server-rendered and dynamic Web UI text consistently."""
        return translate_catalog(key, language=language or ui_language, **values)

    def active_settings_loader():
        return settings_loader() if settings_loader else load_settings()

    def active_settings_saver(values):
        if settings_saver:
            settings_saver(values)
        else:
            save_settings(values)

    # ──────────────────────────────────────────────
    # Status page (/)
    # ──────────────────────────────────────────────
    @aiohttp_jinja2.template("status.html")
    async def web_index(request: web.Request) -> dict:
        now = time.time()
        uptime_s = int(now - request.app["start_time"])
        uptime_h = uptime_s // 3600
        uptime_m = (uptime_s % 3600) // 60

        loop = asyncio.get_event_loop()
        tcp_result = await loop.run_in_executor(
            None, active_tcp_tester, INVERTER_HOST, INVERTER_PORT
        )

        data_age = translate("status.no_data")
        data_age_color = "#f44"
        poll_count = 0
        error_count = 0
        last_error = None
        now_time_str = datetime.now().strftime("%H:%M:%S")

        d = None
        inverter_info = False
        power_source_text = ""
        power_source_color = "#fff"
        grid_voltage_text = ""
        soc = 0
        soc_color = "#f44"
        battery_voltage_text = ""
        house_power = 0
        grid_import = 0
        gen_row_visible = False
        gen_text = ""
        gen_color = ""
        gen_bold = False
        # Network event (structured data instead of HTML)
        event_label = ""
        event_text = ""
        event_color = ""
        event_has_l_id = False
        # Battery rating
        estimate_text = ""
        total_pv_power = 0
        bat_flow = "0W"
        grid_export = 0
        ps = "grid"

        if event_bus:
            try:
                status = await event_bus.request("get_status")
                last_data_time = status.get("last_data_time", 0)
                d = status.get("last_data")
                grid = status.get("grid")
                generator = status.get("generator")
                poll_count = status.get("poll_count", 0)
                error_count = status.get("error_count", 0)
                last_error = status.get("last_error")

                if last_data_time > 0:
                    age_s = int(now - last_data_time)
                    data_age = translate("time.seconds_ago", count=age_s)
                    if age_s < 180:
                        data_age_color = "#0f0"
                    elif age_s < 600:
                        data_age_color = "#ff0"
                    else:
                        data_age_color = "#f44"

                if d:
                    inverter_info = True
                    src_text = {
                        "grid": translate("status.grid_ok"),
                        "low_voltage": translate("status.low_voltage"),
                        "no_grid": translate("status.no_grid"),
                    }
                    src_color = {"grid": "#0f0", "low_voltage": "#ff0", "no_grid": "#f44"}
                    ps = d.power_source

                    power_source_text = src_text.get(ps, ps)
                    power_source_color = src_color.get(ps, "#fff")
                    grid_voltage_text = f"{d.grid_voltage:.0f}V / {d.grid_frequency:.1f}Hz"
                    soc = d.soc
                    if soc >= 50:
                        soc_color = "#0f0"
                    elif soc >= 20:
                        soc_color = "#ff0"
                    else:
                        soc_color = "#f44"
                    battery_voltage_text = f"{d.battery_voltage:.1f}V"
                    house_power = d.house_power
                    grid_import = d.grid_power_import

                    if generator and generator.status == "gen_on":
                        gen_row_visible = True
                        gen_bold = True
                        gen_text = translate("status.running")
                        if d.gen_voltage > 0:
                            gen_text += f" ({d.gen_voltage:.0f}V, {d.gen_power}W)"
                        gen_color = "#fa0"
                    elif generator and generator.status == "gen_off":
                        gen_row_visible = True
                        gen_bold = False
                        gen_text = translate("status.stopped")
                        gen_color = "#666"

                    if d.on_battery:
                        if grid and grid.grid_lost_time > 0:
                            event_label = translate("status.no_grid")
                            event_text = format_time_human(
                                grid.grid_lost_time, language=ui_language
                            )
                            event_color = "#f44"
                            event_has_l_id = False
                    else:
                        if grid and grid.grid_restored_time > 0:
                            event_label = translate("status.grid_ok")
                            event_text = format_time_human(
                                grid.grid_restored_time, language=ui_language
                            )
                            event_color = "#0f0"
                            event_has_l_id = True
                        elif grid and grid.grid_lost_time > 0:
                            event_label = translate("status.last_update")
                            event_text = format_time_human(
                                grid.grid_lost_time, language=ui_language
                            )
                            event_color = ""
                            event_has_l_id = True

                    if d.on_battery and grid and grid.grid_lost_time > 0:
                        est = estimate_battery_runtime(
                            soc,
                            grid.grid_lost_time,
                            generator_on=d.generator_on,
                            pre_gen_soc=getattr(grid, "pre_gen_soc", 0),
                            pre_gen_time=getattr(grid, "pre_gen_time", 0),
                            language=ui_language,
                        )
                        if est:
                            estimate_text = (
                                f"~{est['remaining_text']} ({est['depletion_time_text']})"
                            )

                    bat_flow = (
                        f"↑ {d.battery_charge}W"
                        if d.battery_charge > 0
                        else f"↓ {d.battery_discharge}W"
                        if d.battery_discharge > 0
                        else "0W"
                    )
                    total_pv_power = d.total_pv_power
                    grid_export = d.grid_power_export
            except Exception:
                pass

        return {
            "title": translate("app.name"),
            "active_page": "/",
            "nav_links": NAV_LINKS,
            "inverter_info": inverter_info,
            "power_source_text": power_source_text,
            "power_source_color": power_source_color,
            "power_source_state": ps if d else "grid",
            "grid_voltage_text": grid_voltage_text,
            "soc": soc,
            "soc_color": soc_color,
            "battery_voltage_text": battery_voltage_text,
            "house_power": house_power,
            "grid_import": grid_import,
            "gen_row_visible": gen_row_visible,
            "gen_text": gen_text,
            "gen_color": gen_color,
            "gen_bold": gen_bold,
            "event_label": event_label,
            "event_text": event_text,
            "event_color": event_color,
            "event_has_l_id": event_has_l_id,
            "estimate_text": estimate_text,
            "total_pv_power": total_pv_power,
            "bat_flow": bat_flow,
            "grid_export": grid_export,
            "uptime_h": uptime_h,
            "uptime_m": uptime_m,
            "subscribers_count": len(active_subscriber_mgr.subscribers),
            "data_age": data_age,
            "data_age_color": data_age_color,
            "poll_count": poll_count,
            "error_count": error_count,
            "tcp_result": tcp_result,
            "last_error": last_error,
            "now_time_str": now_time_str,
        }

    # ──────────────────────────────────────────────
    # SSE: push status updates
    # ──────────────────────────────────────────────
    async def web_status_stream(request: web.Request) -> web.StreamResponse:
        """SSE - subscribes to data_updated, returns JSON with each update."""
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(request)

        queue: asyncio.Queue = asyncio.Queue()

        async def on_data_updated(event: Event) -> None:
            await queue.put(True)

        event_bus.subscribe("data_updated", on_data_updated)
        try:
            # Immediately send the current status
            try:
                status = await event_bus.request("get_status")
                payload = json.dumps(
                    _build_status_json(status, active_subscriber_mgr, ui_language),
                    ensure_ascii=False,
                )
                await _sse_send(resp, payload)
            except Exception:
                pass

            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    # Keep-alive
                    try:
                        await resp.write(b": keepalive\n\n")
                        await resp.drain()
                    except ConnectionResetError:
                        break
                    continue

                try:
                    status = await event_bus.request("get_status")
                    payload = json.dumps(
                        _build_status_json(status, active_subscriber_mgr, ui_language),
                        ensure_ascii=False,
                    )
                    await _sse_send(resp, payload)
                except ConnectionResetError:
                    break
                except Exception as e:
                    logger.debug("SSE status error: %s", e)
        finally:
            event_bus.unsubscribe("data_updated", on_data_updated)

        return resp

    # ──────────────────────────────────────────────
    # Admin
    # ──────────────────────────────────────────────
    @aiohttp_jinja2.template("admin.html")
    async def web_admin(request: web.Request) -> dict:
        """Admin panel."""
        msg = request.query.get("msg", "")

        grid_lost_time = state_mgr.grid.grid_lost_time if state_mgr else 0
        grid_restored_time = state_mgr.grid.grid_restored_time if state_mgr else 0

        pending_users = []
        for p in active_subscriber_mgr.pending:
            cid_p = p["chat_id"]
            display_name = active_subscriber_mgr.get_display_name(cid_p)
            pending_users.append(
                {
                    "chat_id": cid_p,
                    "display_name": display_name,
                    "username": active_subscriber_mgr.get_username(cid_p),
                    "initial": display_name[:1].upper() if display_name != "—" else "?",
                    "date": p.get("date", ""),
                }
            )

        active_users = []
        for cid in sorted(active_subscriber_mgr.subscribers):
            display_name = active_subscriber_mgr.get_display_name(cid)
            active_users.append(
                {
                    "chat_id": cid,
                    "display_name": display_name,
                    "username": active_subscriber_mgr.get_username(cid),
                    "initial": display_name[:1].upper() if display_name != "—" else "?",
                }
            )

        blocked_users = []
        for cid in sorted(active_subscriber_mgr.blocked):
            display_name = active_subscriber_mgr.get_display_name(cid)
            blocked_users.append(
                {
                    "chat_id": cid,
                    "display_name": display_name,
                    "username": active_subscriber_mgr.get_username(cid),
                    "initial": display_name[:1].upper() if display_name != "—" else "?",
                }
            )

        def _ts_parts(ts):
            """Split timestamp into dd, mm, yyyy, hh, mi strings."""
            if ts <= 0:
                return "", "", "", "", ""
            dt = datetime.fromtimestamp(ts)
            return (
                dt.strftime("%d"),
                dt.strftime("%m"),
                dt.strftime("%Y"),
                dt.strftime("%H"),
                dt.strftime("%M"),
            )

        gl = _ts_parts(grid_lost_time)
        gr = _ts_parts(grid_restored_time)

        return {
            "title": translate("access.title"),
            "active_page": "/admin",
            "nav_links": NAV_LINKS,
            "msg": msg,
            "pending_users": pending_users,
            "active_users": active_users,
            "blocked_users": blocked_users,
            "pending_count": len(pending_users),
            "active_count": len(active_users),
            "blocked_count": len(blocked_users),
            "telegram_disabled": active_telegram_mode == "disabled",
            "gl_dd": gl[0],
            "gl_mm": gl[1],
            "gl_yyyy": gl[2],
            "gl_hh": gl[3],
            "gl_mi": gl[4],
            "gr_dd": gr[0],
            "gr_mm": gr[1],
            "gr_yyyy": gr[2],
            "gr_hh": gr[3],
            "gr_mi": gr[4],
        }

    async def web_admin_action(request: web.Request) -> web.Response:
        """Handle Telegram access-management actions."""
        data = await request.post()
        action = data.get("action", "")
        _telegram_app = request.app["telegram_app"]
        _main_keyboard = request.app["main_keyboard"]

        if action == "update_times":

            def _parse_fields(dd, mm, yyyy, hh, mi) -> float:
                dd, mm, yyyy = dd.strip(), mm.strip(), yyyy.strip()
                hh, mi = hh.strip(), mi.strip()
                if not dd or not mm or not yyyy:
                    return 0
                if not hh:
                    hh = "00"
                if not mi:
                    mi = "00"
                try:
                    combined = f"{dd}.{mm}.{yyyy} {hh}:{mi}:00"
                    return datetime.strptime(combined, "%d.%m.%Y %H:%M:%S").timestamp()
                except ValueError:
                    return 0

            if state_mgr:
                state_mgr.grid.grid_lost_time = _parse_fields(
                    data.get("gl_dd", ""),
                    data.get("gl_mm", ""),
                    data.get("gl_yyyy", ""),
                    data.get("gl_hh", ""),
                    data.get("gl_mi", ""),
                )
                state_mgr.grid.grid_restored_time = _parse_fields(
                    data.get("gr_dd", ""),
                    data.get("gr_mm", ""),
                    data.get("gr_yyyy", ""),
                    data.get("gr_hh", ""),
                    data.get("gr_mi", ""),
                )
                state_mgr.save_state()
                logger.info(
                    "The admin updated the time: lost=%.0f restored=%.0f",
                    state_mgr.grid.grid_lost_time,
                    state_mgr.grid.grid_restored_time,
                )
            raise web.HTTPFound("/admin?msg=" + translate("web.time_updated", language=ui_language))

        try:
            chat_id = int(data.get("chat_id", 0))
        except (ValueError, TypeError):
            raise web.HTTPFound(
                "/admin?msg=" + translate("web.invalid_chat_id", language=ui_language)
            )

        if action == "approve":
            active_subscriber_mgr.pending = [
                p for p in active_subscriber_mgr.pending if p["chat_id"] != chat_id
            ]
            active_subscriber_mgr.save_pending()
            active_subscriber_mgr.subscribers.add(chat_id)
            active_subscriber_mgr.save_subscribers()
            try:
                if _telegram_app is not None:
                    telegram_language = str(active_settings_loader().get("language", ui_language))
                    await _telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=translate("telegram.approved", language=telegram_language),
                        reply_markup=(
                            _main_keyboard() if callable(_main_keyboard) else _main_keyboard
                        ),
                    )
            except Exception as e:
                logger.error("Approval notification failed: %s", type(e).__name__)
            logger.info("Administrator approved a subscriber")
            raise web.HTTPFound(
                "/admin?msg=" + translate("web.user_approved", language=ui_language)
            )

        elif action == "block":
            active_subscriber_mgr.pending = [
                p for p in active_subscriber_mgr.pending if p["chat_id"] != chat_id
            ]
            active_subscriber_mgr.save_pending()
            active_subscriber_mgr.subscribers.discard(chat_id)
            active_subscriber_mgr.save_subscribers()
            active_subscriber_mgr.blocked.add(chat_id)
            active_subscriber_mgr.save_blocked()
            logger.info("Administrator blocked a subscriber")
            raise web.HTTPFound("/admin?msg=" + translate("web.user_blocked", language=ui_language))

        elif action == "unblock":
            active_subscriber_mgr.blocked.discard(chat_id)
            active_subscriber_mgr.save_blocked()
            logger.info("Administrator unblocked a subscriber")
            raise web.HTTPFound(
                "/admin?msg=" + translate("web.user_unblocked", language=ui_language)
            )

        raise web.HTTPFound("/admin")

    # ──────────────────────────────────────────────
    # Devices
    # ──────────────────────────────────────────────
    @aiohttp_jinja2.template("devices.html")
    async def web_devices(request: web.Request) -> dict:
        """Devices page: show loading, JS will request statuses."""
        msg = request.query.get("msg", "")
        _device_mgr = request.app["device_mgr"]
        _scenario_engine = request.app["scenario_engine"]
        _consumption_monitor = request.app.get("consumption_monitor")

        devices_list = []
        if _device_mgr:
            devices = _device_mgr.list_devices()
            for dev in devices:
                cfg = _device_mgr.get_device(dev["id"])
                monitor_consumption = bool(cfg and cfg.config.get("monitor_consumption"))
                last_power = None
                if monitor_consumption and _consumption_monitor:
                    last_power = _consumption_monitor.get_last_power(dev["id"])
                dev_info = {
                    "id": dev["id"],
                    "name": dev["name"],
                    "provider": dev.get("provider", ""),
                    "enabled": dev["enabled"],
                    "test_mode": dev.get("test_mode", False),
                    "online": None,
                    "on": None,
                    "scenarios": [],
                    "has_scenarios": False,
                    "monitor_consumption": monitor_consumption,
                    "current_power": last_power,
                }
                # Defining the Scenario Type
                scenario_type = "none"
                scenario_timer_hours = 5
                if dev["enabled"] and _scenario_engine:
                    sc_list = _scenario_engine.get_scenarios_for_device(dev["id"])
                    for sc in sc_list:
                        trigger_key = _TRIGGER_LABELS.get(sc["trigger_event"])
                        trigger_label = (
                            translate(trigger_key) if trigger_key else sc["trigger_event"]
                        )
                        timer_str = ""
                        remaining = sc.get("timer_remaining")
                        if remaining is not None and remaining > 0:
                            hours = remaining // 3600
                            mins = (remaining % 3600) // 60
                            if hours > 0:
                                timer_str = f"({hours}h{mins}min)"
                            else:
                                timer_str = f"({mins}min)"
                        dev_info["scenarios"].append(
                            {
                                "id": sc["id"],
                                "name": sc["name"],
                                "enabled": sc["enabled"],
                                "trigger_label": trigger_label,
                                "timer_str": timer_str,
                                "timer_remaining": remaining if remaining and remaining > 0 else 0,
                            }
                        )
                    dev_info["has_scenarios"] = len(dev_info["scenarios"]) > 0
                    # Derive the UI scenario type from the configured rule.
                    if dev_info["has_scenarios"]:
                        # Find the on-battery rule for this device.
                        off_rule_id = f"{dev['id']}_off_battery"
                        rule = _scenario_engine.get_rule(off_rule_id)
                        if rule:
                            if rule.revert_after_seconds > 0:
                                scenario_type = "timer"
                                scenario_timer_hours = rule.revert_after_seconds // 3600 or 1
                            else:
                                scenario_type = "onoff"
                dev_info["scenario_type"] = scenario_type
                dev_info["scenario_timer_hours"] = scenario_timer_hours
                devices_list.append(dev_info)

        return {
            "title": translate("devices.title"),
            "active_page": "/devices",
            "nav_links": NAV_LINKS,
            "msg": msg,
            "devices": devices_list,
            "tuya_protocol_versions": SUPPORTED_TUYA_PROTOCOL_VERSIONS,
        }

    async def web_devices_states(request: web.Request) -> web.Response:
        """JSON API: ping + get on/off for all devices. Called from JS."""
        _device_mgr = request.app["device_mgr"]
        _consumption_monitor = request.app.get("consumption_monitor")
        if not _device_mgr:
            return web.json_response({"devices": []})

        result_queue: asyncio.Queue = asyncio.Queue()

        async def fetch_one(device_id: str):
            cfg = _device_mgr.get_device(device_id)
            current_power = None
            if cfg and cfg.config.get("monitor_consumption") and _consumption_monitor:
                current_power = _consumption_monitor.get_last_power(device_id)
            try:
                state = await _device_mgr.get_device_state(device_id)
                if state:
                    await result_queue.put(
                        {
                            "id": device_id,
                            "online": state.online,
                            "on": state.on,
                            "current_power": current_power,
                        }
                    )
                else:
                    await result_queue.put(
                        {
                            "id": device_id,
                            "online": False,
                            "on": None,
                            "current_power": current_power,
                        }
                    )
            except Exception as e:
                logger.error("[API states] %s: %s", device_id, type(e).__name__)
                await result_queue.put(
                    {
                        "id": device_id,
                        "online": False,
                        "on": None,
                        "current_power": current_power,
                    }
                )

        devices = _device_mgr.list_devices()
        enabled = [d for d in devices if d["enabled"]]
        logger.info("[API states] request for %d devices", len(enabled))

        tasks = [asyncio.create_task(fetch_one(d["id"])) for d in enabled]

        collected = 0
        items = []
        while collected < len(tasks):
            try:
                item = await asyncio.wait_for(result_queue.get(), timeout=15)
                items.append(item)
                collected += 1
            except asyncio.TimeoutError:
                logger.warning("[API states] timeout, received %d/%d", collected, len(tasks))
                break

        for t in tasks:
            if not t.done():
                t.cancel()

        logger.debug("[API states] returned %d devices", len(items))
        return web.json_response({"devices": items})

    async def web_devices_action(request: web.Request) -> web.Response:
        """Manual command to the device."""
        data = await request.post()
        device_id = data.get("device_id", "")
        action = data.get("action", "")
        _device_mgr = request.app["device_mgr"]
        _scenario_engine = request.app["scenario_engine"]

        if not device_id or not action:
            raise web.HTTPFound("/devices?msg=" + translate("web.invalid_parameters"))

        if not _device_mgr:
            raise web.HTTPFound("/devices?msg=" + translate("web.device_manager_unavailable"))

        cfg = _device_mgr.get_device(device_id)
        if not cfg:
            raise web.HTTPFound(
                "/devices?msg=" + translate("web.device_not_found", device_id=device_id)
            )

        if action in ("activate", "deactivate"):
            enabled = action == "activate"
            _device_mgr.set_device_enabled(device_id, enabled)
            sc_count = 0
            if _scenario_engine:
                sc_count = _scenario_engine.set_enabled_for_device(device_id, enabled)
            word = "activated" if enabled else "deactivated"
            msg = f"{cfg.name}: {word}"
            if sc_count > 0:
                msg += f" (+{sc_count}scenarios)"
            raise web.HTTPFound(f"/devices?msg={msg}")

        params = {}
        if action == "set_level":
            try:
                params["level"] = int(data.get("level", 100))
            except (ValueError, TypeError):
                params["level"] = 100

        async def _run():
            ok = await _device_mgr.execute_command(device_id, action, params, source="web")
            action_name = {
                "turn_on": translate("web.action_turn_on"),
                "turn_off": translate("web.action_turn_off"),
                "set_level": translate("web.action_set_level"),
            }.get(action, action)
            if ok:
                logger.info("Web: %s %s - success", action_name, cfg.name)
            else:
                logger.warning("Web: %s %s - failed", action_name, cfg.name)

        asyncio.create_task(_run())

        is_fetch = request.headers.get("Accept", "").find("text/html") == -1
        if is_fetch:
            return web.json_response({"ok": True})

        action_name = {
            "turn_on": translate("web.action_turn_on"),
            "turn_off": translate("web.action_turn_off"),
            "set_level": translate("web.action_set_level"),
        }.get(action, action)
        raise web.HTTPFound(
            "/devices?msg="
            + translate("web.action_in_progress", action=action_name, device=cfg.name)
        )

    # ──────────────────────────────────────────────
    # Scripts (combined into /devices)
    # ──────────────────────────────────────────────
    async def web_scenarios(request: web.Request) -> web.Response:
        """Redirect /scenarios → /devices."""
        raise web.HTTPFound("/devices")

    async def web_scenario_action(request: web.Request) -> web.Response:
        """Enable/disable scenario (POST /devices/scenario)."""
        data = await request.post()
        scenario_id = data.get("scenario_id", "")
        action = data.get("action", "")
        _scenario_engine = request.app["scenario_engine"]

        if not _scenario_engine:
            raise web.HTTPFound("/devices?msg=" + translate("web.scenario_engine_unavailable"))

        if action == "enable":
            _scenario_engine.set_enabled(scenario_id, True)
            raise web.HTTPFound("/devices?msg=" + translate("web.scenario_enabled"))
        elif action == "disable":
            _scenario_engine.set_enabled(scenario_id, False)
            raise web.HTTPFound("/devices?msg=" + translate("web.scenario_disabled"))

        raise web.HTTPFound("/devices")

    async def web_devices_monitor_consumption(request: web.Request) -> web.Response:
        """Enable/disable consumption monitoring for the device."""
        data = await request.post()
        device_id = data.get("device_id", "")
        enabled = data.get("enabled", "") in ("1", "true", "on")
        _device_mgr = request.app["device_mgr"]
        if not _device_mgr:
            return web.json_response(
                {"ok": False, "error": translate("web.device_manager_unavailable")}
            )
        cfg = _device_mgr.get_device(device_id)
        if not cfg:
            return web.json_response(
                {"ok": False, "error": translate("web.device_not_found", device_id=device_id)}
            )
        if cfg.provider != "tapo":
            return web.json_response({"ok": False, "error": translate("web.tapo_only")})
        ok = _device_mgr.set_monitor_consumption(device_id, enabled)
        return web.json_response({"ok": ok, "enabled": enabled})

    async def web_devices_rename(request: web.Request) -> web.Response:
        """Rename the device. If new_name is passed - manually, otherwise - from Tuya Cloud."""
        data = await request.post()
        device_id = data.get("device_id", "")
        new_name = (data.get("new_name", "") or "").strip()
        _device_mgr = request.app["device_mgr"]

        if not _device_mgr:
            return web.json_response(
                {"ok": False, "error": translate("web.device_manager_unavailable")}
            )

        cfg = _device_mgr.get_device(device_id)
        if not cfg:
            return web.json_response(
                {"ok": False, "error": translate("web.device_not_found", device_id=device_id)}
            )

        # Manual rename
        if new_name:
            _device_mgr.rename_device(device_id, new_name)
            logger.info("Device '%s' renamed to '%s'", device_id, new_name)
            return web.json_response({"ok": True, "name": new_name})

        # Fallback: query Tuya Cloud
        tuya_id = cfg.config.get("device_id", "")
        if not tuya_id:
            return web.json_response({"ok": False, "error": translate("web.no_device_id")})

        if not TUYA_ACCESS_ID or not TUYA_ACCESS_SECRET:
            return web.json_response(
                {"ok": False, "error": translate("web.tuya_credentials_required")}
            )

        try:
            import tinytuya

            def _fetch_name():
                c = tinytuya.Cloud(
                    apiRegion=TUYA_REGION,
                    apiKey=TUYA_ACCESS_ID,
                    apiSecret=TUYA_ACCESS_SECRET,
                )
                result = c.getdevices()
                for dev in result:
                    if dev.get("id") == tuya_id:
                        return dev.get("name", "")
                return None

            name = await asyncio.to_thread(_fetch_name)
        except Exception as e:
            logger.error("Tuya Cloud API failed: %s", type(e).__name__)
            return web.json_response({"ok": False, "error": translate("web.add_failed")})

        if name is None:
            return web.json_response({"ok": False, "error": translate("web.tuya_device_not_found")})

        _device_mgr.rename_device(device_id, name)
        logger.info("Device '%s' renamed to '%s' (from Tuya Cloud)", device_id, name)
        return web.json_response({"ok": True, "name": name})

    async def web_devices_change_scenario_type(request: web.Request) -> web.Response:
        """Changing the device scenario type (POST /devices/scenario-type)."""
        data = await request.post()
        device_id = (data.get("device_id", "") or "").strip()
        new_type = data.get("scenario_type", "none")
        timer_hours = 5
        if new_type == "timer":
            try:
                timer_hours = int(data.get("timer_hours", 5))
                timer_hours = max(1, min(24, timer_hours))
            except (ValueError, TypeError):
                timer_hours = 5

        _device_mgr = request.app["device_mgr"]
        _scenario_engine = request.app["scenario_engine"]

        if not _device_mgr:
            return web.json_response(
                {"ok": False, "error": translate("web.device_manager_unavailable")}
            )
        if not _scenario_engine:
            return web.json_response(
                {"ok": False, "error": translate("web.scenario_engine_unavailable")}
            )

        cfg = _device_mgr.get_device(device_id)
        if not cfg:
            return web.json_response(
                {"ok": False, "error": translate("web.device_not_found", device_id=device_id)}
            )

        device_name = cfg.name

        # Removing old scripts (cancels timers without revert)
        removed = _scenario_engine.remove_scenarios_for_device(device_id)

        # Create replacement scenarios.
        added = 0
        if new_type == "onoff":
            scenarios = [
                {
                    "id": f"{device_id}_off_battery",
                    "name": translate("web.scenario_off_battery", device=device_name),
                    "trigger_event": "on_battery",
                    "actions": [{"device_id": device_id, "action": "turn_off"}],
                    "priority": 10,
                    "enabled": True,
                },
                {
                    "id": f"{device_id}_on_grid",
                    "name": translate("web.scenario_on_grid", device=device_name),
                    "trigger_event": "off_battery",
                    "actions": [{"device_id": device_id, "action": "turn_on"}],
                    "priority": 10,
                    "enabled": True,
                },
            ]
            added = _scenario_engine.add_scenarios(scenarios)
        elif new_type == "timer":
            n = timer_hours * 3600
            scenarios = [
                {
                    "id": f"{device_id}_off_battery",
                    "name": translate(
                        "web.scenario_off_battery_timer", device=device_name, hours=timer_hours
                    ),
                    "trigger_event": "on_battery",
                    "actions": [{"device_id": device_id, "action": "turn_off"}],
                    "revert_after_seconds": n,
                    "revert_actions": [{"device_id": device_id, "action": "turn_on"}],
                    "cancel_event": "off_battery",
                    "priority": 10,
                    "enabled": True,
                },
                {
                    "id": f"{device_id}_on_grid",
                    "name": translate("web.scenario_on_grid", device=device_name),
                    "trigger_event": "off_battery",
                    "actions": [{"device_id": device_id, "action": "turn_on"}],
                    "priority": 5,
                    "enabled": True,
                },
            ]
            added = _scenario_engine.add_scenarios(scenarios)

        # Edge case: currently on battery
        if new_type != "none" and state_mgr and state_mgr.battery.on_battery:

            async def _apply_on_battery():
                await _device_mgr.execute_command(device_id, "turn_off", {}, source="scenario")
                if new_type == "timer" and state_mgr.grid.grid_lost_time > 0:
                    elapsed = time.time() - state_mgr.grid.grid_lost_time
                    remaining = int(timer_hours * 3600 - elapsed)
                    if remaining > 0:
                        _scenario_engine.start_timer_with_remaining(
                            f"{device_id}_off_battery", remaining
                        )
                    else:
                        await _device_mgr.execute_command(
                            device_id, "turn_on", {}, source="scenario"
                        )

            asyncio.create_task(_apply_on_battery())

        logger.info(
            "Changing script type '%s' → %s (removed: %d, added: %d)",
            device_name,
            new_type,
            removed,
            added,
        )
        return web.json_response({"ok": True, "removed": removed, "added": added})

    async def web_devices_add(request: web.Request) -> web.Response:
        """Add a device (Tuya or Tapo) via POST /devices/add."""
        data = await request.post()
        provider = (data.get("provider", "") or "tuya").strip()
        ip = (data.get("ip", "") or "").strip()
        tuya_device_id = (data.get("tuya_device_id", "") or "").strip()
        tuya_protocol_version = (data.get("tuya_protocol_version", "3.3") or "3.3").strip()
        tapo_name = (data.get("tapo_name", "") or "").strip()
        scenario_type = data.get("scenario_type", "none")
        timer_hours = 5

        if scenario_type == "timer":
            try:
                timer_hours = int(data.get("timer_hours", 5))
                timer_hours = max(1, min(24, timer_hours))
            except (ValueError, TypeError):
                timer_hours = 5

        _device_mgr = request.app["device_mgr"]
        _scenario_engine = request.app["scenario_engine"]

        if not _device_mgr:
            return web.json_response(
                {"ok": False, "error": translate("web.device_manager_unavailable")}
            )

        if provider == "tuya" and tuya_protocol_version not in SUPPORTED_TUYA_PROTOCOL_VERSIONS:
            return web.json_response(
                {"ok": False, "error": translate("web.invalid_tuya_protocol_version")}
            )

        if request.app.get("showcase_mode") and hasattr(_device_mgr, "add_showcase_device"):
            name = tapo_name or translate("showcase.default_tuya_name")
            result = _device_mgr.add_showcase_device(
                provider=provider,
                name=name,
                host=ip,
                external_id=tuya_device_id,
            )
            if not result:
                return web.json_response({"ok": False, "error": translate("web.add_failed")})
            return web.json_response({"ok": True, "name": name, "scenarios": 0})

        if provider == "tapo":
            # ── Tapo P115 ──
            if not ip:
                return web.json_response({"ok": False, "error": translate("web.ip_required")})
            if not tapo_name:
                return web.json_response({"ok": False, "error": translate("web.name_required")})
            if not TAPO_USERNAME or not TAPO_PASSWORD:
                return web.json_response(
                    {"ok": False, "error": translate("web.tapo_credentials_required")}
                )

            # Reject another device configured with the same host address.
            for cfg in _device_mgr.devices.values():
                if cfg.host == ip:
                    return web.json_response(
                        {"ok": False, "error": translate("web.duplicate_ip", ip=ip)}
                    )

            device_id = "tapo_" + ip.replace(".", "_")
            name = tapo_name

            if device_id in _device_mgr.devices:
                return web.json_response(
                    {"ok": False, "error": translate("web.duplicate_device", device_id=device_id)}
                )

            ok = _device_mgr.add_device(
                device_id=device_id,
                name=name,
                provider="tapo",
                host=ip,
                config={
                    "username": TAPO_USERNAME,
                    "password": TAPO_PASSWORD,
                },
            )
        else:
            # ── Tuya ──
            if not ip or not tuya_device_id:
                return web.json_response(
                    {"ok": False, "error": translate("web.ip_and_device_id_required")}
                )

            # Check for a duplicate by config.device_id.
            for cfg in _device_mgr.devices.values():
                if cfg.config.get("device_id") == tuya_device_id:
                    return web.json_response(
                        {"ok": False, "error": translate("web.duplicate_tuya_id")}
                    )

            if not TUYA_ACCESS_ID or not TUYA_ACCESS_SECRET:
                return web.json_response(
                    {"ok": False, "error": translate("web.tuya_credentials_required")}
                )

            # Request the device metadata from Tuya Cloud.
            try:
                import tinytuya

                def _fetch_device():
                    c = tinytuya.Cloud(
                        apiRegion=TUYA_REGION,
                        apiKey=TUYA_ACCESS_ID,
                        apiSecret=TUYA_ACCESS_SECRET,
                    )
                    result = c.getdevices()
                    for dev in result:
                        if dev.get("id") == tuya_device_id:
                            return dev
                    return None

                cloud_dev = await asyncio.to_thread(_fetch_device)
            except Exception as e:
                logger.error("Tuya Cloud API failed: %s", type(e).__name__)
                return web.json_response({"ok": False, "error": translate("web.add_failed")})

            if cloud_dev is None:
                return web.json_response(
                    {"ok": False, "error": translate("web.tuya_device_not_found")}
                )

            name = cloud_dev.get("name") or tuya_device_id
            local_key = cloud_dev.get("key", "")
            if not local_key:
                return web.json_response(
                    {"ok": False, "error": translate("web.tuya_local_key_missing")}
                )
            device_id = "dev_" + tuya_device_id[:8]

            if device_id in _device_mgr.devices:
                return web.json_response(
                    {"ok": False, "error": translate("web.duplicate_device", device_id=device_id)}
                )

            ok = _device_mgr.add_device(
                device_id=device_id,
                name=name,
                provider="tuya",
                host=ip,
                config={
                    "device_id": tuya_device_id,
                    "local_key": local_key,
                    "version": float(tuya_protocol_version),
                },
            )

        if not ok:
            return web.json_response({"ok": False, "error": translate("web.add_failed")})

        # Scripts - common logic for both providers
        scenarios_added = 0
        if scenario_type != "none" and _scenario_engine:
            if scenario_type == "onoff":
                scenarios = [
                    {
                        "id": f"{device_id}_off_battery",
                        "name": translate("web.scenario_off_battery", device=name),
                        "trigger_event": "on_battery",
                        "actions": [{"device_id": device_id, "action": "turn_off"}],
                        "priority": 10,
                        "enabled": True,
                    },
                    {
                        "id": f"{device_id}_on_grid",
                        "name": translate("web.scenario_on_grid", device=name),
                        "trigger_event": "off_battery",
                        "actions": [{"device_id": device_id, "action": "turn_on"}],
                        "priority": 10,
                        "enabled": True,
                    },
                ]
            elif scenario_type == "timer":
                n = timer_hours * 3600
                scenarios = [
                    {
                        "id": f"{device_id}_off_battery",
                        "name": translate(
                            "web.scenario_off_battery_timer", device=name, hours=timer_hours
                        ),
                        "trigger_event": "on_battery",
                        "actions": [{"device_id": device_id, "action": "turn_off"}],
                        "revert_after_seconds": n,
                        "revert_actions": [{"device_id": device_id, "action": "turn_on"}],
                        "cancel_event": "off_battery",
                        "priority": 10,
                        "enabled": True,
                    },
                    {
                        "id": f"{device_id}_on_grid",
                        "name": translate("web.scenario_on_grid", device=name),
                        "trigger_event": "off_battery",
                        "actions": [{"device_id": device_id, "action": "turn_on"}],
                        "priority": 5,
                        "enabled": True,
                    },
                ]
            else:
                scenarios = []

            if scenarios:
                scenarios_added = _scenario_engine.add_scenarios(scenarios)

        logger.info("Device '%s' (%s) added via UI (scripts: %d)", name, provider, scenarios_added)
        return web.json_response({"ok": True, "name": name, "scenarios": scenarios_added})

    async def web_devices_reload(request: web.Request) -> web.Response:
        """Reread devices.json and scenarios.json without rebooting."""
        _device_mgr = request.app["device_mgr"]
        _scenario_engine = request.app["scenario_engine"]

        result = {}
        if _device_mgr:
            result["devices"] = _device_mgr.reload_from_file()
        else:
            result["devices"] = {"added": [], "removed": [], "updated": []}

        if _scenario_engine:
            result["scenarios_count"] = _scenario_engine.reload_from_file()
        else:
            result["scenarios_count"] = 0

        result["ok"] = True
        logger.info(
            "Reload config: devices=%s, scenarios=%d",
            result["devices"],
            result.get("scenarios_count", 0),
        )
        return web.json_response(result)

    async def web_scenarios_action(request: web.Request) -> web.Response:
        """Scenario on/off (backward compat POST /scenarios)."""
        data = await request.post()
        scenario_id = data.get("scenario_id", "")
        action = data.get("action", "")
        _scenario_engine = request.app["scenario_engine"]

        if not _scenario_engine:
            raise web.HTTPFound("/devices?msg=" + translate("web.scenario_engine_unavailable"))

        if action == "enable":
            _scenario_engine.set_enabled(scenario_id, True)
            raise web.HTTPFound("/devices?msg=" + translate("web.scenario_enabled"))
        elif action == "disable":
            _scenario_engine.set_enabled(scenario_id, False)
            raise web.HTTPFound("/devices?msg=" + translate("web.scenario_disabled"))

        raise web.HTTPFound("/devices")

    # ──────────────────────────────────────────────
    # Device log
    # ──────────────────────────────────────────────
    @aiohttp_jinja2.template("device_log.html")
    async def web_device_log(request: web.Request) -> dict:
        """Device event log page."""
        _device_mgr = request.app.get("device_mgr")
        events = _device_mgr.load_events() if _device_mgr else DeviceManager.load_events()
        events.reverse()

        _source_labels = {
            "web": translate("web.source_web", language=ui_language),
            "scenario": translate("web.source_scenario", language=ui_language),
            "detected": translate("web.source_detected", language=ui_language),
        }
        _action_labels = {
            "turn_on": translate("web.action_turn_on", language=ui_language),
            "turn_off": translate("web.action_turn_off", language=ui_language),
            "set_level": translate("web.action_set_level", language=ui_language),
        }

        events_list = []
        for ev in events:
            action_label = _action_labels.get(ev.get("action", ""), ev.get("action", ""))
            source_label = _source_labels.get(ev.get("source", ""), ev.get("source", "") or "—")

            events_list.append(
                {
                    "time": ev.get("time", ""),
                    "device_name": ev.get("device_name", ev.get("device_id", "")),
                    "action_label": action_label,
                    "source_label": source_label,
                    "details": ev.get("details", ""),
                    "ok": ev.get("ok", True),
                }
            )

        return {
            "title": translate("nav.device_log"),
            "active_page": "/device-log",
            "nav_links": NAV_LINKS,
            "events": events_list,
            "event_count": len(events_list),
        }

    async def web_device_log_stream(request: web.Request) -> web.StreamResponse:
        """SSE - new real-time device log entries."""
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(request)

        queue: asyncio.Queue = asyncio.Queue()

        async def on_cmd_ok(event: Event) -> None:
            await queue.put((True, event.data))

        async def on_cmd_failed(event: Event) -> None:
            await queue.put((False, event.data))

        event_bus.subscribe("device_command_ok", on_cmd_ok)
        event_bus.subscribe("device_command_failed", on_cmd_failed)
        try:
            while True:
                try:
                    ok, data = await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    try:
                        await resp.write(b": keepalive\n\n")
                    except ConnectionResetError:
                        break
                    continue

                try:
                    now = datetime.now()
                    payload = json.dumps(
                        {
                            "time": now.strftime("%d.%m %H:%M:%S"),
                            "device_name": data.get("device_name", data.get("device_id", "")),
                            "action": data.get("action", ""),
                            "source": data.get("source", ""),
                            "source_detail": data.get("source_detail", ""),
                            "ok": ok,
                        },
                        ensure_ascii=False,
                    )
                    await _sse_send(resp, payload)
                except ConnectionResetError:
                    break
                except Exception as e:
                    logger.debug("SSE device-log error: %s", e)
        finally:
            event_bus.unsubscribe("device_command_ok", on_cmd_ok)
            event_bus.unsubscribe("device_command_failed", on_cmd_failed)

        return resp

    async def web_admin_stream(request: web.Request) -> web.StreamResponse:
        """SSE - new real-time subscription requests."""
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(request)

        queue: asyncio.Queue = asyncio.Queue()

        async def on_pending_added(event: Event) -> None:
            await queue.put(event.data)

        event_bus.subscribe("pending_user_added", on_pending_added)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=60)
                except asyncio.TimeoutError:
                    try:
                        await resp.write(b": keepalive\n\n")
                    except ConnectionResetError:
                        break
                    continue

                try:
                    payload = json.dumps(
                        {
                            "chat_id": data.get("chat_id", 0),
                            "first_name": data.get("first_name", ""),
                            "username": data.get("username", ""),
                            "date": data.get("date", ""),
                        },
                        ensure_ascii=False,
                    )
                    await _sse_send(resp, payload)
                except ConnectionResetError:
                    break
                except Exception as e:
                    logger.debug("SSE admin error: %s", e)
        finally:
            event_bus.unsubscribe("pending_user_added", on_pending_added)

        return resp

    @aiohttp_jinja2.template("settings.html")
    async def web_settings(request: web.Request) -> dict:
        """Render configuration without returning any stored secret value."""
        stored_settings = active_settings_loader()
        public_settings = {
            "language": stored_settings.get("language", "en"),
            "timezone": stored_settings.get("timezone", "UTC"),
            "tuya_region": stored_settings.get("tuya_region", "eu"),
        }
        return {
            "title": translate("settings.title"),
            "active_page": "/settings",
            "nav_links": NAV_LINKS,
            "msg": request.query.get("msg", ""),
            "settings": public_settings,
            "languages": SUPPORTED_LANGUAGES,
            "timezones": IANA_TIMEZONES,
            "tuya_regions": SUPPORTED_TUYA_CLOUD_REGIONS,
            "credential_status": {
                "tapo": (
                    translate("settings.configured")
                    if stored_settings.get("tapo_username") and stored_settings.get("tapo_password")
                    else translate("settings.not_configured")
                ),
                "tuya": (
                    translate("settings.configured")
                    if stored_settings.get("tuya_access_id")
                    and stored_settings.get("tuya_access_secret")
                    else translate("settings.not_configured")
                ),
            },
        }

    async def web_settings_action(request: web.Request) -> web.Response:
        """Replace encrypted settings without ever rendering existing secrets."""
        data = await request.post()
        action = str(data.get("action", ""))
        settings = active_settings_loader()
        if action == "general":
            language = str(data.get("language", ""))
            timezone = str(data.get("timezone", "")).strip()
            if language not in SUPPORTED_LANGUAGES:
                raise web.HTTPBadRequest(text=translate("web.settings_invalid_language"))
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise web.HTTPBadRequest(text=translate("web.settings_invalid_timezone"))
            settings["language"] = language
            settings["timezone"] = timezone
        elif action == "tapo":
            username = str(data.get("tapo_username", "")).strip()
            password = str(data.get("tapo_password", ""))
            if not username or not password:
                raise web.HTTPBadRequest(text=translate("web.settings_tapo_required"))
            settings["tapo_username"] = username
            settings["tapo_password"] = password
        elif action == "tuya":
            access_id = str(data.get("tuya_access_id", "")).strip()
            access_secret = str(data.get("tuya_access_secret", ""))
            region = str(data.get("tuya_region", "eu"))
            if not access_id or not access_secret or region not in SUPPORTED_TUYA_CLOUD_REGIONS:
                raise web.HTTPBadRequest(text=translate("web.settings_tuya_required"))
            settings["tuya_access_id"] = access_id
            settings["tuya_access_secret"] = access_secret
            settings["tuya_region"] = region
        else:
            raise web.HTTPBadRequest(text=translate("web.settings_unknown_action"))
        active_settings_saver(settings)
        if action == "general" and telegram_app is not None:
            from inverterscout.interfaces.telegram import refresh_telegram_commands

            await refresh_telegram_commands(telegram_app, language=settings["language"])
        raise web.HTTPFound("/settings?msg=" + translate("settings.saved"))

    @web.middleware
    async def security_middleware(request: web.Request, handler):
        if request.method == "POST":
            form = await request.post()
            supplied_token = str(form.get("csrf_token", ""))
            if not secrets.compare_digest(supplied_token, csrf_token):
                raise web.HTTPForbidden(text="Invalid CSRF token")
        response = await handler(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app = web.Application(middlewares=[security_middleware])
    app["start_time"] = time.time()
    app["telegram_app"] = telegram_app
    app["main_keyboard"] = getattr(telegram_app, "_main_keyboard", None)
    app["device_mgr"] = device_mgr
    app["scenario_engine"] = scenario_engine
    app["consumption_monitor"] = consumption_monitor
    app["subscriber_mgr"] = active_subscriber_mgr
    app["showcase_mode"] = showcase_mode

    # Import lazily to avoid loading Telegram components before they are needed.
    try:
        from inverterscout.interfaces.telegram import main_keyboard

        app["main_keyboard"] = lambda: main_keyboard(
            language=str(active_settings_loader().get("language", ui_language))
        )
    except ImportError:
        pass

    # Setting up Jinja2 templates
    template_dir = Path(__file__).parents[1] / "resources" / "templates"
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(template_dir))
    env = aiohttp_jinja2.get_env(app)
    env.globals["app_version"] = APP_VERSION
    env.globals["t"] = lambda key, **values: translate(key, language=ui_language, **values)
    env.globals["language_code"] = ui_language
    env.globals["text_direction"] = text_direction(ui_language)
    env.globals["csrf_token"] = csrf_token
    env.globals["showcase_mode"] = showcase_mode

    # Static interface assets
    static_dir = Path(__file__).parents[1] / "resources" / "static"
    app.router.add_static("/static", static_dir)

    app.router.add_get("/", web_index)
    app.router.add_get("/status/stream", web_status_stream)
    app.router.add_get("/admin", web_admin)
    app.router.add_post("/admin", web_admin_action)
    app.router.add_get("/devices", web_devices)
    app.router.add_get("/devices/states", web_devices_states)
    app.router.add_post("/devices", web_devices_action)
    app.router.add_post("/devices/rename", web_devices_rename)
    app.router.add_post("/devices/scenario-type", web_devices_change_scenario_type)
    app.router.add_post("/devices/monitor-consumption", web_devices_monitor_consumption)
    app.router.add_post("/devices/add", web_devices_add)
    app.router.add_post("/devices/reload", web_devices_reload)
    app.router.add_post("/devices/scenario", web_scenario_action)
    app.router.add_get("/scenarios", web_scenarios)
    app.router.add_post("/scenarios", web_scenarios_action)
    app.router.add_get("/device-log", web_device_log)
    app.router.add_get("/device-log/stream", web_device_log_stream)
    app.router.add_get("/admin/stream", web_admin_stream)
    app.router.add_get("/settings", web_settings)
    app.router.add_post("/settings", web_settings_action)
    if start_site:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
        await site.start()
        logger.info("Local web interface started on port %d", WEB_PORT)
    return app
