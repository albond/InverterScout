"""Web server contract tests for the public interface."""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp_jinja2
import jinja2
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from inverterscout.core.state import EventBus
from inverterscout.devtools.showcase import (
    ShowcaseConsumptionMonitor,
    ShowcaseDeviceManager,
    ShowcaseScenarioEngine,
    ShowcaseSettings,
    ShowcaseStateManager,
    ShowcaseSubscriberManager,
)
from inverterscout.interfaces import web as web_server
from inverterscout.interfaces.web import NAV_LINKS, _build_status_json


@dataclass
class FakeInverterData:
    power_source: str = "grid"
    on_battery: bool = False
    soc: int = 80
    battery_voltage: float = 52.0
    grid_voltage: float = 220.0
    grid_frequency: float = 50.0
    house_power: int = 500
    grid_power_import: int = 400
    grid_power_export: int = 0
    total_pv_power: int = 300
    battery_charge: int = 0
    battery_discharge: int = 0
    generator_on: bool = False
    gen_voltage: float = 0.0
    gen_power: int = 0


@dataclass
class FakeGridState:
    grid_lost_time: float = 0
    grid_restored_time: float = 0


@dataclass
class FakeGeneratorState:
    status: str = "gen_off"


def status_response(data=None, grid=None, generator=None):
    return {
        "last_data": data,
        "last_data_time": time.time() if data else 0,
        "grid": grid,
        "generator": generator,
        "poll_count": 42,
        "error_count": 1,
        "last_error": None,
    }


@pytest_asyncio.fixture
async def client(monkeypatch):
    event_bus = MagicMock()
    event_bus.subscribe = MagicMock()
    event_bus.unsubscribe = MagicMock()
    event_bus.request = AsyncMock(
        return_value=status_response(FakeInverterData(), FakeGridState(), FakeGeneratorState())
    )

    state_manager = MagicMock()
    state_manager.grid = FakeGridState()

    device_manager = MagicMock()
    device_manager.devices = {}
    device_manager.list_devices.return_value = []
    device_manager.get_log.return_value = []

    scenario_engine = MagicMock()
    scenario_engine.list_scenarios.return_value = []

    monkeypatch.setattr(web_server, "_test_tcp_connect", lambda *args: "OK")
    app = await web_server.start_web_server(
        event_bus,
        state_manager,
        telegram_app=None,
        device_mgr=device_manager,
        scenario_engine=scenario_engine,
        start_site=False,
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


@pytest_asyncio.fixture
async def showcase_client():
    event_bus = EventBus()
    state_manager = ShowcaseStateManager(event_bus)
    device_manager = ShowcaseDeviceManager(event_bus)
    subscriber_manager = ShowcaseSubscriberManager()
    settings = ShowcaseSettings()
    app = await web_server.start_web_server(
        event_bus,
        state_manager,
        telegram_app=None,
        device_mgr=device_manager,
        scenario_engine=ShowcaseScenarioEngine(),
        consumption_monitor=ShowcaseConsumptionMonitor(device_manager),
        subscriber_mgr=subscriber_manager,
        telegram_mode="enabled",
        settings_loader=settings.load,
        settings_saver=settings.save,
        tcp_tester=lambda *_: "SHOWCASE",
        language="en",
        showcase_mode=True,
        start_site=False,
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client, device_manager, subscriber_manager, settings
    finally:
        await test_client.close()


def test_all_templates_parse():
    template_dir = Path(web_server.__file__).parents[1] / "resources" / "templates"
    environment = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    for path in template_dir.glob("*.html"):
        environment.get_template(path.name)


def test_navigation_includes_settings_and_local_access_management():
    assert len(NAV_LINKS) == 5
    assert ("/admin", "nav.access") in NAV_LINKS
    assert ("/settings", "nav.settings") in NAV_LINKS
    assert all(path.startswith("/") for path, _ in NAV_LINKS)


@pytest.mark.asyncio
async def test_status_page_renders_with_security_headers(client):
    response = await client.get("/")
    body = await response.text()

    assert response.status == 200
    assert "InverterScout" in body
    assert "80%" in body
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_status_page_translates_dynamic_values_for_requested_language():
    event_bus = EventBus()
    state_manager = ShowcaseStateManager(event_bus)
    device_manager = ShowcaseDeviceManager(event_bus)
    subscriber_manager = ShowcaseSubscriberManager()
    settings = ShowcaseSettings()
    app = await web_server.start_web_server(
        event_bus,
        state_manager,
        telegram_app=None,
        device_mgr=device_manager,
        scenario_engine=ShowcaseScenarioEngine(),
        consumption_monitor=ShowcaseConsumptionMonitor(device_manager),
        subscriber_mgr=subscriber_manager,
        telegram_mode="enabled",
        settings_loader=settings.load,
        settings_saver=settings.save,
        tcp_tester=lambda *_: "SHOWCASE",
        language="ar",
        showcase_mode=True,
        start_site=False,
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        response = await test_client.get("/")
        body = await response.text()
    finally:
        await test_client.close()

    assert response.status == 200
    assert 'lang="ar" dir="rtl"' in body
    assert "الشبكة متاحة" in body
    assert "متوقف" in body
    assert "Grid available" not in body
    assert ">stopped<" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin", "/devices", "/settings"])
async def test_primary_pages_render(client, path):
    response = await client.get(path)
    assert response.status == 200
    assert "InverterScout" in await response.text()


@pytest.mark.asyncio
async def test_settings_never_echo_stored_credentials(client, monkeypatch):
    private_values = {
        "tapo_username": "private-account-id",
        "tapo_password": "private-tapo-password",
        "tuya_access_id": "private-tuya-id",
        "tuya_access_secret": "private-tuya-secret",
        "language": "en",
        "timezone": "UTC",
    }
    monkeypatch.setattr(web_server, "load_settings", lambda: private_values.copy())

    response = await client.get("/settings")
    body = await response.text()

    assert response.status == 200
    assert "private-account-id" not in body
    assert "private-tapo-password" not in body
    assert "private-tuya-id" not in body
    assert "private-tuya-secret" not in body
    assert 'autocomplete="new-password"' in body
    assert 'name="timezone"' in body
    assert 'list="timezones"' in body
    assert '<datalist id="timezones"' in body
    assert '<option value="Europe/Kyiv">Europe/Kyiv</option>' in body


@pytest.mark.asyncio
async def test_post_without_csrf_token_is_rejected(client):
    response = await client.post(
        "/settings",
        data={"action": "general", "language": "en", "timezone": "UTC"},
    )
    assert response.status == 403


@pytest.mark.asyncio
async def test_valid_settings_post_saves_only_submitted_values(client, monkeypatch):
    current = {
        "setup_complete": True,
        "telegram_mode": "disabled",
        "language": "en",
        "timezone": "UTC",
    }
    saved = []
    monkeypatch.setattr(web_server, "load_settings", lambda: current.copy())
    monkeypatch.setattr(web_server, "save_settings", lambda value: saved.append(value))
    csrf_token = aiohttp_jinja2.get_env(client.app).globals["csrf_token"]

    response = await client.post(
        "/settings",
        data={
            "csrf_token": csrf_token,
            "action": "general",
            "language": "uk",
            "timezone": "Europe/Kyiv",
        },
        allow_redirects=False,
    )

    assert response.status == 302
    assert saved[0]["language"] == "uk"
    assert saved[0]["timezone"] == "Europe/Kyiv"
    assert saved[0]["telegram_mode"] == "disabled"


@pytest.mark.asyncio
async def test_language_change_refreshes_telegram_menu_and_future_messages():
    event_bus = EventBus()
    settings = ShowcaseSettings()
    subscriber_manager = ShowcaseSubscriberManager()
    telegram_app = MagicMock()
    telegram_app.bot.set_my_commands = AsyncMock()
    telegram_app.bot.send_message = AsyncMock()
    app = await web_server.start_web_server(
        event_bus,
        ShowcaseStateManager(event_bus),
        telegram_app=telegram_app,
        device_mgr=ShowcaseDeviceManager(event_bus),
        scenario_engine=ShowcaseScenarioEngine(),
        subscriber_mgr=subscriber_manager,
        telegram_mode="enabled",
        settings_loader=settings.load,
        settings_saver=settings.save,
        tcp_tester=lambda *_: "SHOWCASE",
        language="en",
        showcase_mode=True,
        start_site=False,
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        csrf_token = aiohttp_jinja2.get_env(app).globals["csrf_token"]
        response = await test_client.post(
            "/settings",
            data={
                "csrf_token": csrf_token,
                "action": "general",
                "language": "de",
                "timezone": "Europe/Berlin",
            },
            allow_redirects=False,
        )
        approve_response = await test_client.post(
            "/admin",
            data={
                "csrf_token": csrf_token,
                "chat_id": "100200301",
                "action": "approve",
            },
            allow_redirects=False,
        )
    finally:
        await test_client.close()

    assert response.status == 302
    assert approve_response.status == 302
    commands = telegram_app.bot.set_my_commands.await_args.args[0]
    assert tuple(command.command for command in commands) == (
        "start",
        "stop",
        "battery",
        "help",
        "devices",
        "device_on",
        "device_off",
    )
    assert commands[0].description == "Aktuellen Status anzeigen"
    sent_message = telegram_app.bot.send_message.await_args.kwargs
    assert sent_message["text"].startswith("Ihre Anfrage wurde freigegeben")
    assert sent_message["reply_markup"].keyboard[0][0].text == "🔋 Batterie"


@pytest.mark.asyncio
async def test_device_states_endpoint_has_stable_object_shape(client):
    response = await client.get("/devices/states")
    assert response.status == 200
    assert await response.json() == {"devices": []}


def test_status_json_without_data_is_explicit():
    assert _build_status_json({"last_data": None}) == {"no_data": True}


def test_status_json_contains_inverter_measurements():
    result = _build_status_json(
        status_response(FakeInverterData(), FakeGridState(), FakeGeneratorState())
    )
    assert result["power_source"] == "Grid available"
    assert result["soc"] == 80
    assert result["soc_color"] == "#0f0"
    assert result["grid_voltage"] == "220V / 50.0Hz"
    assert result["house_power"] == 500
    assert result["total_pv_power"] == 300


@pytest.mark.parametrize(("soc", "color"), [(80, "#0f0"), (30, "#ff0"), (10, "#f44")])
def test_status_json_soc_colors(soc, color):
    result = _build_status_json(status_response(FakeInverterData(soc=soc)))
    assert result["soc_color"] == color


def test_status_json_reports_battery_and_generator_flow():
    data = FakeInverterData(
        power_source="no_grid",
        on_battery=True,
        battery_discharge=300,
        gen_voltage=220,
        gen_power=3000,
    )
    result = _build_status_json(
        status_response(data, FakeGridState(), FakeGeneratorState("gen_on"))
    )
    assert result["power_source"] == "Grid unavailable"
    assert result["power_source_color"] == "#f44"
    assert result["bat_flow"] == "↓ 300W"
    assert "running" in result["generator"]


@pytest.mark.asyncio
async def test_showcase_renders_populated_device_and_access_states(showcase_client):
    client, _, _, _ = showcase_client

    devices_response = await client.get("/devices")
    devices_body = await devices_response.text()
    access_response = await client.get("/admin")
    access_body = await access_response.text()

    assert devices_response.status == 200
    assert "UI showcase · Synthetic data only" in devices_body
    assert "Laundry plug" in devices_body
    assert "Garden lights" in devices_body
    assert "Workshop lamp" in devices_body
    assert "Consumption tracking" in devices_body
    assert access_response.status == 200
    assert "@maya_demo" in access_body
    assert "Waiting" in access_body
    assert "Approved" in access_body
    assert "Blocked" in access_body


@pytest.mark.asyncio
async def test_showcase_device_actions_change_only_in_memory(showcase_client):
    client, device_manager, _, _ = showcase_client
    csrf_token = aiohttp_jinja2.get_env(client.app).globals["csrf_token"]

    response = await client.post(
        "/devices",
        data={
            "csrf_token": csrf_token,
            "device_id": "tuya-garden",
            "action": "turn_on",
        },
        headers={"Accept": "application/json"},
    )
    await asyncio.sleep(0.2)
    states = await (await client.get("/devices/states")).json()
    garden = next(item for item in states["devices"] if item["id"] == "tuya-garden")

    assert response.status == 200
    assert garden["online"] is True
    assert garden["on"] is True
    assert device_manager.load_events()[-1]["details"] == "Showcase interaction"


@pytest.mark.asyncio
async def test_showcase_access_action_updates_in_memory_allowlist(showcase_client):
    client, _, subscriber_manager, _ = showcase_client
    csrf_token = aiohttp_jinja2.get_env(client.app).globals["csrf_token"]

    response = await client.post(
        "/admin",
        data={"csrf_token": csrf_token, "chat_id": "100200301", "action": "approve"},
        allow_redirects=False,
    )

    assert response.status == 302
    assert 100200301 in subscriber_manager.subscribers
    assert all(item["chat_id"] != 100200301 for item in subscriber_manager.pending)


@pytest.mark.asyncio
async def test_showcase_add_device_and_settings_never_use_real_backends(showcase_client):
    client, device_manager, _, settings = showcase_client
    csrf_token = aiohttp_jinja2.get_env(client.app).globals["csrf_token"]

    add_response = await client.post(
        "/devices/add",
        data={
            "csrf_token": csrf_token,
            "provider": "tapo",
            "ip": "192.0.2.99",
            "tapo_name": "Reading nook",
        },
        headers={"Accept": "application/json"},
    )
    settings_response = await client.post(
        "/settings",
        data={
            "csrf_token": csrf_token,
            "action": "general",
            "language": "de",
            "timezone": "Europe/Berlin",
        },
        allow_redirects=False,
    )

    assert await add_response.json() == {
        "ok": True,
        "name": "Reading nook",
        "scenarios": 0,
    }
    assert device_manager.get_device("tapo-new-1").name == "Reading nook"
    assert settings_response.status == 302
    assert settings.values["language"] == "de"
    assert settings.values["timezone"] == "Europe/Berlin"
