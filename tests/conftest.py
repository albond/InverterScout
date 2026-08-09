"""Fixtures for inverter-scout integration tests."""

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Isolate persistence and credentials before importing any application module.
# Environment changes stay inside the pytest process and cannot affect the parent shell.
_TEST_PROCESS_DATA_DIR = Path(tempfile.mkdtemp(prefix="inverterscout-tests-"))
atexit.register(shutil.rmtree, _TEST_PROCESS_DATA_DIR, ignore_errors=True)

for _environment_name in (
    "ADMIN_CHAT_ID",
    "DONGLE_SERIAL",
    "INVERTERSCOUT_DATABASE",
    "INVERTERSCOUT_KEY_FILE",
    "INVERTERSCOUT_MASTER_KEY",
    "INVERTER_HOST",
    "INVERTER_PORT",
    "INVERTER_SERIAL",
    "TAPO_PASSWORD",
    "TAPO_USERNAME",
    "TELEGRAM_MODE",
    "TELEGRAM_TOKEN",
    "TUYA_ACCESS_ID",
    "TUYA_ACCESS_SECRET",
    "TUYA_REGION",
):
    os.environ.pop(_environment_name, None)
os.environ["INVERTERSCOUT_DATA_DIR"] = str(_TEST_PROCESS_DATA_DIR)

from inverterscout.core.state import Event, EventBus, StateManager  # noqa: E402
from inverterscout.inverter.luxpower import InverterData  # noqa: E402


# ──────────────────────────────────────────────
# File system - isolation from real data
# ──────────────────────────────────────────────
@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def patch_paths(data_dir):
    """Patches all Path constants to tmp_path/data/..."""
    from inverterscout.core import scenarios as scenario_engine
    from inverterscout.devices import manager as device_manager
    from inverterscout.settings import runtime as shared

    patches = [
        patch.object(device_manager, "DEVICES_FILE", data_dir / "devices.json"),
        patch.object(device_manager, "EVENTS_FILE", data_dir / "device_events.json"),
        patch.object(scenario_engine, "SCENARIOS_FILE", data_dir / "scenarios.json"),
        patch.object(scenario_engine, "TIMERS_FILE", data_dir / "device_state.json"),
        patch.object(shared, "SUBSCRIBERS_FILE", data_dir / "subscribers.json"),
        patch.object(shared, "PENDING_FILE", data_dir / "pending.json"),
        patch.object(shared, "BLOCKED_FILE", data_dir / "blocked.json"),
        patch.object(shared, "USER_NAMES_FILE", data_dir / "user_names.json"),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


# ──────────────────────────────────────────────
# EventBus + StateManager
# ──────────────────────────────────────────────
@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def state_mgr(event_bus, data_dir):
    sm = StateManager(event_bus)
    sm.STATE_FILE = data_dir / "last_state.json"
    return sm


@pytest.fixture
def event_collector(event_bus):
    """Subscribe to all events, returns a list of collected Events."""
    collected: list[Event] = []

    async def _handler(event: Event):
        collected.append(event)

    event_bus.subscribe("*", _handler)
    return collected


# ──────────────────────────────────────────────
# Factories InverterData
# ──────────────────────────────────────────────
def make_inverter_data(**overrides) -> InverterData:
    """Creates an InverterData with reasonable defaults (grid OK, soc=80)."""
    defaults = dict(
        status=0x20,
        grid_voltage=220.0,
        grid_frequency=50.0,
        battery_voltage=52.0,
        soc=80,
        battery_charge=0,
        battery_discharge=0,
        eps_power=500,
        grid_power_import=400,
        grid_power_export=0,
        pv1_power=300,
        pv2_power=200,
        ac_input_type=0,
        gen_voltage=0.0,
        gen_frequency=0.0,
        gen_power=0,
    )
    defaults.update(overrides)
    return InverterData(**defaults)


def grid_ok_data(**overrides) -> InverterData:
    return make_inverter_data(**overrides)


def low_voltage_data(**overrides) -> InverterData:
    defaults = dict(status=0x20, grid_voltage=150.0, soc=80)
    defaults.update(overrides)
    return make_inverter_data(**defaults)


def no_grid_data(**overrides) -> InverterData:
    defaults = dict(status=0x40, grid_voltage=0.0, soc=80)
    defaults.update(overrides)
    return make_inverter_data(**defaults)


def generator_on_data(**overrides) -> InverterData:
    defaults = dict(ac_input_type=1, gen_voltage=220.0, gen_frequency=50.0, gen_power=3000)
    defaults.update(overrides)
    return make_inverter_data(**defaults)


# ──────────────────────────────────────────────
# DeviceManager factory
# ──────────────────────────────────────────────
@pytest.fixture
def device_manager_factory(event_bus, data_dir):
    """Factory DeviceManager: writes devices.json, creates DM."""
    from inverterscout.devices import manager as dm_mod

    async def _create(devices_config: list[dict]):
        devices_file = data_dir / "devices.json"
        devices_file.write_text(json.dumps(devices_config, ensure_ascii=False))

        mgr = dm_mod.DeviceManager(
            event_bus,
            notify_all=AsyncMock(),
            notify_admin=AsyncMock(),
        )
        return mgr

    return _create


# ──────────────────────────────────────────────
# ScenarioEngine factory
# ──────────────────────────────────────────────
@pytest.fixture
def scenario_engine_registry():
    """Track created engines so their background tasks can be closed."""
    return []


@pytest.fixture(autouse=True)
async def close_scenario_engines(scenario_engine_registry):
    yield
    for engine in scenario_engine_registry:
        await engine.close()


@pytest.fixture
def scenario_engine_factory(event_bus, data_dir, scenario_engine_registry):
    """ScenarioEngine factory: writes scenarios.json, creates SE."""
    from inverterscout.core import scenarios as se_mod

    def _create(scenarios_config: list[dict]):
        scenarios_file = data_dir / "scenarios.json"
        scenarios_file.write_text(json.dumps(scenarios_config, ensure_ascii=False))
        engine = se_mod.ScenarioEngine(event_bus)
        scenario_engine_registry.append(engine)
        return engine

    return _create
