"""ScenarioEngine tests: trigger, cancel, timer, persistence, disabled."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from inverterscout.core.state import Event

SCENARIO_BASIC = {
    "id": "s1",
    "name": "Turn off the boiler when grid power is lost",
    "trigger_event": "on_battery",
    "actions": [{"device_id": "boiler", "action": "turn_off"}],
    "priority": 10,
    "enabled": True,
}

SCENARIO_WITH_TIMER = {
    "id": "s2",
    "name": "Boiler with timer",
    "trigger_event": "on_battery",
    "actions": [{"device_id": "boiler", "action": "turn_off"}],
    "revert_after_seconds": 60,
    "revert_actions": [{"device_id": "boiler", "action": "turn_on"}],
    "cancel_event": "off_battery",
    "priority": 10,
    "enabled": True,
}

SCENARIO_DISABLED = {
    "id": "s3",
    "name": "Disabled script",
    "trigger_event": "on_battery",
    "actions": [{"device_id": "boiler", "action": "turn_off"}],
    "priority": 5,
    "enabled": False,
}


@pytest.fixture
def sleep_mock():
    with patch("inverterscout.core.scenarios.asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


class TestTrigger:
    async def test_trigger_dispatches_command(
        self, event_bus, scenario_engine_factory, event_collector
    ):
        scenario_engine_factory([SCENARIO_BASIC])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))

        types = [e.type for e in event_collector]
        assert "device_command" in types

        cmd = [e for e in event_collector if e.type == "device_command"][0]
        assert cmd.data["device_id"] == "boiler"
        assert cmd.data["action"] == "turn_off"
        assert cmd.data["source"] == "scenario"

    async def test_disabled_scenario_not_triggered(
        self, event_bus, scenario_engine_factory, event_collector
    ):
        scenario_engine_factory([SCENARIO_DISABLED])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))

        types = [e.type for e in event_collector]
        assert "device_command" not in types

    async def test_priority_order(self, event_bus, scenario_engine_factory, event_collector):
        """Scripts are executed in descending order of priority."""
        s_high = {
            "id": "high",
            "name": "High",
            "trigger_event": "on_battery",
            "actions": [{"device_id": "dev_high", "action": "turn_off"}],
            "priority": 100,
            "enabled": True,
        }
        s_low = {
            "id": "low",
            "name": "Low",
            "trigger_event": "on_battery",
            "actions": [{"device_id": "dev_low", "action": "turn_off"}],
            "priority": 1,
            "enabled": True,
        }
        scenario_engine_factory([s_low, s_high])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))

        cmds = [e for e in event_collector if e.type == "device_command"]
        assert len(cmds) == 2
        assert cmds[0].data["device_id"] == "dev_high"
        assert cmds[1].data["device_id"] == "dev_low"

    async def test_multiple_actions_in_scenario(
        self, event_bus, scenario_engine_factory, event_collector
    ):
        scenario = {
            "id": "multi",
            "name": "Multi",
            "trigger_event": "on_battery",
            "actions": [
                {"device_id": "dev1", "action": "turn_off"},
                {"device_id": "dev2", "action": "turn_off"},
            ],
            "priority": 10,
            "enabled": True,
        }
        scenario_engine_factory([scenario])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))

        cmds = [e for e in event_collector if e.type == "device_command"]
        assert len(cmds) == 2


class TestCancelEvent:
    async def test_cancel_event_reverts(
        self, event_bus, scenario_engine_factory, event_collector, sleep_mock
    ):
        se = scenario_engine_factory([SCENARIO_WITH_TIMER])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))
        # Allow the timer task to start.
        await asyncio.sleep(0)

        assert "s2" in se._timer_tasks

        # Cancel event
        await event_bus.emit(Event(type="off_battery", timestamp=time.time()))

        # Timer canceled
        assert "s2" not in se._timer_tasks

        # revert_actions completed
        cmds = [e for e in event_collector if e.type == "device_command"]
        revert_cmds = [c for c in cmds if c.data["action"] == "turn_on"]
        assert len(revert_cmds) >= 1

    async def test_cancel_without_active_timer(
        self, event_bus, scenario_engine_factory, event_collector
    ):
        """Cancel event without an active timer - nothing happens."""
        scenario_engine_factory([SCENARIO_WITH_TIMER])
        await event_bus.emit(Event(type="off_battery", timestamp=time.time()))

        cmds = [e for e in event_collector if e.type == "device_command"]
        assert len(cmds) == 0


class TestTimerExpiry:
    async def test_timer_fires_revert(
        self, event_bus, scenario_engine_factory, event_collector, sleep_mock
    ):
        se = scenario_engine_factory([SCENARIO_WITH_TIMER])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))

        # Waiting for timer_worker to complete (sleep locked)
        await asyncio.sleep(0)
        # Wait for every timer task to finish.
        tasks = list(se._timer_tasks.values())
        for t in tasks:
            await t

        types = [e.type for e in event_collector]
        assert "scenario_timer_fired" in types

        # revert command sent
        cmds = [e for e in event_collector if e.type == "device_command"]
        revert = [c for c in cmds if c.data["action"] == "turn_on"]
        assert len(revert) >= 1


class TestTimerPersistence:
    async def test_save_and_restore_timer(
        self, event_bus, scenario_engine_factory, data_dir, sleep_mock
    ):
        se = scenario_engine_factory([SCENARIO_WITH_TIMER])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))
        await asyncio.sleep(0)

        # Timer saved
        timers_file = data_dir / "device_state.json"
        assert timers_file.exists()

        saved = json.loads(timers_file.read_text())
        assert "s2" in saved

        # Cancel current timers
        for task in se._timer_tasks.values():
            task.cancel()
        await asyncio.sleep(0)

        # Rebuilding the engine - timers are restored
        # Store a future firing time.
        saved["s2"]["fires_at"] = time.time() + 30
        timers_file.write_text(json.dumps(saved))

        se2 = scenario_engine_factory([SCENARIO_WITH_TIMER])
        assert "s2" in se2._timer_tasks

    async def test_expired_timer_reverts_immediately(
        self, event_bus, scenario_engine_factory, data_dir, event_collector
    ):
        """The timer expired while the bot was turned off → revert immediately."""
        timers_file = data_dir / "device_state.json"
        timers_file.write_text(
            json.dumps({"s2": {"started_at": time.time() - 120, "fires_at": time.time() - 60}})
        )

        scenario_engine_factory([SCENARIO_WITH_TIMER])
        # _execute_expired_revert launched via create_task - waiting for all pending tasks
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.wait(pending, timeout=2.0)

        types = [e.type for e in event_collector]
        assert "scenario_timer_fired" in types

        fired = [e for e in event_collector if e.type == "scenario_timer_fired"]
        assert fired[0].data.get("expired") is True


class TestListScenarios:
    def test_list_scenarios(self, event_bus, scenario_engine_factory):
        se = scenario_engine_factory([SCENARIO_BASIC, SCENARIO_WITH_TIMER])
        result = se.list_scenarios()
        assert len(result) == 2
        ids = [r["id"] for r in result]
        assert "s1" in ids
        assert "s2" in ids


SCENARIO_REVERT_ONLY = {
    "id": "s4",
    "name": "Boiler rollback",
    "trigger_event": "off_battery",
    "actions": [{"device_id": "other_dev", "action": "turn_on"}],
    "revert_after_seconds": 30,
    "revert_actions": [{"device_id": "boiler", "action": "turn_off"}],
    "priority": 5,
    "enabled": True,
}


class TestGetScenariosForDevice:
    def test_found(self, event_bus, scenario_engine_factory):
        se = scenario_engine_factory([SCENARIO_BASIC, SCENARIO_DISABLED])
        result = se.get_scenarios_for_device("boiler")
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "Turn off the boiler when grid power is lost" in names

    def test_empty(self, event_bus, scenario_engine_factory):
        se = scenario_engine_factory([SCENARIO_BASIC])
        result = se.get_scenarios_for_device("nonexistent")
        assert result == []

    def test_revert_actions(self, event_bus, scenario_engine_factory):
        se = scenario_engine_factory([SCENARIO_REVERT_ONLY])
        result = se.get_scenarios_for_device("boiler")
        assert len(result) == 1
        assert result[0]["name"] == "Boiler rollback"

    async def test_timer(self, event_bus, scenario_engine_factory, sleep_mock):
        se = scenario_engine_factory([SCENARIO_WITH_TIMER])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))
        await asyncio.sleep(0)
        result = se.get_scenarios_for_device("boiler")
        assert len(result) == 1
        assert "timer_remaining" in result[0]
        assert result[0]["timer_remaining"] > 0


class TestAddScenarios:
    async def test_add_scenarios_creates_rules(
        self, event_bus, scenario_engine_factory, event_collector
    ):
        """add_scenarios adds rules and subscribes to events."""
        se = scenario_engine_factory([])  # empty
        new_scenarios = [
            {
                "id": "new1",
                "name": "New scenario",
                "trigger_event": "on_battery",
                "actions": [{"device_id": "dev1", "action": "turn_off"}],
                "priority": 10,
                "enabled": True,
            },
        ]
        count = se.add_scenarios(new_scenarios)
        assert count == 1
        assert len(se.rules) == 1
        assert se.rules[0].id == "new1"
        # The subscribed event must fire.
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))
        cmds = [e for e in event_collector if e.type == "device_command"]
        assert len(cmds) == 1
        assert cmds[0].data["device_id"] == "dev1"

    def test_add_scenarios_saves_to_file(self, event_bus, scenario_engine_factory, data_dir):
        """add_scenarios saves scenarios to a file."""
        se = scenario_engine_factory([SCENARIO_BASIC])
        new_scenarios = [
            {
                "id": "new2",
                "name": "Another script",
                "trigger_event": "off_battery",
                "actions": [{"device_id": "dev2", "action": "turn_on"}],
                "priority": 5,
                "enabled": True,
            },
        ]
        se.add_scenarios(new_scenarios)
        saved = json.loads((data_dir / "scenarios.json").read_text())
        ids = [s["id"] for s in saved]
        assert "s1" in ids
        assert "new2" in ids


class TestRemoveScenariosForDevice:
    def test_remove_deletes_rules(self, event_bus, scenario_engine_factory, data_dir):
        """remove_scenarios_for_device removes rules and saves them to disk."""
        se = scenario_engine_factory([SCENARIO_BASIC, SCENARIO_WITH_TIMER])
        assert len(se.rules) == 2
        count = se.remove_scenarios_for_device("boiler")
        assert count == 2
        assert len(se.rules) == 0
        saved = json.loads((data_dir / "scenarios.json").read_text())
        assert len(saved) == 0

    def test_remove_only_matching_device(self, event_bus, scenario_engine_factory):
        """Deletes only the scripts of the specified device, others remain."""
        other_scenario = {
            "id": "other",
            "name": "Another scenario",
            "trigger_event": "on_battery",
            "actions": [{"device_id": "other_dev", "action": "turn_off"}],
            "priority": 5,
            "enabled": True,
        }
        se = scenario_engine_factory([SCENARIO_BASIC, other_scenario])
        count = se.remove_scenarios_for_device("boiler")
        assert count == 1
        assert len(se.rules) == 1
        assert se.rules[0].id == "other"

    def test_remove_nonexistent_device(self, event_bus, scenario_engine_factory):
        """Non-existent device → 0, no changes."""
        se = scenario_engine_factory([SCENARIO_BASIC])
        count = se.remove_scenarios_for_device("nonexistent")
        assert count == 0
        assert len(se.rules) == 1

    async def test_remove_cancels_active_timer(
        self, event_bus, scenario_engine_factory, event_collector, sleep_mock
    ):
        """Removing cancels the timer WITHOUT executing revert_actions."""
        se = scenario_engine_factory([SCENARIO_WITH_TIMER])
        await event_bus.emit(Event(type="on_battery", timestamp=time.time()))
        await asyncio.sleep(0)
        assert "s2" in se._timer_tasks

        count = se.remove_scenarios_for_device("boiler")
        assert count == 1
        assert "s2" not in se._timer_tasks
        assert "s2" not in se._timer_meta
        # revert is NOT executed - no turn_on after deletion
        cmds_after = [
            e
            for e in event_collector
            if e.type == "device_command" and e.data["action"] == "turn_on"
        ]
        assert len(cmds_after) == 0

    def test_remove_via_revert_actions(self, event_bus, scenario_engine_factory):
        """Finds a scenario where the device is only in revert_actions."""
        se = scenario_engine_factory([SCENARIO_REVERT_ONLY])
        count = se.remove_scenarios_for_device("boiler")
        assert count == 1
        assert len(se.rules) == 0


class TestSetEnabled:
    def test_enable_disable(self, event_bus, scenario_engine_factory):
        se = scenario_engine_factory([SCENARIO_BASIC])
        se.set_enabled("s1", False)
        rule = se.get_rule("s1")
        assert rule.enabled is False

        se.set_enabled("s1", True)
        rule = se.get_rule("s1")
        assert rule.enabled is True

    def test_set_enabled_nonexistent(self, event_bus, scenario_engine_factory):
        se = scenario_engine_factory([SCENARIO_BASIC])
        result = se.set_enabled("nonexistent", False)
        assert result is False


class TestReloadFromFile:
    def test_reload_clears_and_reloads(self, event_bus, scenario_engine_factory, data_dir):
        """reload_from_file rereads scenarios.json."""
        se = scenario_engine_factory([SCENARIO_BASIC])
        assert len(se.rules) == 1

        # Updating the file with two scripts
        new_scenarios = [SCENARIO_BASIC, SCENARIO_WITH_TIMER]
        (data_dir / "scenarios.json").write_text(json.dumps(new_scenarios, ensure_ascii=False))

        count = se.reload_from_file()
        assert count == 2
        assert len(se.rules) == 2

    def test_reload_returns_zero_on_empty_file(self, event_bus, scenario_engine_factory, data_dir):
        """Empty file → 0 rules."""
        se = scenario_engine_factory([SCENARIO_BASIC])
        (data_dir / "scenarios.json").write_text(json.dumps([]))

        count = se.reload_from_file()
        assert count == 0
        assert len(se.rules) == 0
