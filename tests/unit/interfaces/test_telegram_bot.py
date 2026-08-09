"""Tests of device control commands in the Telegram bot.

Testing the auxiliary functions _format_devices_list, _build_action_keyboard,
_fetch_device_states."""

from unittest.mock import AsyncMock, MagicMock, patch

from inverterscout.devices.manager import DeviceState
from inverterscout.interfaces.telegram import (
    TELEGRAM_COMMAND_NAMES,
    PollingHeartbeatRequest,
    _build_action_keyboard,
    _fetch_device_states,
    _format_devices_list,
    _restart_stale_polling_once,
    cmd_help,
    handle_button_text,
    main_keyboard,
    on_help_callback,
    refresh_telegram_commands,
    sub_mgr,
    telegram_commands,
)
from inverterscout.settings.i18n import SUPPORTED_LANGUAGES, translate

# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────
DEVICE_BOILER = {"id": "boiler", "name": "Boiler", "enabled": True}
DEVICE_LAMP = {"id": "lamp", "name": "Lamp", "enabled": True}
DEVICE_DISABLED = {"id": "pump", "name": "Pump", "enabled": False}


class TestTelegramLocalization:
    def test_persistent_keyboard_is_built_for_every_language(self):
        for language in SUPPORTED_LANGUAGES:
            keyboard = main_keyboard(language)
            assert keyboard.keyboard[0][0].text == translate(
                "telegram.button_battery", language=language
            )
            assert keyboard.keyboard[0][1].text == translate(
                "telegram.button_help", language=language
            )

    def test_command_names_never_change_when_descriptions_are_translated(self):
        descriptions_by_language = {}
        for language in SUPPORTED_LANGUAGES:
            commands = telegram_commands(language)
            assert tuple(command.command for command in commands) == TELEGRAM_COMMAND_NAMES
            descriptions_by_language[language] = tuple(command.description for command in commands)
            assert all(command.description for command in commands)

        assert descriptions_by_language["en"] != descriptions_by_language["de"]
        assert descriptions_by_language["en"] != descriptions_by_language["ja"]

    async def test_command_menu_refresh_uses_bot_api_without_sending_messages(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        app.bot.send_message = AsyncMock()

        refreshed = await refresh_telegram_commands(app, language="uk")

        assert refreshed is True
        commands = app.bot.set_my_commands.await_args.args[0]
        assert tuple(command.command for command in commands) == TELEGRAM_COMMAND_NAMES
        assert commands[0].description == translate("telegram.command_start", language="uk")
        app.bot.send_message.assert_not_called()

    async def test_button_from_previous_language_still_works_after_language_change(self):
        chat_id = 987654
        sub_mgr.subscribers.add(chat_id)
        try:
            update = MagicMock()
            update.effective_chat.id = chat_id
            update.message.text = translate("telegram.button_battery", language="de")
            context = MagicMock()

            with patch(
                "inverterscout.interfaces.telegram.cmd_battery",
                new=AsyncMock(),
            ) as battery_command:
                await handle_button_text(update, context)

            battery_command.assert_awaited_once_with(update, context)
        finally:
            sub_mgr.subscribers.discard(chat_id)


def make_state_fn(states: dict):
    """Creates get_state_fn from the {device_id: DeviceState} dictionary."""

    def fn(device_id):
        return states.get(device_id, DeviceState())

    return fn


# ──────────────────────────────────────────────
# _format_devices_list
# ──────────────────────────────────────────────
class TestFormatDevicesList:
    def test_devices_list_only_enabled(self):
        """Deactivated devices are not shown."""
        devices = [DEVICE_BOILER, DEVICE_DISABLED]
        states = {"boiler": DeviceState(online=True, on=True)}
        text = _format_devices_list(devices, make_state_fn(states))
        assert "Boiler" in text
        assert "Pump" not in text

    def test_devices_list_shows_on_off_status(self):
        """Correct icons for enabled, disabled and unknown."""
        devices = [DEVICE_BOILER, DEVICE_LAMP, {"id": "fan", "name": "Fan", "enabled": True}]
        states = {
            "boiler": DeviceState(online=True, on=True),
            "lamp": DeviceState(online=True, on=False),
            "fan": DeviceState(online=False, on=None),
        }
        text = _format_devices_list(devices, make_state_fn(states))
        assert "⚡ Boiler" in text
        assert "💤 Lamp" in text
        assert "❓ Fan" in text

    def test_devices_list_empty(self):
        """No devices → corresponding text."""
        text = _format_devices_list([], make_state_fn({}))
        assert "No active devices" in text

    def test_devices_list_all_disabled(self):
        """All devices are deactivated → no active ones."""
        devices = [DEVICE_DISABLED]
        text = _format_devices_list(devices, make_state_fn({}))
        assert "No active devices" in text


# ──────────────────────────────────────────────
# _build_action_keyboard - enable
# ──────────────────────────────────────────────
class TestDeviceOnKeyboard:
    def test_device_on_keyboard_only_off_devices(self):
        """Power buttons are only for switched off devices."""
        devices = [DEVICE_BOILER, DEVICE_LAMP]
        states = {
            "boiler": DeviceState(online=True, on=True),
            "lamp": DeviceState(online=True, on=False),
        }
        text, kb = _build_action_keyboard(devices, make_state_fn(states), "on")
        assert kb is not None
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        assert len(buttons) == 1
        assert buttons[0].text == "Lamp"
        assert buttons[0].callback_data == "dev_on:lamp"

    def test_device_on_keyboard_all_on(self):
        """No action buttons are shown when every device is already enabled."""
        devices = [DEVICE_BOILER, DEVICE_LAMP]
        states = {
            "boiler": DeviceState(online=True, on=True),
            "lamp": DeviceState(online=True, on=True),
        }
        text, kb = _build_action_keyboard(devices, make_state_fn(states), "on")
        assert kb is None
        assert "already on" in text


# ──────────────────────────────────────────────
# _build_action_keyboard - disable
# ──────────────────────────────────────────────
class TestDeviceOffKeyboard:
    def test_device_off_keyboard_only_on_devices(self):
        """Power off buttons are only for switched on devices."""
        devices = [DEVICE_BOILER, DEVICE_LAMP]
        states = {
            "boiler": DeviceState(online=True, on=True),
            "lamp": DeviceState(online=True, on=False),
        }
        text, kb = _build_action_keyboard(devices, make_state_fn(states), "off")
        assert kb is not None
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        assert len(buttons) == 1
        assert buttons[0].text == "Boiler"
        assert buttons[0].callback_data == "dev_off:boiler"

    def test_device_off_keyboard_all_off(self):
        """All are off → no buttons."""
        devices = [DEVICE_BOILER, DEVICE_LAMP]
        states = {
            "boiler": DeviceState(online=True, on=False),
            "lamp": DeviceState(online=True, on=False),
        }
        text, kb = _build_action_keyboard(devices, make_state_fn(states), "off")
        assert kb is None
        assert "already off" in text


# ──────────────────────────────────────────────
# callback_data format
# ──────────────────────────────────────────────
class TestCallbackDataFormat:
    def test_callback_data_format(self):
        """callback_data of buttons corresponds to the format dev_on:id / dev_off:id."""
        devices = [DEVICE_BOILER, DEVICE_LAMP]
        states = {
            "boiler": DeviceState(online=True, on=False),
            "lamp": DeviceState(online=True, on=True),
        }

        # dev_on - boiler is turned off
        _, kb_on = _build_action_keyboard(devices, make_state_fn(states), "on")
        assert kb_on is not None
        btn_on = kb_on.inline_keyboard[0][0]
        assert btn_on.callback_data.startswith("dev_on:")
        assert btn_on.callback_data == "dev_on:boiler"

        # dev_off - lamp is on
        _, kb_off = _build_action_keyboard(devices, make_state_fn(states), "off")
        assert kb_off is not None
        btn_off = kb_off.inline_keyboard[0][0]
        assert btn_off.callback_data.startswith("dev_off:")
        assert btn_off.callback_data == "dev_off:lamp"


# ──────────────────────────────────────────────
# _fetch_device_states - real device polling
# ──────────────────────────────────────────────
class TestFetchDeviceStates:
    async def test_fetch_queries_all_enabled(self):
        """For each enabled device, get_device_state is called (in parallel)."""
        dm = AsyncMock()
        dm.get_device_state = AsyncMock(return_value=DeviceState(online=True, on=True))

        devices = [DEVICE_BOILER, DEVICE_LAMP, DEVICE_DISABLED]
        await _fetch_device_states(dm, devices)

        # get_device_state is called for boiler and lamp, but NOT for disabled pump
        assert dm.get_device_state.call_count == 2
        queried_ids = {c.args[0] for c in dm.get_device_state.call_args_list}
        assert queried_ids == {"boiler", "lamp"}

    async def test_fetch_returns_states(self):
        """Returns a dict with DeviceState for enabled devices."""
        dm = AsyncMock()

        # Parallel call - return by device_id
        async def _get_state(did):
            return {
                "boiler": DeviceState(online=True, on=True),
                "lamp": DeviceState(online=True, on=False),
            }[did]

        dm.get_device_state = AsyncMock(side_effect=_get_state)

        devices = [DEVICE_BOILER, DEVICE_LAMP]
        states = await _fetch_device_states(dm, devices)

        assert states["boiler"].on is True
        assert states["lamp"].on is False

    async def test_fetch_handles_none(self):
        """If get_device_state returned None → DeviceState(online=False)."""
        dm = AsyncMock()
        dm.get_device_state = AsyncMock(return_value=None)

        devices = [DEVICE_BOILER]
        states = await _fetch_device_states(dm, devices)

        assert states["boiler"].online is False
        assert states["boiler"].on is None


# ──────────────────────────────────────────────
# cmd_help - inline device buttons
# ──────────────────────────────────────────────
class TestHelpInlineButtons:
    """Tests of inline buttons in the help."""

    async def test_help_sends_inline_buttons(self):
        """cmd_help sends a message with 3 inline buttons."""
        chat_id = 123
        sub_mgr.subscribers.add(chat_id)
        try:
            update = MagicMock()
            update.effective_chat.id = chat_id
            update.message.reply_text = AsyncMock()
            ctx = MagicMock()

            await cmd_help(update, ctx)

            update.message.reply_text.assert_called_once()
            call_kwargs = update.message.reply_text.call_args
            markup = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get("reply_markup")
            # 2 rows: [Devices], [Enable, Disable]
            assert len(markup.inline_keyboard) == 2
            assert len(markup.inline_keyboard[0]) == 1  # Devices
            assert len(markup.inline_keyboard[1]) == 2  # Enable + Disable
        finally:
            sub_mgr.subscribers.discard(chat_id)

    async def test_help_button_callback_data(self):
        """callback_data of buttons - help:devices, help:device_on, help:device_off."""
        chat_id = 456
        sub_mgr.subscribers.add(chat_id)
        try:
            update = MagicMock()
            update.effective_chat.id = chat_id
            update.message.reply_text = AsyncMock()

            await cmd_help(update, MagicMock())

            markup = update.message.reply_text.call_args.kwargs.get(
                "reply_markup"
            ) or update.message.reply_text.call_args[1].get("reply_markup")
            all_buttons = [btn for row in markup.inline_keyboard for btn in row]
            data_set = {btn.callback_data for btn in all_buttons}
            assert data_set == {"help:devices", "help:device_on", "help:device_off"}
        finally:
            sub_mgr.subscribers.discard(chat_id)


# ──────────────────────────────────────────────
# on_help_callback — handling clicks
# ──────────────────────────────────────────────
class TestOnHelpCallback:
    """Tests of the inline help button handler."""

    def _make_callback_update(self, chat_id: int, data: str):
        """Creates a mock Update with a CallbackQuery."""
        update = MagicMock()
        update.effective_chat.id = chat_id
        update.callback_query.data = data
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock()
        return update

    async def test_help_devices_callback(self):
        """help:devices shows a list of devices."""
        chat_id = 789
        sub_mgr.subscribers.add(chat_id)
        try:
            update = self._make_callback_update(chat_id, "help:devices")
            dm = MagicMock()
            dm.list_devices.return_value = [DEVICE_BOILER]
            dm.get_device_state = AsyncMock(return_value=DeviceState(online=True, on=True))

            with patch("inverterscout.interfaces.telegram.device_mgr", dm):
                await on_help_callback(update, MagicMock())

            update.callback_query.answer.assert_called_once()
            reply = update.callback_query.message.reply_text
            reply.assert_called_once()
            assert "Boiler" in reply.call_args[0][0]
        finally:
            sub_mgr.subscribers.discard(chat_id)

    async def test_help_device_on_callback(self):
        """help:device_on shows buttons to enable."""
        chat_id = 790
        sub_mgr.subscribers.add(chat_id)
        try:
            update = self._make_callback_update(chat_id, "help:device_on")
            dm = MagicMock()
            dm.list_devices.return_value = [DEVICE_BOILER]
            dm.get_device_state = AsyncMock(return_value=DeviceState(online=True, on=False))

            with patch("inverterscout.interfaces.telegram.device_mgr", dm):
                await on_help_callback(update, MagicMock())

            reply = update.callback_query.message.reply_text
            reply.assert_called_once()
            markup = reply.call_args.kwargs.get("reply_markup") or reply.call_args[1].get(
                "reply_markup"
            )
            buttons = [btn for row in markup.inline_keyboard for btn in row]
            assert any(btn.callback_data == "dev_on:boiler" for btn in buttons)
        finally:
            sub_mgr.subscribers.discard(chat_id)

    async def test_help_device_off_callback(self):
        """help:device_off shows buttons to turn off."""
        chat_id = 791
        sub_mgr.subscribers.add(chat_id)
        try:
            update = self._make_callback_update(chat_id, "help:device_off")
            dm = MagicMock()
            dm.list_devices.return_value = [DEVICE_BOILER]
            dm.get_device_state = AsyncMock(return_value=DeviceState(online=True, on=True))

            with patch("inverterscout.interfaces.telegram.device_mgr", dm):
                await on_help_callback(update, MagicMock())

            reply = update.callback_query.message.reply_text
            reply.assert_called_once()
            markup = reply.call_args.kwargs.get("reply_markup") or reply.call_args[1].get(
                "reply_markup"
            )
            buttons = [btn for row in markup.inline_keyboard for btn in row]
            assert any(btn.callback_data == "dev_off:boiler" for btn in buttons)
        finally:
            sub_mgr.subscribers.discard(chat_id)

    async def test_help_callback_no_device_mgr(self):
        """Without a device module - warning."""
        chat_id = 792
        sub_mgr.subscribers.add(chat_id)
        try:
            update = self._make_callback_update(chat_id, "help:devices")
            with patch("inverterscout.interfaces.telegram.device_mgr", None):
                await on_help_callback(update, MagicMock())

            reply = update.callback_query.message.reply_text
            reply.assert_called_once()
            assert "unavailable" in reply.call_args[0][0]
        finally:
            sub_mgr.subscribers.discard(chat_id)


# ──────────────────────────────────────────────
# Telegram polling watchdog
# ──────────────────────────────────────────────
class TestPollingWatchdog:
    async def test_heartbeat_updates_after_success(self):
        request = PollingHeartbeatRequest()
        request.last_success_monotonic = 0

        with (
            patch(
                "telegram.request.HTTPXRequest.do_request",
                new=AsyncMock(return_value=(200, b"{}")),
            ),
            patch("inverterscout.interfaces.telegram.time.monotonic", return_value=123.0),
        ):
            result = await request.do_request("https://example.test", "POST")

        assert result == (200, b"{}")
        assert request.last_success_monotonic == 123.0

    async def test_heartbeat_ignores_error_response(self):
        request = PollingHeartbeatRequest()
        request.last_success_monotonic = 10.0

        with (
            patch(
                "telegram.request.HTTPXRequest.do_request",
                new=AsyncMock(return_value=(502, b"bad gateway")),
            ),
            patch("inverterscout.interfaces.telegram.time.monotonic", return_value=123.0),
        ):
            await request.do_request("https://example.test", "POST")

        assert request.last_success_monotonic == 10.0

    async def test_fresh_polling_is_not_restarted(self):
        app = MagicMock()
        request = MagicMock(last_success_monotonic=100.0)

        with patch("inverterscout.interfaces.telegram.time.monotonic", return_value=150.0):
            restarted = await _restart_stale_polling_once(
                app,
                request,
                stale_after=60,
            )

        assert restarted is False
        app.updater.stop.assert_not_called()
        app.updater.start_polling.assert_not_called()

    async def test_stale_polling_is_restarted(self):
        app = MagicMock()
        app.updater.running = True
        app.updater.stop = AsyncMock()
        app.updater.start_polling = AsyncMock()
        request = MagicMock(last_success_monotonic=100.0)

        with patch("inverterscout.interfaces.telegram.time.monotonic", return_value=200.0):
            restarted = await _restart_stale_polling_once(
                app,
                request,
                stale_after=60,
            )

        assert restarted is True
        app.updater.stop.assert_awaited_once()
        app.updater.start_polling.assert_awaited_once()
