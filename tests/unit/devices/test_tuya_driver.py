"""TuyaDriver tests: persistent socket, get_state, is_reachable, errors.

tinytuya is completely wet - the tests do not touch real devices."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from inverterscout.devices.manager import DeviceConfig, DeviceState

TUYA_CONFIG = DeviceConfig(
    id="test_plug",
    name="Test socket",
    provider="tuya",
    host="192.0.2.99",
    config={
        "device_id": "abc123",
        "local_key": "secret_key_123",
        "version": 3.5,
    },
)


@pytest.fixture
def mock_tinytuya():
    """Mocking tinytuya.OutletDevice."""
    with patch("inverterscout.devices.tuya.tinytuya") as mock_mod:
        mock_device = MagicMock()
        mock_mod.OutletDevice.return_value = mock_device
        yield mock_device


@pytest.fixture
def driver(mock_tinytuya):
    """Creates a TuyaDriver with tinytuya embedded."""
    from inverterscout.devices.tuya import TuyaDriver

    return TuyaDriver(TUYA_CONFIG)


@pytest.fixture
def mock_tinytuya_module():
    """Mock tinytuya for is_reachable tests that require a fresh socket."""
    with patch("inverterscout.devices.tuya.tinytuya") as mock_mod:
        yield mock_mod


class TestSingleRead:
    """Persistent socket: one status() for full DPS, updatedps-fallback for partial."""

    async def test_single_status_when_dps1_present(self, driver, mock_tinytuya):
        """DPS 1 is → status() once, updatedps is not called."""
        mock_tinytuya.status.return_value = {"dps": {"1": True}}

        state = await driver.get_state()

        mock_tinytuya.status.assert_called_once()
        mock_tinytuya.updatedps.assert_not_called()
        assert state.on is True

    async def test_partial_dps_triggers_updatedps_retry(self, driver, mock_tinytuya):
        """Partial DPS (no DPS 1) → updatedps([1]) + repeat status()."""
        mock_tinytuya.status.side_effect = [
            {"dps": {"23": 1942}},  # first - partial
            {"dps": {"1": True, "23": 1942}},  # retry - complete
        ]

        state = await driver.get_state()

        assert mock_tinytuya.status.call_count == 2
        mock_tinytuya.updatedps.assert_called_once_with([1])
        assert state == DeviceState(online=True, on=True)

    async def test_no_sleep_in_status(self, driver, mock_tinytuya):
        """time.sleep is not called (no import time in the module)."""
        mock_tinytuya.status.return_value = {"dps": {"1": True}}

        from inverterscout.devices import tuya as tuya_driver

        assert not hasattr(tuya_driver, "time") or "time" not in dir(tuya_driver)


class TestGetState:
    """Parsing the response status() → DeviceState."""

    async def test_relay_on(self, driver, mock_tinytuya):
        mock_tinytuya.status.return_value = {"dps": {"1": True}}

        state = await driver.get_state()

        assert state == DeviceState(online=True, on=True)

    async def test_relay_off(self, driver, mock_tinytuya):
        mock_tinytuya.status.return_value = {"dps": {"1": False}}

        state = await driver.get_state()

        assert state == DeviceState(online=True, on=False)

    async def test_no_dps1_after_retry_returns_on_none(self, driver, mock_tinytuya):
        """If there is no DPS 1 and after updatedps+retry - on=None."""
        mock_tinytuya.status.side_effect = [
            {"dps": {"20": 2300}},  # first - partial
            {"dps": {"20": 2300}},  # retry - also partial
        ]

        state = await driver.get_state()

        assert state == DeviceState(online=True, on=None)
        mock_tinytuya.updatedps.assert_called_once_with([1])


class TestErrors:
    """Tuya error handling."""

    async def test_error_904_heartbeat_then_none(self, driver, mock_tinytuya):
        """Error 904 → heartbeat to reset the session → None (retry)."""
        mock_tinytuya.status.return_value = {"Error": "904", "Err": "904"}

        state = await driver.get_state()

        assert state is None
        mock_tinytuya.heartbeat.assert_called_once_with(nowait=True)

    async def test_error_914_heartbeat_then_none(self, driver, mock_tinytuya):
        """Error 914 → heartbeat to reset the session → None (retry)."""
        mock_tinytuya.status.return_value = {"Error": "914", "Err": "914"}

        state = await driver.get_state()

        assert state is None
        mock_tinytuya.heartbeat.assert_called_once_with(nowait=True)

    async def test_heartbeat_fail_resets_socket(self, driver, mock_tinytuya):
        """If the heartbeat also dropped → _reset_socket → None."""
        mock_tinytuya.status.return_value = {"Error": "904", "Err": "904"}
        mock_tinytuya.heartbeat.side_effect = Exception("heartbeat failed")

        state = await driver.get_state()

        assert state is None
        mock_tinytuya.heartbeat.assert_called_once_with(nowait=True)
        mock_tinytuya.close.assert_called()  # _reset_socket called

    async def test_other_error_returns_offline(self, driver, mock_tinytuya):
        """Other errors → DeviceState(online=False)."""
        mock_tinytuya.status.return_value = {"Error": "network timeout"}

        state = await driver.get_state()

        assert state == DeviceState(online=False)

    async def test_timeout_returns_error(self, driver, mock_tinytuya):
        """Timeout → error, socket reset."""
        mock_tinytuya.status.side_effect = Exception("timed out")

        state = await driver.get_state()

        assert state == DeviceState(online=False) or state is None


class TestIsReachable:
    """is_reachable() uses updatedps instead of ICMP ping."""

    async def test_reachable_on_first_attempt(self, mock_tinytuya_module):
        """updatedps successful on first try → True."""
        from inverterscout.devices.tuya import TuyaDriver

        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.return_value = {"dps": {"1": True}}
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(TUYA_CONFIG)
        result = await driver.is_reachable()

        assert result is True
        mock_ping_device.set_socketPersistent.assert_called_with(False)

    async def test_unreachable_after_all_retries(self, mock_tinytuya_module):
        """All updatedps attempts failed → False."""
        from inverterscout.devices.tuya import TuyaDriver

        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.return_value = {"Error": "timeout", "Err": "905"}
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(TUYA_CONFIG)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await driver.is_reachable()

        assert result is False
        # Default 3 attempts (ping_count)
        assert mock_ping_device.updatedps.call_count == 3

    async def test_reachable_after_retry(self, mock_tinytuya_module):
        """A successful second attempt reports the device as reachable."""
        from inverterscout.devices.tuya import TuyaDriver

        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.side_effect = [
            {"Error": "timeout"},  # attempt 1
            {"dps": {"1": True}},  # attempt 2
        ]
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(TUYA_CONFIG)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await driver.is_reachable()

        assert result is True
        assert mock_ping_device.updatedps.call_count == 2

    async def test_ping_904_retries_within_attempt(self, mock_tinytuya_module):
        """904 in ping → close + repeated updatedps inside _ping_updatedps."""
        from inverterscout.devices.tuya import TuyaDriver

        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.side_effect = [
            {"Error": "904", "Err": "904"},  # first updatedps → 904
            {"dps": {"1": True}},  # retry after close → OK
        ]
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(TUYA_CONFIG)
        result = await driver.is_reachable()

        assert result is True
        assert mock_ping_device.updatedps.call_count == 2
        mock_ping_device.close.assert_called()  # socket reset before retry

    async def test_exception_in_updatedps_counts_as_fail(self, mock_tinytuya_module):
        """Exception in updatedps → the attempt is considered unsuccessful."""
        from inverterscout.devices.tuya import TuyaDriver

        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.side_effect = Exception("connection refused")
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(TUYA_CONFIG)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await driver.is_reachable()

        assert result is False

    async def test_fresh_socket_per_attempt(self, mock_tinytuya_module):
        """Each attempt creates a fresh OutletDevice (thread safety)."""
        from inverterscout.devices.tuya import TuyaDriver

        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.return_value = {"dps": {"1": True}}
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(TUYA_CONFIG)
        await driver.is_reachable()

        # OutletDevice is created at least 2 times: 1 in __init__ + 1 in _ping_updatedps
        assert mock_tinytuya_module.OutletDevice.call_count >= 2

    async def test_ping_timeout_from_config(self, mock_tinytuya_module):
        """ping_timeout from config is used for socketTimeout."""
        from inverterscout.devices.tuya import TuyaDriver

        config_with_timeout = DeviceConfig(
            id="slow_plug",
            name="Slow socket",
            provider="tuya",
            host="192.0.2.99",
            config={"device_id": "abc", "local_key": "key", "version": 3.4, "ping_timeout": 8},
        )
        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.return_value = {"dps": {"1": True}}
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(config_with_timeout)
        await driver.is_reachable()

        # Check that socketTimeout is set with ping_timeout=8
        calls = mock_ping_device.set_socketTimeout.call_args_list
        assert any(c == call(8) for c in calls)

    async def test_ping_count_from_config(self, mock_tinytuya_module):
        """ping_count from config sets the number of attempts."""
        from inverterscout.devices.tuya import TuyaDriver

        config_2_retries = DeviceConfig(
            id="plug2",
            name="Socket 2",
            provider="tuya",
            host="192.0.2.99",
            config={"device_id": "abc", "local_key": "key", "version": 3.5, "ping_count": 2},
        )
        mock_ping_device = MagicMock()
        mock_ping_device.updatedps.return_value = {"Error": "timeout"}
        mock_tinytuya_module.OutletDevice.return_value = mock_ping_device

        driver = TuyaDriver(config_2_retries)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await driver.is_reachable()

        assert mock_ping_device.updatedps.call_count == 2


class TestSocketSafety:
    """socketPersistent=True — persistent socket for a stable connection."""

    def test_socket_persistent_is_true(self, driver, mock_tinytuya):
        """socketPersistent MUST be True."""
        mock_tinytuya.set_socketPersistent.assert_called_once_with(True)
