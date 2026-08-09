"""End-to-end: InverterData → event → script → device.

Full integration: EventBus + StateManager + DeviceManager(stub) + ScenarioEngine."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from tests.conftest import grid_ok_data, no_grid_data

BOILER_DEVICE = {
    "id": "boiler",
    "name": "Boiler",
    "provider": "stub",
    "config": {},
    "enabled": True,
}

SCENARIO_GRID_LOSS = {
    "id": "grid_loss_boiler",
    "name": "Turn off the boiler when grid power is lost",
    "trigger_event": "on_battery",
    "actions": [{"device_id": "boiler", "action": "turn_off"}],
    "priority": 10,
    "enabled": True,
}

SCENARIO_GRID_RESTORE = {
    "id": "grid_restore_boiler",
    "name": "Turn on the boiler when grid power is restored",
    "trigger_event": "off_battery",
    "actions": [{"device_id": "boiler", "action": "turn_on"}],
    "priority": 10,
    "enabled": True,
}

SCENARIO_TIMER = {
    "id": "timer_boiler",
    "name": "Boiler with timer",
    "trigger_event": "on_battery",
    "actions": [{"device_id": "boiler", "action": "turn_off"}],
    "revert_after_seconds": 30,
    "revert_actions": [{"device_id": "boiler", "action": "turn_on"}],
    "cancel_event": "off_battery",
    "priority": 10,
    "enabled": True,
}


@pytest.fixture
def dm_sleep_mock():
    with patch("inverterscout.devices.manager.asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def se_sleep_mock():
    with patch("inverterscout.core.scenarios.asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


class TestGridLossChain:
    async def test_grid_loss_triggers_scenario_turns_off_device(
        self,
        event_bus,
        state_mgr,
        device_manager_factory,
        scenario_engine_factory,
        event_collector,
        dm_sleep_mock,
        se_sleep_mock,
    ):
        """Grid loss → on_battery → script → turn_off boiler → device_command_ok."""
        dm = await device_manager_factory([BOILER_DEVICE])
        scenario_engine_factory([SCENARIO_GRID_LOSS])

        # Turn on the boiler first
        driver = dm.get_driver("boiler")
        await driver.turn_on()

        # First reading - grid ok
        await state_mgr.process_data(grid_ok_data())
        # Second - grid off
        await state_mgr.process_data(no_grid_data())

        types = [e.type for e in event_collector]
        assert "grid_lost" in types
        assert "on_battery" in types
        assert "device_command" in types

        # Verify the dispatched command payload.
        cmd = [e for e in event_collector if e.type == "device_command"][0]
        assert cmd.data["device_id"] == "boiler"
        assert cmd.data["action"] == "turn_off"
        assert cmd.data["source"] == "scenario"


class TestGridRestoreChain:
    async def test_grid_restored_turns_on_device(
        self,
        event_bus,
        state_mgr,
        device_manager_factory,
        scenario_engine_factory,
        event_collector,
        dm_sleep_mock,
        se_sleep_mock,
    ):
        dm = await device_manager_factory([BOILER_DEVICE])
        scenario_engine_factory([SCENARIO_GRID_LOSS, SCENARIO_GRID_RESTORE])

        # Turn on the boiler
        driver = dm.get_driver("boiler")
        await driver.turn_on()

        # grid ok → grid off → grid ok
        await state_mgr.process_data(grid_ok_data())
        await state_mgr.process_data(no_grid_data())
        await asyncio.sleep(0.1)
        await state_mgr.process_data(grid_ok_data())
        await asyncio.sleep(0.1)

        types = [e.type for e in event_collector]
        assert "grid_restored" in types
        assert "off_battery" in types

        # There must be both turn_off and turn_on
        cmds = [e for e in event_collector if e.type == "device_command"]
        actions = [c.data["action"] for c in cmds]
        assert "turn_off" in actions
        assert "turn_on" in actions


class TestTimerRevertChain:
    async def test_timer_reverts_after_expiry(
        self,
        event_bus,
        state_mgr,
        device_manager_factory,
        scenario_engine_factory,
        event_collector,
        dm_sleep_mock,
        se_sleep_mock,
    ):
        dm = await device_manager_factory([BOILER_DEVICE])
        se = scenario_engine_factory([SCENARIO_TIMER])

        # Turn on the boiler
        driver = dm.get_driver("boiler")
        await driver.turn_on()

        # grid ok → grid off
        await state_mgr.process_data(grid_ok_data())
        await state_mgr.process_data(no_grid_data())
        await asyncio.sleep(0)

        # Wait for the timer worker while its delay is mocked.
        tasks = list(se._timer_tasks.values())
        for t in tasks:
            await t

        types = [e.type for e in event_collector]
        assert "scenario_timer_fired" in types

        # turn_off + turn_on (revert)
        cmds = [e for e in event_collector if e.type == "device_command"]
        actions = [c.data["action"] for c in cmds]
        assert "turn_off" in actions
        assert "turn_on" in actions


class TestCancelTimerChain:
    async def test_cancel_timer_on_grid_restored(
        self,
        event_bus,
        state_mgr,
        device_manager_factory,
        scenario_engine_factory,
        event_collector,
        dm_sleep_mock,
        se_sleep_mock,
    ):
        dm = await device_manager_factory([BOILER_DEVICE])
        se = scenario_engine_factory([SCENARIO_TIMER])

        driver = dm.get_driver("boiler")
        await driver.turn_on()

        await state_mgr.process_data(grid_ok_data())
        await state_mgr.process_data(no_grid_data())
        await asyncio.sleep(0)

        assert "timer_boiler" in se._timer_tasks

        # Restore grid power before the timer expires.
        await state_mgr.process_data(grid_ok_data())
        await asyncio.sleep(0.1)

        # Timer canceled
        assert "timer_boiler" not in se._timer_tasks

        # revert_actions completed (from cancel)
        cmds = [e for e in event_collector if e.type == "device_command"]
        reverts = [c for c in cmds if c.data["action"] == "turn_on"]
        assert len(reverts) >= 1


class TestBatteryAlertsChain:
    async def test_battery_low_then_critical(self, event_bus, state_mgr, event_collector):
        """soc 80 → 25 → 10: battery_low → battery_critical."""
        await state_mgr.process_data(grid_ok_data(soc=80))
        await state_mgr.process_data(no_grid_data(soc=80))
        await state_mgr.process_data(no_grid_data(soc=25))
        await state_mgr.process_data(no_grid_data(soc=10))

        types = [e.type for e in event_collector]
        assert "battery_low" in types
        assert "battery_critical" in types

        # battery_low before battery_critical
        low_idx = types.index("battery_low")
        crit_idx = types.index("battery_critical")
        assert low_idx < crit_idx
