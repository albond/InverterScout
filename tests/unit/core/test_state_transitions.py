"""StateManager tests: grid/generator/battery transitions, first reading."""

from unittest.mock import patch

from tests.conftest import (
    generator_on_data,
    grid_ok_data,
    low_voltage_data,
    no_grid_data,
)

from inverterscout.core.state import StateManager


class TestFirstRead:
    async def test_first_read_sets_state_silently(self, state_mgr, event_collector):
        """First reading: silently sets the state, does not emit grid_lost."""
        data = no_grid_data()
        await state_mgr.process_data(data)

        event_types = [e.type for e in event_collector]
        assert "grid_lost" not in event_types
        assert "on_battery" not in event_types
        assert state_mgr._initialized is True
        assert state_mgr.grid.status == "grid_off"

    async def test_first_read_emits_data_updated(self, state_mgr, event_collector):
        data = grid_ok_data()
        await state_mgr.process_data(data)

        event_types = [e.type for e in event_collector]
        assert "data_updated" in event_types

    async def test_first_read_grid_ok(self, state_mgr):
        data = grid_ok_data()
        await state_mgr.process_data(data)
        assert state_mgr.grid.status == "grid_ok"

    async def test_first_read_generator_on(self, state_mgr):
        data = generator_on_data()
        await state_mgr.process_data(data)
        assert state_mgr.generator.status == "gen_on"


class TestGridTransitions:
    async def _init(self, state_mgr, data):
        """Initialization by first read."""
        await state_mgr.process_data(data)

    async def test_grid_ok_to_grid_off(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        await state_mgr.process_data(no_grid_data())

        types = [e.type for e in event_collector]
        assert "grid_lost" in types
        assert "on_battery" in types
        assert state_mgr.grid.status == "grid_off"

    async def test_grid_off_to_grid_ok(self, state_mgr, event_collector):
        await self._init(state_mgr, no_grid_data())
        await state_mgr.process_data(grid_ok_data())

        types = [e.type for e in event_collector]
        assert "grid_restored" in types
        assert "off_battery" in types

    async def test_grid_restored_has_outage_seconds(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        with patch("inverterscout.core.state.time.time", return_value=1000.0):
            await state_mgr.process_data(no_grid_data())
        with patch("inverterscout.core.state.time.time", return_value=1300.0):
            await state_mgr.process_data(grid_ok_data())

        restored_events = [e for e in event_collector if e.type == "grid_restored"]
        assert len(restored_events) == 1
        assert restored_events[0].data["outage_seconds"] == 300

    async def test_grid_ok_to_low_voltage(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        await state_mgr.process_data(low_voltage_data())

        types = [e.type for e in event_collector]
        assert "grid_low_voltage" in types
        assert state_mgr.grid.status == "grid_low_voltage"

    async def test_low_voltage_to_grid_ok(self, state_mgr, event_collector):
        await self._init(state_mgr, low_voltage_data())
        await state_mgr.process_data(grid_ok_data())

        types = [e.type for e in event_collector]
        assert "grid_voltage_normal" in types
        assert state_mgr.grid.status == "grid_ok"

    async def test_low_voltage_to_grid_off(self, state_mgr, event_collector):
        await self._init(state_mgr, low_voltage_data())
        await state_mgr.process_data(no_grid_data())

        types = [e.type for e in event_collector]
        assert "grid_lost" in types
        grid_lost_event = [e for e in event_collector if e.type == "grid_lost"][0]
        assert grid_lost_event.data["prev_status"] == "grid_low_voltage"

    async def test_grid_off_to_low_voltage(self, state_mgr, event_collector):
        """grid_off → grid_low_voltage = grid_restored."""
        await self._init(state_mgr, no_grid_data())
        await state_mgr.process_data(low_voltage_data())

        types = [e.type for e in event_collector]
        assert "grid_restored" in types

    async def test_no_change_no_event(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        event_collector.clear()
        await state_mgr.process_data(grid_ok_data())

        types = [e.type for e in event_collector]
        assert "grid_lost" not in types
        assert "grid_restored" not in types
        assert "data_updated" in types

    async def test_grid_voltage_updates(self, state_mgr):
        await self._init(state_mgr, grid_ok_data(grid_voltage=220.0))
        await state_mgr.process_data(grid_ok_data(grid_voltage=230.0))
        assert state_mgr.grid.voltage == 230.0


class TestGeneratorTransitions:
    async def _init(self, state_mgr, data):
        await state_mgr.process_data(data)

    async def test_gen_off_to_gen_on(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        await state_mgr.process_data(generator_on_data())

        types = [e.type for e in event_collector]
        assert "generator_started" in types
        assert state_mgr.generator.status == "gen_on"

    async def test_gen_on_to_gen_off(self, state_mgr, event_collector):
        await self._init(state_mgr, generator_on_data())
        await state_mgr.process_data(grid_ok_data(ac_input_type=0))

        types = [e.type for e in event_collector]
        assert "generator_stopped" in types
        assert state_mgr.generator.status == "gen_off"

    async def test_gen_stopped_has_run_seconds(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        with patch("inverterscout.core.state.time.time", return_value=1000.0):
            await state_mgr.process_data(generator_on_data())
        with patch("inverterscout.core.state.time.time", return_value=1600.0):
            await state_mgr.process_data(grid_ok_data(ac_input_type=0))

        stopped = [e for e in event_collector if e.type == "generator_stopped"]
        assert len(stopped) == 1
        assert stopped[0].data["run_seconds"] == 600

    async def test_gen_no_change_no_event(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        event_collector.clear()
        await state_mgr.process_data(grid_ok_data())

        types = [e.type for e in event_collector]
        assert "generator_started" not in types
        assert "generator_stopped" not in types


class TestBatteryTransitions:
    async def _init(self, state_mgr, data):
        await state_mgr.process_data(data)

    async def test_off_battery_to_on_battery(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        await state_mgr.process_data(no_grid_data())

        types = [e.type for e in event_collector]
        assert "on_battery" in types
        assert state_mgr.battery.on_battery is True

    async def test_on_battery_to_off_battery(self, state_mgr, event_collector):
        await self._init(state_mgr, no_grid_data())
        await state_mgr.process_data(grid_ok_data())

        types = [e.type for e in event_collector]
        assert "off_battery" in types
        assert state_mgr.battery.on_battery is False

    async def test_off_battery_event_has_power_source(self, state_mgr, event_collector):
        await self._init(state_mgr, no_grid_data())
        await state_mgr.process_data(grid_ok_data())

        off_bat = [e for e in event_collector if e.type == "off_battery"]
        assert len(off_bat) == 1
        assert off_bat[0].data["power_source"] == "grid"

    async def test_low_voltage_no_on_battery(self, state_mgr, event_collector):
        """low_voltage does not give on_battery."""
        await self._init(state_mgr, grid_ok_data())
        await state_mgr.process_data(low_voltage_data())

        types = [e.type for e in event_collector]
        assert "on_battery" not in types

    async def test_battery_state_updates(self, state_mgr):
        await self._init(state_mgr, grid_ok_data(soc=80, battery_charge=100))
        await state_mgr.process_data(grid_ok_data(soc=85, battery_charge=200))
        assert state_mgr.battery.soc == 85
        assert state_mgr.battery.charge_power == 200

    async def test_no_change_no_event(self, state_mgr, event_collector):
        await self._init(state_mgr, grid_ok_data())
        event_collector.clear()
        await state_mgr.process_data(grid_ok_data())

        types = [e.type for e in event_collector]
        assert "on_battery" not in types
        assert "off_battery" not in types


class TestSaveLoadState:
    async def test_save_state_creates_file(self, state_mgr, data_dir):
        await state_mgr.process_data(grid_ok_data())
        assert state_mgr.STATE_FILE.exists()

    async def test_get_status_request(self, event_bus, state_mgr):
        await state_mgr.process_data(grid_ok_data(soc=75))
        result = await event_bus.request("get_status")
        assert result["battery"].soc == 75
        assert result["last_data"] is not None

    async def test_pre_gen_baseline_persisted(self, state_mgr, event_bus, data_dir):
        """pre_gen_soc/pre_gen_time are saved and restored."""
        # Initialize (grid_ok)
        await state_mgr.process_data(grid_ok_data(soc=100))
        # The grid is gone → no_grid
        await state_mgr.process_data(no_grid_data(soc=99))
        # After a while - another no_grid (generator off)
        await state_mgr.process_data(no_grid_data(soc=85))
        assert state_mgr.grid.pre_gen_soc == 85
        assert state_mgr.grid.pre_gen_time > 0
        saved_time = state_mgr.grid.pre_gen_time

        # Loading into a new manager
        new_mgr = StateManager(event_bus)
        new_mgr.STATE_FILE = state_mgr.STATE_FILE
        new_mgr.load_state()
        assert new_mgr.grid.pre_gen_soc == 85
        assert new_mgr.grid.pre_gen_time == saved_time


class TestPreGenBaseline:
    """Baseline for forecast when the generator is running."""

    async def test_baseline_updated_on_no_grid_gen_off(self, state_mgr):
        await state_mgr.process_data(grid_ok_data(soc=100))
        await state_mgr.process_data(no_grid_data(soc=99))
        await state_mgr.process_data(no_grid_data(soc=80))
        assert state_mgr.grid.pre_gen_soc == 80
        assert state_mgr.grid.pre_gen_time == state_mgr.last_data_time

    async def test_baseline_frozen_when_generator_starts(self, state_mgr):
        """When the generator is running, baseline is not updated."""
        await state_mgr.process_data(grid_ok_data(soc=100))
        await state_mgr.process_data(no_grid_data(soc=80))
        frozen_soc = state_mgr.grid.pre_gen_soc
        frozen_time = state_mgr.grid.pre_gen_time
        # The generator is turned on, soc changes (charges) - baseline should remain
        await state_mgr.process_data(
            no_grid_data(
                soc=85,
                ac_input_type=1,
                gen_voltage=224.0,
                gen_power=2000,
            )
        )
        assert state_mgr.grid.pre_gen_soc == frozen_soc
        assert state_mgr.grid.pre_gen_time == frozen_time

    async def test_baseline_resumes_after_generator_stops(self, state_mgr):
        await state_mgr.process_data(grid_ok_data(soc=100))
        await state_mgr.process_data(no_grid_data(soc=80))
        # The generator turned on
        await state_mgr.process_data(
            no_grid_data(
                soc=85,
                ac_input_type=1,
                gen_voltage=224.0,
                gen_power=2000,
            )
        )
        frozen_time = state_mgr.grid.pre_gen_time
        # Generator turned off, no_grid continues → baseline is updated again
        await state_mgr.process_data(no_grid_data(soc=82))
        assert state_mgr.grid.pre_gen_soc == 82
        assert state_mgr.grid.pre_gen_time > frozen_time

    async def test_baseline_reset_on_grid_restored(self, state_mgr):
        await state_mgr.process_data(grid_ok_data(soc=100))
        await state_mgr.process_data(no_grid_data(soc=80))
        assert state_mgr.grid.pre_gen_soc == 80
        # Grid is back → baseline is reset
        await state_mgr.process_data(grid_ok_data(soc=80))
        assert state_mgr.grid.pre_gen_soc == 0
        assert state_mgr.grid.pre_gen_time == 0
