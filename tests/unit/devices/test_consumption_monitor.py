"""ConsumptionMonitor tests.

Tests call _tick() directly without starting a background task.
The driver is modeled via StubDriver with get_current_power_w overridden."""

import json
from unittest.mock import AsyncMock

import pytest

from inverterscout.devices.consumption import ConsumptionMonitor
from inverterscout.devices.manager import _DRIVER_REGISTRY, StubDriver


class FakePowerStub(StubDriver):
    """StubDriver + programmable get_current_power_w."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self._fake_power: int | None = 0

    async def get_current_power_w(self) -> int | None:
        return self._fake_power


@pytest.fixture(autouse=True)
def register_fake_driver():
    _DRIVER_REGISTRY["fake_power"] = FakePowerStub
    yield
    _DRIVER_REGISTRY.pop("fake_power", None)


@pytest.fixture
async def monitored_setup(event_bus, data_dir, device_manager_factory):
    """Scenario: one socket with monitor_consumption=true, window 30 min."""
    devices_config = [
        {
            "id": "ps_nas",
            "name": "PowerStation: NAS",
            "provider": "fake_power",
            "device_type": "switch",
            "host": "192.168.0.99",
            "config": {
                "monitor_consumption": True,
                "consumption_threshold_w": 3,
                "consumption_window_min": 30,
            },
            "enabled": True,
            "test_mode": False,
        }
    ]
    dm = await device_manager_factory(devices_config)
    # Mark the outlet as enabled without issuing a real provider command.
    # (it would do retry+verify for 3 seconds and slow down the test)
    from inverterscout.devices.manager import DeviceState

    dm._last_states["ps_nas"] = DeviceState(online=True, on=True)
    notify_all = AsyncMock()
    monitor = ConsumptionMonitor(
        event_bus,
        dm,
        notify_all,
        poll_interval_sec=1,  # The test invokes _tick manually.
        recycle_pause_sec=0,  # no delay in tests
        state_file=data_dir / "consumption_state.json",
    )
    return dm, monitor, notify_all


def _stub_power_source(monitor, value: str):
    async def _fake():
        return value

    monitor._current_power_source = _fake


def _set_power(dm, device_id: str, watts: int | None):
    dm.drivers[device_id]._fake_power = watts


@pytest.mark.asyncio
async def test_zero_power_starts_streak(monitored_setup):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)
    await monitor._tick()
    assert monitor.state["ps_nas"]["no_load_since"] > 0
    assert monitor.state["ps_nas"]["last_power"] == 0


@pytest.mark.asyncio
async def test_power_returns_resets_streak(monitored_setup):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)
    await monitor._tick()
    assert monitor.state["ps_nas"]["no_load_since"] > 0
    _set_power(dm, "ps_nas", 50)
    await monitor._tick()
    assert monitor.state["ps_nas"]["no_load_since"] == 0


@pytest.mark.asyncio
async def test_no_grid_resets_streak(monitored_setup):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)
    await monitor._tick()
    assert monitor.state["ps_nas"]["no_load_since"] > 0
    _stub_power_source(monitor, "no_grid")
    await monitor._tick()
    assert monitor.state["ps_nas"]["no_load_since"] == 0


@pytest.mark.asyncio
async def test_window_exceeded_triggers_recycle(monitored_setup, event_collector):
    dm, monitor, notify_all = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)

    # The first below-threshold reading starts an incident window.
    await monitor._tick()
    monitor.state["ps_nas"]["no_load_since"] = monitor.state["ps_nas"]["no_load_since"] - 31 * 60

    await monitor._tick()
    types = [e.type for e in event_collector]
    assert "device_no_consumption" in types
    assert monitor.state["ps_nas"]["recycle_done"] is True


@pytest.mark.asyncio
async def test_second_window_after_recycle_triggers_second_alert(monitored_setup, event_collector):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)

    # Simulate a completed recycle followed by another failed window.
    monitor.state["ps_nas"] = {
        "no_load_since": __import__("time").time() - 31 * 60,
        "recycle_done": True,
        "second_alert_sent": False,
        "last_power": 0,
        "last_seen": 0,
    }
    await monitor._tick()
    types = [e.type for e in event_collector]
    assert "device_no_consumption_after_recycle" in types
    assert monitor.state["ps_nas"]["second_alert_sent"] is True


@pytest.mark.asyncio
async def test_second_alert_not_repeated(monitored_setup, event_collector):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)
    monitor.state["ps_nas"] = {
        "no_load_since": __import__("time").time() - 31 * 60,
        "recycle_done": True,
        "second_alert_sent": True,  # already sent
        "last_power": 0,
        "last_seen": 0,
    }
    await monitor._tick()
    types = [e.type for e in event_collector]
    assert "device_no_consumption_after_recycle" not in types


@pytest.mark.asyncio
async def test_disabled_device_skipped(event_bus, data_dir, device_manager_factory):
    devices_config = [
        {
            "id": "ps_off",
            "name": "PS off",
            "provider": "fake_power",
            "device_type": "switch",
            "host": "1.2.3.4",
            "config": {"monitor_consumption": True},
            "enabled": False,  # off
            "test_mode": False,
        }
    ]
    dm = await device_manager_factory(devices_config)
    monitor = ConsumptionMonitor(
        event_bus,
        dm,
        AsyncMock(),
        state_file=data_dir / "consumption_state.json",
    )
    assert monitor._monitored_devices() == []


@pytest.mark.asyncio
async def test_device_without_flag_skipped(event_bus, data_dir, device_manager_factory):
    devices_config = [
        {
            "id": "no_flag",
            "name": "No flag",
            "provider": "fake_power",
            "device_type": "switch",
            "host": "1.2.3.4",
            "config": {},  # without monitor_consumption
            "enabled": True,
            "test_mode": False,
        }
    ]
    dm = await device_manager_factory(devices_config)
    monitor = ConsumptionMonitor(
        event_bus,
        dm,
        AsyncMock(),
        state_file=data_dir / "consumption_state.json",
    )
    assert monitor._monitored_devices() == []


@pytest.mark.asyncio
async def test_state_persisted_to_file(monitored_setup):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 0)
    await monitor._tick()
    assert monitor.state_file.exists()
    saved = json.loads(monitor.state_file.read_text())
    assert "ps_nas" in saved
    assert saved["ps_nas"]["last_power"] == 0


@pytest.mark.asyncio
async def test_state_loaded_from_file(event_bus, data_dir, device_manager_factory):
    state_file = data_dir / "consumption_state.json"
    state_file.write_text(
        json.dumps(
            {
                "ps_nas": {
                    "no_load_since": 12345.0,
                    "recycle_done": True,
                    "second_alert_sent": False,
                    "last_power": 0,
                    "last_seen": 0,
                }
            }
        )
    )
    devices_config = [
        {
            "id": "ps_nas",
            "name": "PS",
            "provider": "fake_power",
            "device_type": "switch",
            "host": "1.2.3.4",
            "config": {"monitor_consumption": True},
            "enabled": True,
            "test_mode": False,
        }
    ]
    dm = await device_manager_factory(devices_config)
    monitor = ConsumptionMonitor(event_bus, dm, AsyncMock(), state_file=state_file)
    assert monitor.state["ps_nas"]["recycle_done"] is True
    assert monitor.state["ps_nas"]["no_load_since"] == 12345.0


@pytest.mark.asyncio
async def test_get_last_power_helper(monitored_setup):
    dm, monitor, _ = monitored_setup
    _stub_power_source(monitor, "grid")
    _set_power(dm, "ps_nas", 27)
    await monitor._tick()
    assert monitor.get_last_power("ps_nas") == 27
    assert monitor.get_last_power("nonexistent") is None
