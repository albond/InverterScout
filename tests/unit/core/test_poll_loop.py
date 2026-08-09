"""poll_loop tests: first reading, debounce, backoff.

Strategy: side_effect on sleep to control the number of iterations,
side_effect on read_inverter for data sequence.
The tests wait for the task to complete (not yield loops) so as not to depend on the sleep mock."""

import asyncio
from unittest.mock import patch

from tests.conftest import grid_ok_data, no_grid_data

from inverterscout.core.state import poll_loop
from inverterscout.inverter.luxpower import InverterData


class TestPollLoopBasic:
    async def test_first_read_initializes(self, state_mgr, event_collector):
        """First read in poll_loop → _initialized."""
        reads = [grid_ok_data()]
        read_iter = iter(reads)

        async def mock_read(host, port, timeout=30):
            try:
                return next(read_iter)
            except StopIteration:
                raise asyncio.CancelledError()

        sleep_count = [0]

        async def mock_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] > 2:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 10))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert state_mgr._initialized is True
        types = [e.type for e in event_collector]
        assert "data_updated" in types


class TestDebounce:
    async def test_debounce_confirmed(self, state_mgr, event_collector):
        """First reading=grid, second=no_grid, confirm=no_grid → grid_lost."""
        reads = [
            grid_ok_data(),  # init
            no_grid_data(),  # shift detected
            no_grid_data(),  # confirmation
        ]
        read_iter = iter(reads)

        async def mock_read(host, port, timeout=30):
            try:
                return next(read_iter)
            except StopIteration:
                raise asyncio.CancelledError()

        sleep_count = [0]

        async def mock_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] > 10:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 10))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        types = [e.type for e in event_collector]
        assert "grid_lost" in types

    async def test_debounce_not_confirmed(self, state_mgr, event_collector):
        """Bounce: first=no_grid, confirm=grid_ok → no grid_lost."""
        reads = [
            grid_ok_data(),  # init
            no_grid_data(),  # shift detected
            grid_ok_data(),  # confirmation - returned back
        ]
        read_iter = iter(reads)

        async def mock_read(host, port, timeout=30):
            try:
                return next(read_iter)
            except StopIteration:
                raise asyncio.CancelledError()

        sleep_count = [0]

        async def mock_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] > 10:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 10))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        types = [e.type for e in event_collector]
        assert "grid_lost" not in types

    async def test_all_confirms_invalid_ignored(self, state_mgr, event_collector):
        """All 3 confirmations are invalid → the change is ignored (for-else)."""
        reads = [
            grid_ok_data(),  # init
            no_grid_data(),  # shift detected
            None,  # confirm 1
            None,  # confirm 2
            None,  # confirm 3
        ]
        read_iter = iter(reads)

        async def mock_read(host, port, timeout=30):
            try:
                return next(read_iter)
            except StopIteration:
                raise asyncio.CancelledError()

        sleep_count = [0]

        async def mock_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] > 15:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 10))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        types = [e.type for e in event_collector]
        assert "grid_lost" not in types


class TestErrorBackoff:
    async def test_backoff_after_errors(self, state_mgr):
        """4+ errors → sleep increases to min(interval*2, 300)."""
        sleep_values = []

        async def mock_read(host, port, timeout=30):
            return None  # error

        async def mock_sleep(seconds):
            sleep_values.append(seconds)
            if len(sleep_values) >= 6:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 100))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # First 3 errors: interval=100, then backoff=200
        assert sleep_values[0] == 100  # 1st mistake
        assert sleep_values[1] == 100  # 2nd mistake
        assert sleep_values[2] == 100  # 3rd mistake
        # 4th error: consecutive_errors=4 > 3 → backoff
        assert sleep_values[3] == 200

    async def test_backoff_capped_at_300(self, state_mgr):
        """Backoff does not exceed 300."""
        sleep_values = []

        async def mock_read(host, port, timeout=30):
            return None

        async def mock_sleep(seconds):
            sleep_values.append(seconds)
            if len(sleep_values) >= 6:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 200))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # interval=200, backoff=min(400, 300)=300
        if len(sleep_values) > 3:
            assert sleep_values[3] == 300

    async def test_invalid_data_counts_as_error(self, state_mgr):
        """Invalid data (battery_voltage=0, soc=0) is considered an error."""
        call_count = [0]

        async def mock_read(host, port, timeout=30):
            call_count[0] += 1
            return InverterData(battery_voltage=0.0, soc=0)

        sleep_count = [0]

        async def mock_sleep(seconds):
            sleep_count[0] += 1
            if sleep_count[0] >= 3:
                raise asyncio.CancelledError()

        with patch("inverterscout.core.state.read_inverter", side_effect=mock_read):
            with patch("inverterscout.core.state.asyncio.sleep", side_effect=mock_sleep):
                task = asyncio.create_task(poll_loop(state_mgr, "127.0.0.1", 8000, 10))
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert state_mgr.error_count >= 1
