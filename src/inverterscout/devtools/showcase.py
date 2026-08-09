"""Run an isolated, in-memory UI showcase with synthetic household data."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiohttp import web

from inverterscout.core.state import (
    BatteryState,
    Event,
    EventBus,
    GeneratorState,
    GridState,
    HouseState,
)
from inverterscout.devices.manager import DeviceConfig, DeviceState
from inverterscout.interfaces.web import start_web_server
from inverterscout.inverter.luxpower import InverterData, InverterStatus
from inverterscout.settings.i18n import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)


class ShowcaseStateManager:
    """Expose stable inverter readings without using persistent state."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.battery = BatteryState(on_battery=False, soc=78, battery_voltage=52.4)
        self.grid = GridState(
            status="grid_ok",
            voltage=231.0,
            frequency=50.0,
            grid_restored_time=time.time() - 4820,
        )
        self.generator = GeneratorState(status="gen_off")
        self.house = HouseState(house_power=1380, grid_import=620)
        self.last_data = InverterData(
            status=InverterStatus.ON_GRID,
            battery_voltage=52.4,
            soc=78,
            soh=97,
            pv1_power=410,
            pv2_power=350,
            grid_voltage=231.0,
            grid_frequency=50.0,
            grid_power_import=620,
        )
        self.last_data_time = time.time() - 8
        self.poll_count = 18432
        self.error_count = 2
        self.last_error = None
        event_bus.subscribe("get_status", self._handle_status)

    async def _handle_status(self, event: Event) -> None:
        event.respond(
            {
                "battery": self.battery,
                "grid": self.grid,
                "generator": self.generator,
                "house": self.house,
                "last_data": self.last_data,
                "last_data_time": self.last_data_time,
                "poll_count": self.poll_count,
                "error_count": self.error_count,
                "last_error": self.last_error,
            }
        )

    def save_state(self) -> None:
        """Keep showcase changes in memory."""


class ShowcaseSubscriberManager:
    """In-memory Telegram allowlist with clearly fictional identities."""

    def __init__(self):
        self.pending = [
            {"chat_id": 100200301, "date": "2026-08-09 09:42"},
            {"chat_id": 100200302, "date": "2026-08-09 10:18"},
        ]
        self.subscribers = {100200303, 100200304}
        self.blocked = {100200305}
        self.user_names = {
            100200301: {"first_name": "Maya", "username": "maya_demo"},
            100200302: {"first_name": "Noah", "username": "noah_demo"},
            100200303: {"first_name": "Sofia", "username": "sofia_demo"},
            100200304: {"first_name": "Daniel", "username": "daniel_demo"},
            100200305: {"first_name": "Alex", "username": "alex_demo"},
        }

    def get_display_name(self, chat_id: int) -> str:
        return self.user_names.get(chat_id, {}).get("first_name", "—")

    def get_username(self, chat_id: int) -> str:
        username = self.user_names.get(chat_id, {}).get("username", "")
        return f"@{username}" if username else "—"

    def save_pending(self) -> None:
        """Keep showcase changes in memory."""

    def save_subscribers(self) -> None:
        """Keep showcase changes in memory."""

    def save_blocked(self) -> None:
        """Keep showcase changes in memory."""


@dataclass
class ShowcaseDevice:
    config: DeviceConfig
    state: DeviceState
    power_w: int | None = None


class ShowcaseDeviceManager:
    """Simulate Tapo and Tuya devices without network or disk access."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._items: dict[str, ShowcaseDevice] = {}
        self._events = self._initial_events()
        self._sequence = 0
        self._add(
            "tapo-laundry",
            "Laundry plug",
            "tapo",
            "192.0.2.21",
            online=True,
            on=True,
            power_w=684,
            monitor=True,
        )
        self._add(
            "tuya-garden",
            "Garden lights",
            "tuya",
            "192.0.2.31",
            online=True,
            on=False,
        )
        self._add(
            "tapo-workshop",
            "Workshop lamp",
            "tapo",
            "192.0.2.41",
            online=False,
            on=None,
            power_w=None,
            monitor=True,
        )

    @property
    def devices(self) -> dict[str, DeviceConfig]:
        return {device_id: item.config for device_id, item in self._items.items()}

    @staticmethod
    def _initial_events() -> list[dict]:
        now = datetime.now()

        def entry(
            minutes: int,
            device_id: str,
            name: str,
            action: str,
            source: str,
            details: str,
            ok: bool = True,
        ) -> dict:
            moment = now - timedelta(minutes=minutes)
            return {
                "time": moment.strftime("%d.%m %H:%M:%S"),
                "ts": int(moment.timestamp()),
                "device_id": device_id,
                "device_name": name,
                "action": action,
                "source": source,
                "details": details,
                "ok": ok,
            }

        return [
            entry(72, "tuya-garden", "Garden lights", "turn_off", "scenario", "Grid restored"),
            entry(41, "tapo-laundry", "Laundry plug", "turn_on", "web", "Manual control"),
            entry(
                16,
                "tapo-workshop",
                "Workshop lamp",
                "turn_on",
                "scenario",
                "Device unreachable",
                ok=False,
            ),
            entry(4, "tuya-garden", "Garden lights", "turn_off", "detected", "State synced"),
        ]

    def _add(
        self,
        device_id: str,
        name: str,
        provider: str,
        host: str,
        *,
        online: bool,
        on: bool | None,
        power_w: int | None = None,
        monitor: bool = False,
    ) -> None:
        config = DeviceConfig(
            id=device_id,
            name=name,
            provider=provider,
            host=host,
            config={"monitor_consumption": monitor},
        )
        self._items[device_id] = ShowcaseDevice(
            config=config,
            state=DeviceState(online=online, on=on),
            power_w=power_w,
        )

    def list_devices(self) -> list[dict]:
        return [
            {
                "id": item.config.id,
                "name": item.config.name,
                "provider": item.config.provider,
                "device_type": item.config.device_type,
                "enabled": item.config.enabled,
                "test_mode": True,
            }
            for item in self._items.values()
        ]

    def get_device(self, device_id: str) -> DeviceConfig | None:
        item = self._items.get(device_id)
        return item.config if item else None

    async def get_device_state(self, device_id: str) -> DeviceState | None:
        await asyncio.sleep(0.08)
        item = self._items.get(device_id)
        return item.state if item else None

    async def execute_command(
        self,
        device_id: str,
        action: str,
        params: dict | None = None,
        source: str = "",
        source_detail: str = "",
    ) -> bool:
        await asyncio.sleep(0.16)
        item = self._items.get(device_id)
        ok = bool(item and item.config.enabled and item.state.online)
        if ok and item:
            if action == "turn_on":
                item.state.on = True
                if item.power_w is not None:
                    item.power_w = 684
            elif action == "turn_off":
                item.state.on = False
                if item.power_w is not None:
                    item.power_w = 0
            elif action == "set_level":
                item.state.level = int((params or {}).get("level", 100))

        name = item.config.name if item else device_id
        now = datetime.now()
        event_data = {
            "device_id": device_id,
            "device_name": name,
            "action": action,
            "source": source,
            "source_detail": source_detail,
        }
        self._events.append(
            {
                "time": now.strftime("%d.%m %H:%M:%S"),
                "ts": int(now.timestamp()),
                **event_data,
                "details": "Showcase interaction",
                "ok": ok,
            }
        )
        await self.event_bus.emit(
            Event(
                type="device_command_ok" if ok else "device_command_failed",
                timestamp=time.time(),
                data=event_data,
            )
        )
        return ok

    def add_showcase_device(
        self, provider: str, name: str, host: str, external_id: str = ""
    ) -> bool:
        if provider not in {"tapo", "tuya"}:
            return False
        self._sequence += 1
        device_id = f"{provider}-new-{self._sequence}"
        friendly_name = name.strip() or f"{provider.title()} device"
        self._add(
            device_id,
            friendly_name,
            provider,
            host or "192.0.2.99",
            online=True,
            on=False,
            power_w=0 if provider == "tapo" else None,
            monitor=provider == "tapo",
        )
        return True

    def load_events(self) -> list[dict]:
        return [event.copy() for event in self._events]

    def set_device_enabled(self, device_id: str, enabled: bool) -> bool:
        item = self._items.get(device_id)
        if not item:
            return False
        item.config.enabled = enabled
        return True

    def set_monitor_consumption(self, device_id: str, enabled: bool) -> bool:
        item = self._items.get(device_id)
        if not item:
            return False
        item.config.config["monitor_consumption"] = enabled
        return True

    def rename_device(self, device_id: str, new_name: str) -> bool:
        item = self._items.get(device_id)
        if not item:
            return False
        item.config.name = new_name
        return True

    def reload_from_file(self) -> dict:
        return {"added": [], "removed": [], "updated": []}


class ShowcaseConsumptionMonitor:
    """Return simulated live power readings."""

    def __init__(self, device_manager: ShowcaseDeviceManager):
        self.device_manager = device_manager

    def get_last_power(self, device_id: str) -> int | None:
        item = self.device_manager._items.get(device_id)
        return item.power_w if item else None


class ShowcaseScenarioEngine:
    """Provide representative automation metadata for device cards."""

    def __init__(self):
        self._scenarios = {
            "tapo-laundry": [
                {
                    "id": "tapo-laundry_off_battery",
                    "name": "Laundry plug: off while on battery",
                    "trigger_event": "on_battery",
                    "enabled": True,
                    "timer_remaining": None,
                },
                {
                    "id": "tapo-laundry_on_grid",
                    "name": "Laundry plug: on when grid power returns",
                    "trigger_event": "off_battery",
                    "enabled": True,
                    "timer_remaining": None,
                },
            ],
            "tuya-garden": [
                {
                    "id": "tuya-garden_off_battery",
                    "name": "Garden lights: pause during an outage",
                    "trigger_event": "on_battery",
                    "enabled": True,
                    "timer_remaining": 5940,
                }
            ],
        }

    def get_scenarios_for_device(self, device_id: str) -> list[dict]:
        return [item.copy() for item in self._scenarios.get(device_id, [])]

    def get_rule(self, scenario_id: str):
        seconds = 7200 if scenario_id == "tuya-garden_off_battery" else 0
        return SimpleNamespace(revert_after_seconds=seconds)

    def set_enabled_for_device(self, device_id: str, enabled: bool) -> int:
        items = self._scenarios.get(device_id, [])
        for item in items:
            item["enabled"] = enabled
        return len(items)

    def set_enabled(self, scenario_id: str, enabled: bool) -> bool:
        for items in self._scenarios.values():
            for item in items:
                if item["id"] == scenario_id:
                    item["enabled"] = enabled
                    return True
        return False

    def remove_scenarios_for_device(self, device_id: str) -> int:
        return len(self._scenarios.pop(device_id, []))

    def add_scenarios(self, scenarios: list[dict]) -> int:
        for scenario in scenarios:
            device_id = scenario["actions"][0]["device_id"]
            self._scenarios.setdefault(device_id, []).append(scenario)
        return len(scenarios)

    def reload_from_file(self) -> int:
        return sum(len(items) for items in self._scenarios.values())


class ShowcaseSettings:
    """Use a private in-memory settings dictionary for the showcase."""

    def __init__(self):
        self.values = {
            "setup_complete": True,
            "language": "en",
            "timezone": "Europe/London",
            "telegram_mode": "enabled",
            "tapo_username": "synthetic",
            "tapo_password": "synthetic",
            "tuya_access_id": "synthetic",
            "tuya_access_secret": "synthetic",
            "tuya_region": "eu",
        }

    def load(self) -> dict:
        return self.values.copy()

    def save(self, values: dict) -> None:
        self.values = values.copy()


async def run_showcase(host: str, port: int, language: str = "en") -> None:
    """Build and serve the showcase until interrupted."""
    event_bus = EventBus()
    state_manager = ShowcaseStateManager(event_bus)
    device_manager = ShowcaseDeviceManager(event_bus)
    scenario_engine = ShowcaseScenarioEngine()
    subscriber_manager = ShowcaseSubscriberManager()
    settings = ShowcaseSettings()

    app = await start_web_server(
        event_bus,
        state_manager,
        telegram_app=None,
        device_mgr=device_manager,
        scenario_engine=scenario_engine,
        consumption_monitor=ShowcaseConsumptionMonitor(device_manager),
        subscriber_mgr=subscriber_manager,
        telegram_mode="enabled",
        settings_loader=settings.load,
        settings_saver=settings.save,
        tcp_tester=lambda *_: "SHOWCASE",
        language=language,
        showcase_mode=True,
        start_site=False,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"InverterScout UI showcase: http://{host}:{port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated InverterScout UI showcase with synthetic data."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2301)
    parser.add_argument("--language", choices=sorted(SUPPORTED_LANGUAGES), default="en")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Allow binding to a non-loopback address (data remains synthetic).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_network:
        raise SystemExit("Use --allow-network to bind the showcase beyond this computer.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run_showcase(args.host, args.port, args.language))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
