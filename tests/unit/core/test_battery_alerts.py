"""Battery alert tests: battery_low, battery_critical, reset flags."""

from tests.conftest import grid_ok_data, no_grid_data


class TestBatteryAlerts:
    async def _init_on_battery(self, state_mgr, soc=80):
        """Initialization: grid_ok, then switch to battery."""
        await state_mgr.process_data(grid_ok_data(soc=soc))
        await state_mgr.process_data(no_grid_data(soc=soc))

    async def test_battery_low_at_30(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=30))

        types = [e.type for e in event_collector]
        assert "battery_low" in types

    async def test_battery_low_at_25(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=25))

        types = [e.type for e in event_collector]
        assert "battery_low" in types

    async def test_battery_low_not_repeated(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=25))
        await state_mgr.process_data(no_grid_data(soc=22))

        low_events = [e for e in event_collector if e.type == "battery_low"]
        assert len(low_events) == 1

    async def test_battery_critical_at_15(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=15))

        types = [e.type for e in event_collector]
        assert "battery_critical" in types

    async def test_battery_critical_at_10(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=10))

        types = [e.type for e in event_collector]
        assert "battery_critical" in types

    async def test_critical_not_repeated(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=10))
        await state_mgr.process_data(no_grid_data(soc=5))

        critical_events = [e for e in event_collector if e.type == "battery_critical"]
        assert len(critical_events) == 1

    async def test_skip_battery_low_when_direct_to_critical(self, state_mgr, event_collector):
        """soc 80 → 10 directly: battery_critical only (elif skips battery_low)."""
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=10))

        types = [e.type for e in event_collector]
        assert "battery_critical" in types
        assert "battery_low" not in types

    async def test_no_alerts_when_not_on_battery(self, state_mgr, event_collector):
        """On the network there are no alerts even with low SOC."""
        await state_mgr.process_data(grid_ok_data(soc=80))
        await state_mgr.process_data(grid_ok_data(soc=10))

        types = [e.type for e in event_collector]
        assert "battery_low" not in types
        assert "battery_critical" not in types

    async def test_alerts_reset_on_grid_restored(self, state_mgr, event_collector):
        """Grid restoration resets threshold flags for the next outage."""
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=25))
        low_count_1 = len([e for e in event_collector if e.type == "battery_low"])
        assert low_count_1 == 1

        # Restore grid power.
        await state_mgr.process_data(grid_ok_data(soc=60))
        # Back to battery
        await state_mgr.process_data(no_grid_data(soc=25))

        low_count_2 = len([e for e in event_collector if e.type == "battery_low"])
        assert low_count_2 == 2  # the second alert worked

    async def test_battery_low_has_grid_lost_time(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=25))

        low_events = [e for e in event_collector if e.type == "battery_low"]
        assert len(low_events) == 1
        assert "grid_lost_time" in low_events[0].data

    async def test_battery_critical_has_soc_and_voltage(self, state_mgr, event_collector):
        await self._init_on_battery(state_mgr, soc=80)
        await state_mgr.process_data(no_grid_data(soc=10, battery_voltage=46.0))

        critical = [e for e in event_collector if e.type == "battery_critical"]
        assert len(critical) == 1
        assert critical[0].data["soc"] == 10
        assert critical[0].data["battery_voltage"] == 46.0
