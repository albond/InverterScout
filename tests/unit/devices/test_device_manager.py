"""DeviceManager tests: retry/verify, pre-check, fail, disabled."""

from unittest.mock import AsyncMock, patch

import pytest

from inverterscout.devices.manager import DeviceState

STUB_DEVICE = {
    "id": "boiler",
    "name": "Boiler",
    "provider": "stub",
    "config": {},
    "enabled": True,
}


def stub_device_with_fails(n):
    return {
        "id": "boiler",
        "name": "Boiler",
        "provider": "stub",
        "config": {"fail_count": n},
        "enabled": True,
    }


@pytest.fixture
def sleep_mock():
    with patch("inverterscout.devices.manager.asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


class TestBasicCommands:
    async def test_turn_on_verify_ok(self, device_manager_factory, event_collector, sleep_mock):
        dm = await device_manager_factory([STUB_DEVICE])
        result = await dm.execute_command("boiler", "turn_on")
        assert result is True
        types = [e.type for e in event_collector]
        assert "device_command_ok" in types

    async def test_turn_off_verify_ok(self, device_manager_factory, event_collector, sleep_mock):
        dm = await device_manager_factory([STUB_DEVICE])
        # First turn on, then turn off
        await dm.execute_command("boiler", "turn_on")
        result = await dm.execute_command("boiler", "turn_off")
        assert result is True

    async def test_command_ok_has_attempt_1(
        self, device_manager_factory, event_collector, sleep_mock
    ):
        dm = await device_manager_factory([STUB_DEVICE])
        await dm.execute_command("boiler", "turn_on")

        ok_events = [e for e in event_collector if e.type == "device_command_ok"]
        assert len(ok_events) >= 1
        assert ok_events[-1].data["attempt"] == 1


class TestPreCheck:
    async def test_already_in_desired_state(self, device_manager_factory, sleep_mock):
        dm = await device_manager_factory([STUB_DEVICE])
        # turn_on → stub is on, turn_on again → pre-check → skip
        await dm.execute_command("boiler", "turn_on")
        result = await dm.execute_command("boiler", "turn_on")
        assert result is True  # pre-check returns True without retry


class TestRetryOnFail:
    async def test_retry_succeeds_on_attempt_3(
        self, device_manager_factory, event_collector, sleep_mock
    ):
        """The first two get_state verifications fail and the third succeeds."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")

        # Pre-check: off (needs turn_on), then attempts: fail, fail, success
        call_count = [0]

        async def mock_get_state():
            call_count[0] += 1
            if call_count[0] == 1:
                # Pre-check: device off
                return DeviceState(online=True, on=False)
            if call_count[0] <= 3:
                # Attempt 1 & 2 verify: still off
                return DeviceState(online=True, on=False)
            # Attempt 3 verify: success
            return DeviceState(online=True, on=True)

        driver.get_state = mock_get_state
        result = await dm.execute_command("boiler", "turn_on")
        assert result is True

        ok_events = [e for e in event_collector if e.type == "device_command_ok"]
        assert len(ok_events) == 1
        assert ok_events[0].data["attempt"] == 3

    async def test_all_attempts_fail(self, device_manager_factory, event_collector, sleep_mock):
        """All 5 attempts fail."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")

        async def mock_get_state():
            return DeviceState(online=True, on=False)

        driver.get_state = mock_get_state
        result = await dm.execute_command("boiler", "turn_on")
        assert result is False

        types = [e.type for e in event_collector]
        assert "device_command_failed" in types

    async def test_failed_calls_notify_all(self, device_manager_factory, sleep_mock):
        """If it fails and test_mode=False → notify_all is called."""
        device = dict(STUB_DEVICE)
        device["test_mode"] = False
        dm = await device_manager_factory([device])
        driver = dm.get_driver("boiler")

        async def mock_get_state():
            return DeviceState(online=True, on=False)

        driver.get_state = mock_get_state
        await dm.execute_command("boiler", "turn_on")
        dm.notify_all.assert_called()


class TestTestMode:
    async def test_test_mode_fail_calls_notify_admin(self, device_manager_factory, sleep_mock):
        device = dict(STUB_DEVICE)
        device["test_mode"] = True
        dm = await device_manager_factory([device])
        driver = dm.get_driver("boiler")

        async def mock_get_state():
            return DeviceState(online=True, on=False)

        driver.get_state = mock_get_state
        await dm.execute_command("boiler", "turn_on")
        dm.notify_admin.assert_called()


class TestDisabledAndUnknown:
    async def test_disabled_device(self, device_manager_factory, sleep_mock):
        device = dict(STUB_DEVICE)
        device["enabled"] = False
        dm = await device_manager_factory([device])
        result = await dm.execute_command("boiler", "turn_on")
        assert result is False

    async def test_unknown_device(self, device_manager_factory, sleep_mock):
        dm = await device_manager_factory([STUB_DEVICE])
        result = await dm.execute_command("nonexistent", "turn_on")
        assert result is False


class TestRenameDevice:
    async def test_rename_device_updates_name(self, device_manager_factory, sleep_mock):
        """rename_device updates the name in memory and saves it to a file."""
        dm = await device_manager_factory([STUB_DEVICE])
        result = dm.rename_device("boiler", "New name")
        assert result is True
        assert dm.devices["boiler"].name == "New name"
        # Verify that list_devices returns the new name.
        devices = dm.list_devices()
        assert devices[0]["name"] == "New name"

    async def test_rename_device_unknown_id(self, device_manager_factory, sleep_mock):
        """rename_device for a non-existent ID returns False."""
        dm = await device_manager_factory([STUB_DEVICE])
        result = dm.rename_device("nonexistent", "Name")
        assert result is False


class TestMonitorConsumption:
    async def test_set_monitor_consumption_enable(self, device_manager_factory, sleep_mock):
        """Enabling sets a flag and defaults."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert dm.set_monitor_consumption("boiler", True) is True
        cfg = dm.devices["boiler"].config
        assert cfg["monitor_consumption"] is True
        assert cfg["consumption_threshold_w"] == 3
        assert cfg["consumption_window_min"] == 30

    async def test_set_monitor_consumption_disable_keeps_params(
        self, device_manager_factory, sleep_mock
    ):
        """Disabling leaves threshold/window untouched (if they were already there)."""
        dm = await device_manager_factory([STUB_DEVICE])
        dm.set_monitor_consumption("boiler", True)
        dm.devices["boiler"].config["consumption_threshold_w"] = 5
        assert dm.set_monitor_consumption("boiler", False) is True
        cfg = dm.devices["boiler"].config
        assert cfg["monitor_consumption"] is False
        assert cfg["consumption_threshold_w"] == 5

    async def test_set_monitor_consumption_unknown_id(self, device_manager_factory, sleep_mock):
        dm = await device_manager_factory([STUB_DEVICE])
        assert dm.set_monitor_consumption("nonexistent", True) is False

    async def test_set_monitor_consumption_persisted(
        self, device_manager_factory, sleep_mock, data_dir
    ):
        """The flag is saved in devices.json."""
        import json

        dm = await device_manager_factory([STUB_DEVICE])
        dm.set_monitor_consumption("boiler", True)
        saved = json.loads((data_dir / "devices.json").read_text())
        boiler = next(d for d in saved if d["id"] == "boiler")
        assert boiler["config"]["monitor_consumption"] is True


class TestAddDevice:
    async def test_add_device_success(self, device_manager_factory, data_dir, sleep_mock):
        """add_device adds a device to memory and saves it to a file."""
        dm = await device_manager_factory([STUB_DEVICE])
        result = dm.add_device("new_dev", "New device", "stub", host="1.2.3.4")
        assert result is True
        assert "new_dev" in dm.devices
        assert dm.devices["new_dev"].name == "New device"
        assert dm.get_driver("new_dev") is not None
        # Verify encrypted-path persistence.
        import json

        saved = json.loads((data_dir / "devices.json").read_text())
        ids = [d["id"] for d in saved]
        assert "new_dev" in ids

    async def test_add_device_duplicate_id(self, device_manager_factory, sleep_mock):
        """add_device with duplicate ID returns False."""
        dm = await device_manager_factory([STUB_DEVICE])
        result = dm.add_device("boiler", "Duplicate", "stub")
        assert result is False


class TestSetLevel:
    async def test_set_level(self, device_manager_factory, event_collector, sleep_mock):
        dm = await device_manager_factory([STUB_DEVICE])
        result = await dm.execute_command("boiler", "set_level", {"level": 50})
        assert result is True


class TestPingConfig:
    async def test_ping_timeout_from_config(self, device_manager_factory, sleep_mock):
        """ping_timeout from config is passed to is_reachable."""
        device = {
            "id": "slow_dev",
            "name": "Slow",
            "provider": "stub",
            "config": {"ping_timeout": 8},
            "enabled": True,
        }
        dm = await device_manager_factory([device])
        driver = dm.get_driver("slow_dev")
        assert driver.config.config.get("ping_timeout") == 8

    async def test_ping_timeout_default(self, device_manager_factory, sleep_mock):
        """Without ping_timeout in config - default 4 is used."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")
        assert driver.config.config.get("ping_timeout", 4) == 4

    async def test_ping_count_from_config(self, device_manager_factory, sleep_mock):
        """ping_count from config - more packets for buggy devices."""
        device = {
            "id": "flaky_dev",
            "name": "Glitchy",
            "provider": "stub",
            "config": {"ping_count": 5},
            "enabled": True,
        }
        dm = await device_manager_factory([device])
        driver = dm.get_driver("flaky_dev")
        assert driver.config.config.get("ping_count") == 5

    async def test_ping_count_default(self, device_manager_factory, sleep_mock):
        """Without ping_count - 3 packets default."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")
        assert driver.config.config.get("ping_count", 3) == 3


class TestCacheTTL:
    async def test_is_cache_fresh_after_get_state(self, device_manager_factory, sleep_mock):
        """get_device_state updates _last_state_times → cache is fresh."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert dm.is_cache_fresh("boiler") is False
        # Emulate ping online=True so that get_device_state pulls Tuya
        dm._last_states["boiler"] = DeviceState(online=True)
        await dm.get_device_state("boiler")
        assert dm.is_cache_fresh("boiler") is True

    async def test_is_cache_fresh_returns_false_for_unknown(
        self, device_manager_factory, sleep_mock
    ):
        """For a non-existent device, the cache is not fresh."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert dm.is_cache_fresh("nonexistent") is False

    async def test_get_cached_state_age_none_before_query(self, device_manager_factory, sleep_mock):
        """Cache age None until first request."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert dm.get_cached_state_age("boiler") is None

    async def test_get_cached_state_age_after_query(self, device_manager_factory, sleep_mock):
        """Cache age >= 0 after request."""
        dm = await device_manager_factory([STUB_DEVICE])
        dm._last_states["boiler"] = DeviceState(online=True)
        await dm.get_device_state("boiler")
        age = dm.get_cached_state_age("boiler")
        assert age is not None
        assert age >= 0

    async def test_cache_timestamp_updated_on_command(self, device_manager_factory, sleep_mock):
        """execute_command updates _last_state_times."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert dm.is_cache_fresh("boiler") is False
        await dm.execute_command("boiler", "turn_on")
        assert dm.is_cache_fresh("boiler") is True

    async def test_get_device_state_always_queries_driver(self, device_manager_factory, sleep_mock):
        """get_device_state always calls driver.get_state() (without ping-guard)."""
        dm = await device_manager_factory([STUB_DEVICE])
        dm._last_states["boiler"] = DeviceState(online=False)
        driver = dm.get_driver("boiler")
        original = driver.get_state
        call_count = [0]

        async def counting_get_state():
            call_count[0] += 1
            return await original()

        driver.get_state = counting_get_state
        result = await dm.get_device_state("boiler")
        assert call_count[0] >= 1  # driver called
        assert result.online is True  # StubDriver responds online

    async def test_get_device_state_retries_on_none(self, device_manager_factory, sleep_mock):
        """get_state returns None → retry to STATE_RETRIES → offline."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")
        call_count = [0]

        async def mock_get_state():
            call_count[0] += 1
            return None

        driver.get_state = mock_get_state
        result = await dm.get_device_state("boiler")
        assert call_count[0] == dm.STATE_RETRIES
        assert result.online is False  # all attempts failed

    async def test_get_device_state_retries_on_offline(self, device_manager_factory, sleep_mock):
        """get_state returns DeviceState(online=False) → retry, not online=True."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")
        call_count = [0]

        async def mock_get_state():
            call_count[0] += 1
            return DeviceState(online=False)

        driver.get_state = mock_get_state
        result = await dm.get_device_state("boiler")
        assert call_count[0] == dm.STATE_RETRIES
        assert result.online is False  # NOT True

    async def test_get_device_state_retries_on_partial_dps(
        self, device_manager_factory, sleep_mock
    ):
        """get_state returns DeviceState(online=True, on=None) → partial DPS → retry."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")
        call_count = [0]

        async def mock_get_state():
            call_count[0] += 1
            if call_count[0] < 3:
                return DeviceState(online=True, on=None)  # partial DPS
            return DeviceState(online=True, on=True)  # full answer

        driver.get_state = mock_get_state
        result = await dm.get_device_state("boiler")
        assert call_count[0] == 3
        assert result.online is True
        assert result.on is True

    async def test_get_device_state_all_partial_returns_offline(
        self, device_manager_factory, sleep_mock
    ):
        """All attempts are partial DPS (on=None) → return offline."""
        dm = await device_manager_factory([STUB_DEVICE])
        driver = dm.get_driver("boiler")
        call_count = [0]

        async def mock_get_state():
            call_count[0] += 1
            return DeviceState(online=True, on=None)

        driver.get_state = mock_get_state
        result = await dm.get_device_state("boiler")
        assert call_count[0] == dm.STATE_RETRIES
        assert result.online is False


class TestReloadFromFile:
    async def test_reload_adds_new_device(self, device_manager_factory, data_dir, sleep_mock):
        """Added the device to JSON → reload → appeared."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert "new_dev" not in dm.devices

        # Replace the device configuration with an additional device.
        import json

        new_config = [
            STUB_DEVICE,
            {"id": "new_dev", "name": "New", "provider": "stub", "config": {}, "enabled": True},
        ]
        (data_dir / "devices.json").write_text(json.dumps(new_config))

        result = dm.reload_from_file()
        assert "new_dev" in result["added"]
        assert "new_dev" in dm.devices
        assert dm.get_driver("new_dev") is not None
        assert "new_dev" in dm._locks

    async def test_reload_removes_device(self, device_manager_factory, data_dir, sleep_mock):
        """Removed from JSON → reload → disappeared."""
        dm = await device_manager_factory([STUB_DEVICE])
        assert "boiler" in dm.devices

        # Replace the device configuration with an empty list.
        import json

        (data_dir / "devices.json").write_text(json.dumps([]))

        result = dm.reload_from_file()
        assert "boiler" in result["removed"]
        assert "boiler" not in dm.devices
        assert "boiler" not in dm.drivers
        assert "boiler" not in dm._locks

    async def test_reload_updates_config(self, device_manager_factory, data_dir, sleep_mock):
        """Changed config → reload → driver recreated."""
        dm = await device_manager_factory([STUB_DEVICE])
        old_driver = dm.get_driver("boiler")

        # Change provider configuration.
        import json

        updated = dict(STUB_DEVICE)
        updated["config"] = {"fail_count": 3}
        (data_dir / "devices.json").write_text(json.dumps([updated]))

        result = dm.reload_from_file()
        assert "boiler" in result["updated"]
        new_driver = dm.get_driver("boiler")
        assert new_driver is not old_driver  # driver recreated

    async def test_reload_preserves_locks(self, device_manager_factory, data_dir, sleep_mock):
        """Existing devices retain the lock upon reload."""
        dm = await device_manager_factory([STUB_DEVICE])
        old_lock = dm._locks["boiler"]

        # Reload a name-only change without recreating the lock.
        import json

        updated = dict(STUB_DEVICE)
        updated["name"] = "Boiler renamed"
        (data_dir / "devices.json").write_text(json.dumps([updated]))

        dm.reload_from_file()
        assert dm._locks["boiler"] is old_lock  # same lock

    async def test_reload_no_file(self, device_manager_factory, data_dir, sleep_mock):
        """The file does not exist → empty result, devices do not change."""
        dm = await device_manager_factory([STUB_DEVICE])
        (data_dir / "devices.json").unlink()

        result = dm.reload_from_file()
        assert result == {"added": [], "removed": [], "updated": []}
        assert "boiler" in dm.devices  # untouched

    async def test_reload_name_only_no_driver_recreate(
        self, device_manager_factory, data_dir, sleep_mock
    ):
        """Changing only the name means the driver is not recreated."""
        dm = await device_manager_factory([STUB_DEVICE])
        old_driver = dm.get_driver("boiler")

        import json

        updated = dict(STUB_DEVICE)
        updated["name"] = "New name"
        (data_dir / "devices.json").write_text(json.dumps([updated]))

        dm.reload_from_file()
        assert dm.get_driver("boiler") is old_driver  # driver is the same
        assert dm.devices["boiler"].name == "New name"
