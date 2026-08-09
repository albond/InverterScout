"""Telegram and runtime orchestration for InverterScout."""

import asyncio
import logging
import os
import sys
import time

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from inverterscout import __version__
from inverterscout.core.state import Event, EventBus, StateManager, poll_loop
from inverterscout.devices.manager import DeviceState
from inverterscout.inverter.luxpower import InverterData
from inverterscout.security.logging import sensitive_values, stdout_handler
from inverterscout.settings.i18n import SUPPORTED_LANGUAGES, translate
from inverterscout.settings.runtime import (
    ADMIN_CHAT_ID,
    INVERTER_HOST,
    INVERTER_PORT,
    POLL_INTERVAL,
    TELEGRAM_MODE,
    TELEGRAM_TOKEN,
    estimate_battery_runtime,
    format_duration,
    format_time_human,
    sub_mgr,
)
from inverterscout.storage.encrypted import load_settings

logger = logging.getLogger(__name__)

TELEGRAM_COMMAND_SPECS = (
    ("start", "telegram.command_start"),
    ("stop", "telegram.command_stop"),
    ("battery", "telegram.command_battery"),
    ("help", "telegram.command_help"),
    ("devices", "telegram.command_devices"),
    ("device_on", "telegram.command_device_on"),
    ("device_off", "telegram.command_device_off"),
)
TELEGRAM_COMMAND_NAMES = tuple(name for name, _ in TELEGRAM_COMMAND_SPECS)


def main_keyboard(language: str | None = None) -> ReplyKeyboardMarkup:
    """Build the persistent keyboard in the currently selected language."""
    return ReplyKeyboardMarkup(
        [
            [
                translate("telegram.button_battery", language=language),
                translate("telegram.button_help", language=language),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def telegram_commands(language: str | None = None) -> list[BotCommand]:
    """Build localized command descriptions without changing command names."""
    return [
        BotCommand(command=name, description=translate(key, language=language))
        for name, key in TELEGRAM_COMMAND_SPECS
    ]


async def refresh_telegram_commands(
    app: Application | None,
    language: str | None = None,
) -> bool:
    """Refresh Telegram's slash-command menu for the selected application language."""
    if app is None:
        return False
    try:
        await app.bot.set_my_commands(telegram_commands(language))
    except Exception as exc:
        logger.warning("Failed to refresh Telegram command menu: %s", type(exc).__name__)
        return False
    return True


# ──────────────────────────────────────────────
# Event architecture
# ──────────────────────────────────────────────
event_bus: EventBus | None = None
state_mgr: StateManager | None = None

# Smart home
device_mgr = None  # DeviceManager
scenario_engine = None  # ScenarioEngine

# Link to Telegram app (for sending from event handlers)
_telegram_app: Application | None = None
_polling_request: "PollingHeartbeatRequest | None" = None

TELEGRAM_POLL_STALE_SECONDS = 180
TELEGRAM_WATCHDOG_INTERVAL = 30


class PollingHeartbeatRequest(HTTPXRequest):
    """Track successful getUpdates responses for the polling watchdog."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_success_monotonic = time.monotonic()

    async def do_request(self, *args, **kwargs):
        result = await super().do_request(*args, **kwargs)
        if result[0] == 200:
            self.last_success_monotonic = time.monotonic()
        return result


async def _restart_stale_polling_once(
    app: Application,
    request: PollingHeartbeatRequest,
    stale_after: float = TELEGRAM_POLL_STALE_SECONDS,
) -> bool:
    """Restart only Telegram polling when no successful response was seen."""
    age = time.monotonic() - request.last_success_monotonic
    if age < stale_after:
        return False

    logger.error("Telegram polling has been unresponsive for %.0f seconds; restarting", age)
    try:
        if app.updater.running:
            await app.updater.stop()

        def error_callback(error) -> None:
            app.create_task(app.process_error(error=error, update=None))

        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            error_callback=error_callback,
        )
        return True
    except Exception:
        logger.exception("Failed to restart Telegram polling")
        return False


async def telegram_polling_watchdog(
    app: Application,
    request: PollingHeartbeatRequest,
    interval: float = TELEGRAM_WATCHDOG_INTERVAL,
) -> None:
    """Keep the web process alive while recovering a stuck Telegram poller."""
    while True:
        await asyncio.sleep(interval)
        await _restart_stale_polling_once(app, request)


# ──────────────────────────────────────────────
# Notifications
# ──────────────────────────────────────────────
async def remove_subscriber(chat_id: int) -> None:
    sub_mgr.subscribers.discard(chat_id)
    sub_mgr.save_subscribers()
    logger.info("Subscriber removed after blocking the bot")


async def notify_all(text: str) -> None:
    """Sending a notification to all subscribers."""
    if not _telegram_app:
        return
    to_remove = set()
    approved_recipients = sub_mgr.subscribers - sub_mgr.blocked
    for chat_id in list(approved_recipients):
        try:
            await _telegram_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=main_keyboard(),
            )
        except Forbidden:
            to_remove.add(chat_id)
            logger.info("Subscriber blocked the bot and will be removed")
        except Exception as e:
            logger.error("Subscriber notification failed: %s", type(e).__name__)

    for chat_id in to_remove:
        await remove_subscriber(chat_id)


async def notify_admin(text: str) -> None:
    """Sending a message only to the super-admin (for test_mode devices)."""
    if not _telegram_app:
        return
    try:
        await _telegram_app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"{translate('telegram.test_prefix')} {text}",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        logger.error("Administrator notification failed: %s", type(e).__name__)


# ──────────────────────────────────────────────
# Event handlers (subscribe to StateManager events)
# ──────────────────────────────────────────────
async def on_grid_lost(event: Event) -> None:
    """Notify approved users that grid power is unavailable."""
    d = event.data
    if d.get("prev_status") == "grid_low_voltage":
        reason = translate("telegram.reason_voltage_zero")
    else:
        reason = translate("telegram.reason_power_off")
    await notify_all(
        translate(
            "telegram.alert_grid_lost",
            reason=reason,
            soc=d["soc"],
            voltage=d["battery_voltage"],
        )
    )


async def on_grid_restored(event: Event) -> None:
    """Handle restoration of normal utility-grid power."""
    d = event.data
    duration_line = ""
    if d.get("outage_seconds", 0) > 0:
        duration_line = translate(
            "telegram.outage_duration",
            duration=format_duration(d["outage_seconds"]),
        )

    await notify_all(
        translate(
            "telegram.alert_grid_restored",
            voltage=d["voltage"],
            soc=d["soc"],
            duration=duration_line,
        )
    )

    if d.get("generator_on"):
        await notify_all(translate("telegram.alert_generator_after_grid"))


async def on_grid_low_voltage(event: Event) -> None:
    """Handle low utility-grid voltage."""
    d = event.data
    await notify_all(
        translate(
            "telegram.alert_low_voltage",
            voltage=d["voltage"],
            soc=d["soc"],
            battery_voltage=d["battery_voltage"],
        )
    )


async def on_grid_voltage_normal(event: Event) -> None:
    """The tension has returned to normal."""
    d = event.data
    duration_line = ""
    if d.get("low_voltage_seconds", 0) > 0:
        duration_line = translate(
            "telegram.low_voltage_duration",
            duration=format_duration(d["low_voltage_seconds"]),
        )
    await notify_all(
        translate(
            "telegram.alert_voltage_normal",
            voltage=d["voltage"],
            soc=d["soc"],
            duration=duration_line,
        )
    )


async def on_generator_started(event: Event) -> None:
    """The generator is running."""
    d = event.data
    await notify_all(
        translate(
            "telegram.alert_generator_started",
            voltage=d.get("gen_voltage", 0),
            frequency=d.get("gen_frequency", 0),
            soc=d["soc"],
        )
    )


async def on_generator_stopped(event: Event) -> None:
    """The generator has stopped."""
    d = event.data
    duration_line = ""
    if d.get("run_seconds", 0) > 0:
        duration_line = translate(
            "telegram.generator_duration", duration=format_duration(d["run_seconds"])
        )
    await notify_all(
        translate(
            "telegram.alert_generator_stopped",
            soc=d["soc"],
            duration=duration_line,
        )
    )


async def on_battery_low(event: Event) -> None:
    """Low battery (<=30%)."""
    d = event.data
    duration = ""
    if d.get("grid_lost_time", 0) > 0:
        duration = translate(
            "telegram.no_grid_duration",
            duration=format_duration(int(time.time() - d["grid_lost_time"])),
        )
    estimate_line = ""
    est = estimate_battery_runtime(
        d["soc"],
        d.get("grid_lost_time", 0),
        generator_on=d.get("generator_on", False),
        pre_gen_soc=d.get("pre_gen_soc", 0),
        pre_gen_time=d.get("pre_gen_time", 0),
    )
    if est:
        estimate_line = translate(
            "telegram.estimate_until",
            remaining=est["remaining_text"],
            until=est["depletion_time_text"],
        )
    await notify_all(
        translate(
            "telegram.alert_battery_low",
            soc=d["soc"],
            voltage=d["battery_voltage"],
            duration=duration,
            estimate=estimate_line,
        )
    )


async def on_battery_critical(event: Event) -> None:
    """Critical battery charge (<=15%)."""
    d = event.data
    estimate_line = ""
    est = estimate_battery_runtime(
        d["soc"],
        d.get("grid_lost_time", 0),
        generator_on=d.get("generator_on", False),
        pre_gen_soc=d.get("pre_gen_soc", 0),
        pre_gen_time=d.get("pre_gen_time", 0),
    )
    if est:
        estimate_line = translate("telegram.estimate_remaining", remaining=est["remaining_text"])
    await notify_all(
        translate(
            "telegram.alert_battery_critical",
            soc=d["soc"],
            voltage=d["battery_voltage"],
            estimate=estimate_line,
        )
    )


async def on_device_no_consumption(event: Event) -> None:
    """Handle a monitored outlet reporting zero consumption while grid power is available."""
    d = event.data
    minutes = max(1, int(d.get("elapsed_sec", 0) // 60))
    await notify_all(
        translate(
            "telegram.alert_no_consumption",
            device=d["device_name"],
            minutes=minutes,
        )
    )


async def on_device_no_consumption_after_recycle(event: Event) -> None:
    """Auto-plugging did not help - consumption never returned."""
    d = event.data
    minutes = max(1, int(d.get("elapsed_sec", 0) // 60))
    await notify_all(
        translate(
            "telegram.alert_no_consumption_after_recycle",
            device=d["device_name"],
            minutes=minutes,
        )
    )


async def on_switched_to_battery(event: Event) -> None:
    """Switched to battery (no_grid). For future modules (boiler, etc.)."""
    d = event.data
    logger.info(
        "ON_BATTERY: soc=%d%%, bat=%.1fV, grid=%.1fV, gen=%s",
        d["soc"],
        d["battery_voltage"],
        d["grid_voltage"],
        d["generator_on"],
    )


async def on_switched_from_battery(event: Event) -> None:
    """Switched from battery to external power. For future modules."""
    d = event.data
    logger.info(
        "OFF_BATTERY: soc=%d%%, source=%s, gen=%s", d["soc"], d["power_source"], d["generator_on"]
    )


# ──────────────────────────────────────────────
# Global Authorization Gateway
# ──────────────────────────────────────────────
async def auth_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only allows subscribers. All others are silence.

    Works in group=-1, before all other handlers.
    Any new functionality is automatically protected."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        raise ApplicationHandlerStop()

    if chat_id in sub_mgr.subscribers:
        return

    if (
        update.message
        and update.message.text
        and update.message.text.startswith("/start")
        and chat_id not in sub_mgr.blocked
        and not any(p["chat_id"] == chat_id for p in sub_mgr.pending)
    ):
        user = update.effective_user
        sub_mgr.pending.append(
            {
                "chat_id": chat_id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        sub_mgr.save_pending()
        sub_mgr.set_user_name(chat_id, user.first_name or "", user.username or "")
        if event_bus:
            await event_bus.emit(
                Event(
                    type="pending_user_added",
                    timestamp=time.time(),
                    data={
                        "chat_id": chat_id,
                        "username": user.username or "",
                        "first_name": user.first_name or "",
                        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
            )
        logger.info("New Telegram access request is pending")

    raise ApplicationHandlerStop()


# ──────────────────────────────────────────────
# Bot commands (subscribers only)
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show status to subscriber. Only subscribers (auth_gate) go here."""
    greeting = translate("telegram.welcome") + "\n\n" + translate("telegram.help")
    await update.message.reply_text(greeting, reply_markup=main_keyboard())
    if event_bus:
        try:
            status = await event_bus.request("get_status")
            data = status.get("last_data")
            if data:
                power_text = _power_source_text(data, status)
                status_text = (
                    f"{power_text}\n\n"
                    f"🔋 {translate('status.battery')}: *{data.soc}%* ({data.battery_voltage:.1f} V)\n"
                    f"🔌 {translate('status.grid')}: {data.grid_power_import} W"
                )
                await update.message.reply_text(
                    status_text, parse_mode="Markdown", reply_markup=main_keyboard()
                )
        except Exception:
            pass


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    sub_mgr.subscribers.discard(chat_id)
    sub_mgr.save_subscribers()
    await update.message.reply_text(
        translate("telegram.stopped"), reply_markup=ReplyKeyboardRemove()
    )
    logger.info("Subscriber unsubscribed")


def _power_source_text(data: InverterData, status: dict | None = None) -> str:
    """Text about the power supply for /battery."""
    src = data.power_source
    if src == "grid":
        return translate(
            "telegram.source_grid",
            voltage=data.grid_voltage,
            frequency=data.grid_frequency,
        )
    elif src == "low_voltage":
        return translate("telegram.source_low_voltage", voltage=data.grid_voltage)
    return translate(
        "telegram.source_no_grid",
        voltage=data.grid_voltage,
    )


async def cmd_battery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return

    if not event_bus:
        await update.message.reply_text(
            translate("telegram.starting"),
            reply_markup=main_keyboard(),
        )
        return

    try:
        status = await event_bus.request("get_status")
    except RuntimeError:
        await update.message.reply_text(
            translate("telegram.waiting_for_data"),
            reply_markup=main_keyboard(),
        )
        return

    data = status.get("last_data")
    if data is None:
        await update.message.reply_text(
            translate("telegram.waiting_for_data"),
            reply_markup=main_keyboard(),
        )
        return

    last_data_time = status.get("last_data_time", 0)
    grid = status.get("grid")
    generator = status.get("generator")

    age_s = int(time.time() - last_data_time) if last_data_time > 0 else 0
    power_text = _power_source_text(data, status)

    event_line = ""
    estimate_line = ""
    if data.on_battery:
        if grid and grid.grid_lost_time > 0:
            event_line = translate(
                "telegram.event_grid_lost",
                when=format_time_human(grid.grid_lost_time),
            )
            est = estimate_battery_runtime(
                data.soc,
                grid.grid_lost_time,
                generator_on=data.generator_on,
                pre_gen_soc=getattr(grid, "pre_gen_soc", 0),
                pre_gen_time=getattr(grid, "pre_gen_time", 0),
            )
            if est:
                estimate_line = translate(
                    "telegram.estimate_until",
                    remaining=est["remaining_text"],
                    until=est["depletion_time_text"],
                )
    else:
        if grid and grid.grid_restored_time > 0:
            event_line = translate(
                "telegram.event_grid_restored",
                when=format_time_human(grid.grid_restored_time),
            )
        elif grid and grid.grid_lost_time > 0:
            event_line = translate(
                "telegram.event_last_outage",
                when=format_time_human(grid.grid_lost_time),
            )

    gen_line = ""
    if generator and generator.status == "gen_on":
        gen_line = translate("telegram.generator_running")
        if data.gen_voltage > 0:
            gen_line += f" ({data.gen_voltage:.0f}V, {data.gen_power}W)"

    grid_line = (
        "" if data.on_battery else translate("telegram.grid_import", power=data.grid_power_import)
    )

    text = translate(
        "telegram.battery_summary",
        source=power_text,
        soc=data.soc,
        battery_voltage=data.battery_voltage,
        house_power=data.house_power,
        grid_line=grid_line,
        generator_line=gen_line,
        event_line=event_line,
        estimate_line=estimate_line,
        age=age_s,
    )

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    help_buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(translate("nav.devices"), callback_data="help:devices")],
            [
                InlineKeyboardButton(translate("devices.turn_on"), callback_data="help:device_on"),
                InlineKeyboardButton(
                    translate("devices.turn_off"), callback_data="help:device_off"
                ),
            ],
        ]
    )
    await update.message.reply_text(
        translate("telegram.help_full"),
        parse_mode="Markdown",
        reply_markup=help_buttons,
    )


async def on_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help-menu inline buttons."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    cmd = query.data.split(":", 1)[1]  # "devices", "device_on", "device_off"
    if not device_mgr:
        await query.message.reply_text(
            translate("telegram.device_module_unavailable"), reply_markup=main_keyboard()
        )
        return
    devices = device_mgr.list_devices()
    if cmd == "devices":
        enabled = [d for d in devices if d.get("enabled", True)]
        if not enabled:
            await query.message.reply_text(
                translate("telegram.no_active_devices"), reply_markup=main_keyboard()
            )
            return
        states = await _fetch_device_states(device_mgr, devices)
        text = _format_devices_list(devices, lambda did: states.get(did, DeviceState()))
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())
    elif cmd in ("device_on", "device_off"):
        action = "on" if cmd == "device_on" else "off"
        states = await _fetch_device_states(device_mgr, devices)
        text, keyboard = _build_action_keyboard(
            devices, lambda did: states.get(did, DeviceState()), action
        )
        await query.message.reply_text(text, reply_markup=keyboard or main_keyboard())


async def handle_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle current and previously rendered persistent-keyboard labels."""
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    text = update.message.text
    battery_labels = {
        translate("telegram.button_battery", language=language) for language in SUPPORTED_LANGUAGES
    }
    help_labels = {
        translate("telegram.button_help", language=language) for language in SUPPORTED_LANGUAGES
    }
    if text in battery_labels:
        await cmd_battery(update, context)
    elif text in help_labels:
        await cmd_help(update, context)


# ──────────────────────────────────────────────
# Managing devices from Telegram
# ──────────────────────────────────────────────
def _format_devices_list(devices: list[dict], get_state_fn) -> str:
    """Formats a list of devices with their status.

    devices: dict list with keys id, name, enabled.
    get_state_fn: callable(device_id) → DeviceState.
    Returns the text of the message."""
    enabled = [d for d in devices if d.get("enabled", True)]
    if not enabled:
        return translate("telegram.no_active_devices")
    lines = [translate("telegram.devices_title")]
    for d in enabled:
        state = get_state_fn(d["id"])
        if state.on is True:
            icon = "⚡"
        elif state.on is False:
            icon = "💤"
        else:
            icon = "❓"
        lines.append(f"{icon} {d['name']}")
    return "\n".join(lines)


def _build_action_keyboard(devices: list[dict], get_state_fn, action: str):
    """Assembles an inline keyboard for turning devices on/off.

    action: 'on' or 'off'.
    Returns (text, InlineKeyboardMarkup | None)."""
    enabled = [d for d in devices if d.get("enabled", True)]
    buttons = []
    for d in enabled:
        state = get_state_fn(d["id"])
        if action == "on" and state.on is False:
            buttons.append(InlineKeyboardButton(d["name"], callback_data=f"dev_on:{d['id']}"))
        elif action == "off" and state.on is True:
            buttons.append(InlineKeyboardButton(d["name"], callback_data=f"dev_off:{d['id']}"))
    if not buttons:
        if action == "on":
            return translate("telegram.all_devices_on"), None
        else:
            return translate("telegram.all_devices_off"), None
    text = translate("telegram.select_device")
    keyboard = InlineKeyboardMarkup([[b] for b in buttons])
    return text, keyboard


async def _fetch_device_states(dm, devices: list[dict]) -> dict:
    """Request on/off for all enabled devices IN PARALLEL. Returns {id: DeviceState}."""
    states = {}

    async def _fetch_one(did: str):
        try:
            state = await dm.get_device_state(did)
            states[did] = state or DeviceState(online=False)
        except Exception:
            states[did] = DeviceState(online=False)

    enabled = [d for d in devices if d.get("enabled", True)]
    tasks = [asyncio.create_task(_fetch_one(d["id"])) for d in enabled]
    if tasks:
        await asyncio.gather(*tasks)
    return states


async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List of devices with current status (ping + Tuya request)."""
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    if not device_mgr:
        await update.message.reply_text(
            translate("telegram.device_module_unavailable"), reply_markup=main_keyboard()
        )
        return
    devices = device_mgr.list_devices()
    enabled = [d for d in devices if d.get("enabled", True)]
    if not enabled:
        await update.message.reply_text(
            translate("telegram.no_active_devices"), reply_markup=main_keyboard()
        )
        return
    states = await _fetch_device_states(device_mgr, devices)
    text = _format_devices_list(devices, lambda did: states.get(did, DeviceState()))
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


async def cmd_device_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show inline buttons to turn on disabled devices."""
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    if not device_mgr:
        await update.message.reply_text(
            translate("telegram.device_module_unavailable"), reply_markup=main_keyboard()
        )
        return
    devices = device_mgr.list_devices()
    states = await _fetch_device_states(device_mgr, devices)
    text, keyboard = _build_action_keyboard(
        devices, lambda did: states.get(did, DeviceState()), "on"
    )
    await update.message.reply_text(text, reply_markup=keyboard or main_keyboard())


async def cmd_device_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show inline buttons to turn off enabled devices."""
    chat_id = update.effective_chat.id
    if chat_id not in sub_mgr.subscribers:
        return
    if not device_mgr:
        await update.message.reply_text(
            translate("telegram.device_module_unavailable"), reply_markup=main_keyboard()
        )
        return
    devices = device_mgr.list_devices()
    states = await _fetch_device_states(device_mgr, devices)
    text, keyboard = _build_action_keyboard(
        devices, lambda did: states.get(did, DeviceState()), "off"
    )
    await update.message.reply_text(text, reply_markup=keyboard or main_keyboard())


async def on_device_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an inline device power action."""
    query = update.callback_query
    await query.answer()

    data = query.data  # dev_on:boiler or dev_off:boiler
    parts = data.split(":", 1)
    if len(parts) != 2:
        return
    prefix, device_id = parts
    action = "turn_on" if prefix == "dev_on" else "turn_off"
    action_label = (
        translate("telegram.action_turn_on")
        if action == "turn_on"
        else translate("telegram.action_turn_off")
    )

    if not device_mgr:
        await query.edit_message_text(translate("telegram.device_module_unavailable"))
        return

    cfg = device_mgr.get_device(device_id)
    if not cfg:
        await query.edit_message_text(translate("telegram.device_not_found", device_id=device_id))
        return

    name = cfg.name
    await query.edit_message_text(f"⏳ {action_label}: {name}...")

    ok = await device_mgr.execute_command(device_id, action, source="telegram")
    if ok:
        await query.edit_message_text(f"✅ {name}: {action_label}")
    else:
        await query.edit_message_text(
            translate(
                "telegram.device_action_failed",
                action=action_label,
                device=name,
            )
        )


# ──────────────────────────────────────────────
# post_init — initialization of the event architecture
# ──────────────────────────────────────────────
async def post_init(app: Application | None) -> None:
    global event_bus, state_mgr, device_mgr, scenario_engine, _telegram_app

    _telegram_app = app
    await refresh_telegram_commands(app)
    sub_mgr.load_all()
    logger.info(
        "Loaded %d subscribers, %d pending, %d blocked",
        len(sub_mgr.subscribers),
        len(sub_mgr.pending),
        len(sub_mgr.blocked),
    )

    # Move names from pending
    for p in sub_mgr.pending:
        cid = p["chat_id"]
        if cid not in sub_mgr.user_names:
            sub_mgr.set_user_name(cid, p.get("first_name", ""), p.get("username", ""))

    # Resolve names for subscribers/blocked without a name
    all_ids = sub_mgr.subscribers | sub_mgr.blocked
    missing = [cid for cid in all_ids if cid not in sub_mgr.user_names]
    if missing and app is not None:
        logger.info("Resolving names for %d subscribers", len(missing))
        for cid in missing:
            try:
                chat = await app.bot.get_chat(cid)
                sub_mgr.set_user_name(cid, chat.first_name or "", chat.username or "")
            except Exception as e:
                logger.warning("Failed to resolve a subscriber name: %s", type(e).__name__)
                sub_mgr.user_names[cid] = {"first_name": "", "username": ""}
        sub_mgr.save_user_names()

    # Create EventBus and StateManager
    event_bus = EventBus()
    state_mgr = StateManager(event_bus)

    # Register domain event handlers.
    event_bus.subscribe("grid_lost", on_grid_lost)
    event_bus.subscribe("grid_restored", on_grid_restored)
    event_bus.subscribe("grid_low_voltage", on_grid_low_voltage)
    event_bus.subscribe("grid_voltage_normal", on_grid_voltage_normal)
    event_bus.subscribe("generator_started", on_generator_started)
    event_bus.subscribe("generator_stopped", on_generator_stopped)
    event_bus.subscribe("battery_low", on_battery_low)
    event_bus.subscribe("battery_critical", on_battery_critical)
    event_bus.subscribe("on_battery", on_switched_to_battery)
    event_bus.subscribe("off_battery", on_switched_from_battery)

    # Smart home: devices + scenarios
    from inverterscout.core.scenarios import ScenarioEngine
    from inverterscout.devices.consumption import ConsumptionMonitor
    from inverterscout.devices.manager import DeviceManager, register_driver
    from inverterscout.devices.tapo import TapoDriver
    from inverterscout.devices.tuya import TuyaDriver

    register_driver("tuya", TuyaDriver)
    register_driver("tapo", TapoDriver)
    device_mgr = DeviceManager(event_bus, notify_all, notify_admin)
    scenario_engine = ScenarioEngine(event_bus)
    consumption_monitor = ConsumptionMonitor(event_bus, device_mgr, notify_all)
    consumption_monitor.start()
    event_bus.subscribe("device_no_consumption", on_device_no_consumption)
    event_bus.subscribe(
        "device_no_consumption_after_recycle", on_device_no_consumption_after_recycle
    )
    logger.info(
        "Smart home: %d devices, %d scenarios, consumption monitoring: %d devices",
        len(device_mgr.devices),
        len(scenario_engine.rules),
        len(consumption_monitor._monitored_devices()),
    )

    from inverterscout.interfaces.web import HAS_AIOHTTP, start_web_server

    if HAS_AIOHTTP:
        await start_web_server(
            event_bus,
            state_mgr,
            app,
            device_mgr,
            scenario_engine,
            consumption_monitor=consumption_monitor,
        )

    # Start inverter polling.
    asyncio.create_task(poll_loop(state_mgr, INVERTER_HOST, INVERTER_PORT, POLL_INTERVAL))

    if app is not None and _polling_request:
        asyncio.create_task(telegram_polling_watchdog(app, _polling_request))


async def run_without_telegram() -> None:
    """Run inverter monitoring and the local web UI without Telegram."""
    await post_init(None)
    logger.info("Telegram mode is disabled")
    await asyncio.Event().wait()


def main() -> None:
    global _polling_request
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[stdout_handler(log_format, sensitive_values(load_settings()))],
        force=True,
    )
    # httpx logs include the full Bot API URL and therefore the Telegram token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logger.info("=== InverterScout %s ===", __version__)
    logger.info("Python %s", sys.version)
    from inverterscout.interfaces.web import HAS_AIOHTTP as _has_aiohttp

    logger.info("Web UI available: %s", _has_aiohttp)
    logger.info("Inverter connection configured: %s", bool(INVERTER_HOST))
    logger.info("Polling interval: %d seconds", POLL_INTERVAL)

    if TELEGRAM_MODE == "disabled":
        asyncio.run(run_without_telegram())
        return

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Telegram mode is enabled but no token is configured")

    _polling_request = PollingHeartbeatRequest(
        read_timeout=15,
        connect_timeout=5,
        pool_timeout=5,
    )
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .get_updates_request(_polling_request)
        .post_init(post_init)
        .build()
    )

    # Global gateway: non-subscribers don't go any further
    app.add_handler(TypeHandler(Update, auth_gate), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("battery", cmd_battery))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("device_on", cmd_device_on))
    app.add_handler(CommandHandler("device_off", cmd_device_off))
    app.add_handler(CallbackQueryHandler(on_help_callback, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(on_device_callback, pattern=r"^dev_(on|off):"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_button_text,
        )
    )

    logger.info("Starting Telegram polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
