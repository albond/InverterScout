"""Formatter tests: format_duration, ts_to_date, ts_to_time, format_time_human."""

from datetime import datetime
from unittest.mock import patch

from inverterscout.settings.runtime import (
    estimate_battery_runtime,
    format_duration,
    format_time_human,
    ts_to_date,
    ts_to_time,
)


class TestFormatDuration:
    def test_less_than_minute(self):
        assert format_duration(0) == "less than a minute"
        assert format_duration(30) == "less than a minute"
        assert format_duration(59) == "less than a minute"

    def test_exactly_one_minute(self):
        assert format_duration(60) == "1 min"

    def test_minutes_only(self):
        assert format_duration(120) == "2 min"
        assert format_duration(45 * 60) == "45 min"

    def test_exactly_one_hour(self):
        assert format_duration(3600) == "1 h"

    def test_hours_only(self):
        assert format_duration(7200) == "2 h"

    def test_hours_and_minutes(self):
        assert format_duration(3660) == "1 h 1 min"
        assert format_duration(7260) == "2 h 1 min"
        assert format_duration(3600 + 15 * 60) == "1 h 15 min"

    def test_large_duration(self):
        # 24 hours
        assert format_duration(86400) == "24 h"
        # 25 hours 30 minutes
        assert format_duration(25 * 3600 + 30 * 60) == "25 h 30 min"


class TestTsToDate:
    def test_zero_returns_empty(self):
        assert ts_to_date(0) == ""

    def test_negative_returns_empty(self):
        assert ts_to_date(-1) == ""

    def test_valid_timestamp(self):
        # 1 Jan 2024 00:00:00 UTC
        ts = datetime(2024, 1, 15, 12, 0, 0).timestamp()
        result = ts_to_date(ts)
        assert result == "2024-01-15"


class TestTsToTime:
    def test_zero_returns_empty(self):
        assert ts_to_time(0) == ""

    def test_negative_returns_empty(self):
        assert ts_to_time(-1) == ""

    def test_valid_timestamp(self):
        ts = datetime(2024, 1, 15, 18, 30, 45).timestamp()
        result = ts_to_time(ts)
        assert result == "18:30:45"


class TestFormatTimeHuman:
    def _mock_now(self, year=2024, month=6, day=15, hour=18, minute=30):
        return datetime(year, month, day, hour, minute, 0)

    def test_today(self):
        now = self._mock_now(hour=18, minute=30)
        event_ts = datetime(2024, 6, 15, 15, 15, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "today at 15:15" in result
        assert "3 h 15 min ago" in result

    def test_yesterday(self):
        now = self._mock_now(hour=10, minute=0)
        event_ts = datetime(2024, 6, 14, 22, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "yesterday at 22:00" in result

    def test_day_before_yesterday(self):
        now = self._mock_now(hour=10, minute=0)
        event_ts = datetime(2024, 6, 13, 8, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "2024-06-13 08:00" in result

    def test_older_date(self):
        now = self._mock_now(hour=10, minute=0)
        event_ts = datetime(2024, 6, 10, 14, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "2024-06-10 14:00" in result

    def test_just_now(self):
        now = self._mock_now(hour=18, minute=30)
        event_ts = datetime(2024, 6, 15, 18, 30, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "just now" in result

    def test_just_now_under_30_sec(self):
        """20 seconds ago - still 'just now'."""
        now = datetime(2024, 6, 15, 18, 30, 20)
        event_ts = datetime(2024, 6, 15, 18, 30, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "just now" in result

    def test_less_than_minute_30_to_59_sec(self):
        """30-59 sec - 'less than a minute ago'."""
        for sec in (30, 45, 59):
            now = datetime(2024, 6, 15, 18, 30, sec)
            event_ts = datetime(2024, 6, 15, 18, 30, 0).timestamp()
            with patch("inverterscout.settings.runtime.datetime") as mock_dt:
                mock_dt.now.return_value = now
                mock_dt.fromtimestamp = datetime.fromtimestamp
                result = format_time_human(event_ts)
            assert "less than a minute ago" in result, f"sec={sec}: {result}"

    def test_one_minute_at_60_sec(self):
        """60 sec - '1 minute ago'."""
        now = datetime(2024, 6, 15, 18, 31, 0)
        event_ts = datetime(2024, 6, 15, 18, 30, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "1 min ago" in result

    def test_minutes_ago(self):
        now = self._mock_now(hour=18, minute=30)
        event_ts = datetime(2024, 6, 15, 18, 10, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "20 min ago" in result

    def test_hours_ago_no_minutes(self):
        now = self._mock_now(hour=18, minute=0)
        event_ts = datetime(2024, 6, 15, 16, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = format_time_human(event_ts)
        assert "2 h 0 min ago" in result


class TestEstimateBatteryRuntime:
    """Tests estimate_battery_runtime - estimate the remaining battery life."""

    def _mock_now(self, year=2024, month=6, day=15, hour=18, minute=0):
        return datetime(year, month, day, hour, minute, 0)

    def test_normal_discharge(self):
        """SOC=70%, discharged 30% in 2 hours → 60% to 10% → another 4 hours."""
        now = self._mock_now(hour=20, minute=0)
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(70, grid_lost)
        assert result is not None
        assert result["remaining_sec"] == 14400  # 4 hours
        assert "4 h" in result["remaining_text"]
        assert "at 00:00" in result["depletion_time_text"]

    def test_slow_discharge(self):
        """SOC=50%, discharged 50% in 5 hours → 40% to 10% → another 4 hours."""
        now = self._mock_now(hour=23, minute=0)
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(50, grid_lost)
        assert result is not None
        assert result["remaining_sec"] == 14400  # 4 hours
        assert "tomorrow at 03:00" in result["depletion_time_text"]

    def test_soc_100_returns_none(self):
        """SOC=100% - nothing to count."""
        now = self._mock_now()
        grid_lost = datetime(2024, 6, 15, 17, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(100, grid_lost)
        assert result is None

    def test_soc_10_returns_none(self):
        """SOC=10% - already at the shutdown limit."""
        now = self._mock_now()
        grid_lost = datetime(2024, 6, 15, 17, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(10, grid_lost)
        assert result is None

    def test_soc_below_10_returns_none(self):
        """SOC=5% - below the threshold."""
        now = self._mock_now()
        grid_lost = datetime(2024, 6, 15, 17, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(5, grid_lost)
        assert result is None

    def test_grid_lost_time_zero_returns_none(self):
        """grid_lost_time=0 - it is unknown when it was disabled."""
        result = estimate_battery_runtime(70, 0)
        assert result is None

    def test_grid_lost_time_negative_returns_none(self):
        """grid_lost_time<0 - invalid."""
        result = estimate_battery_runtime(70, -1)
        assert result is None

    def test_too_little_time_returns_none(self):
        """Less than 2 minutes from the moment of shutdown - not enough data."""
        now = self._mock_now(hour=18, minute=1)
        # 60 seconds ago
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(95, grid_lost)
        assert result is None

    def test_depletion_today(self):
        """Discharge today - text 'today in HH:MM'."""
        now = self._mock_now(hour=14, minute=0)
        # 1 hour ago, discharged 10% → another 80% to 10% → 8 hours → 22:00 today
        grid_lost = datetime(2024, 6, 15, 13, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(90, grid_lost)
        assert result is not None
        assert "today at 22:00" in result["depletion_time_text"]

    def test_depletion_tomorrow(self):
        """Discharge tomorrow - text 'tomorrow at HH:MM'."""
        now = self._mock_now(hour=22, minute=0)
        # 2 hours ago, discharged 10% → another 80% to 10% → 16 hours → tomorrow 14:00
        grid_lost = datetime(2024, 6, 15, 20, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(90, grid_lost)
        assert result is not None
        assert "tomorrow at 14:00" in result["depletion_time_text"]

    def test_remaining_text_format(self):
        """Check the remaining_text format (format_duration)."""
        now = self._mock_now(hour=19, minute=0)
        # 1 hour ago, discharged 20% → another 70% to 10% → 3.5 hours
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(80, grid_lost)
        assert result is not None
        assert result["remaining_sec"] == 12600  # 3.5 hours = 12600 sec
        assert "3 h 30 min" in result["remaining_text"]

    def test_with_generator_uses_baseline(self):
        """The generator works: the rate is taken from the baseline period."""
        # Scenario: The grid was turned off at 18:00. By 19:00 it was discharged to 80% (rate = 20%/h).
        # The generator starts at 19:00 and charges the battery back to 90% by 20:00.
        # Forecast “without generator”: from 90% to 10% at a rate of 20%/h → 4 hours.
        now = datetime(2024, 6, 15, 20, 0, 0)
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        pre_gen_time = datetime(2024, 6, 15, 19, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(
                90,
                grid_lost,
                generator_on=True,
                pre_gen_soc=80,
                pre_gen_time=pre_gen_time,
            )
        assert result is not None
        assert result["remaining_sec"] == 4 * 3600

    def test_with_generator_no_baseline_returns_none(self):
        """The generator is running but the baseline has not yet been accumulated - None."""
        now = datetime(2024, 6, 15, 19, 0, 0)
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(
                85,
                grid_lost,
                generator_on=True,
                pre_gen_soc=0,
                pre_gen_time=0,
            )
        assert result is None

    def test_with_generator_baseline_too_short_returns_none(self):
        """Baseline accumulated less than 2 minutes - not enough data."""
        now = datetime(2024, 6, 15, 19, 0, 0)
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        # baseline only 60 sec after grid_lost
        pre_gen_time = datetime(2024, 6, 15, 18, 1, 0).timestamp()
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(
                85,
                grid_lost,
                generator_on=True,
                pre_gen_soc=98,
                pre_gen_time=pre_gen_time,
            )
        assert result is None

    def test_without_generator_ignores_baseline(self):
        """When generator_on=False, baseline parameters are not used."""
        now = self._mock_now(hour=20, minute=0)
        grid_lost = datetime(2024, 6, 15, 18, 0, 0).timestamp()
        # An invalid baseline must be ignored.
        pre_gen_time = datetime(2024, 6, 15, 17, 0, 0).timestamp()  # BEFORE grid_lost
        with patch("inverterscout.settings.runtime.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.fromtimestamp = datetime.fromtimestamp
            result = estimate_battery_runtime(
                70,
                grid_lost,
                generator_on=False,
                pre_gen_soc=99,
                pre_gen_time=pre_gen_time,
            )
        assert result is not None
        assert result["remaining_sec"] == 14400  # the same 4h from test_normal_discharge
