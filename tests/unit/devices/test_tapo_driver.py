"""TapoDriver tests: lazy-init, get_state, turn_on/off, is_reachable, errors.

The tapo library is completely mocked - the tests do not touch real devices."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from inverterscout.devices.manager import DeviceConfig, DeviceState

TAPO_CONFIG = DeviceConfig(
    id="test_tapo",
    name="Tapo test socket",
    provider="tapo",
    host="192.0.2.50",
    config={
        "username": "synthetic-tapo-account",
        "password": "secret123",
    },
)


@pytest.fixture
def mock_tapo_device():
    """Mock Tapo device (generic_device)."""
    device = AsyncMock()
    device.on = AsyncMock()
    device.off = AsyncMock()
    device.get_device_info_json = AsyncMock(return_value={"device_on": True})
    return device


@pytest.fixture
def mock_api_client(mock_tapo_device):
    """Mock ApiClient → generic_device() returns mock_tapo_device."""
    with patch("inverterscout.devices.tapo.ApiClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.generic_device = AsyncMock(return_value=mock_tapo_device)
        mock_cls.return_value = mock_client
        yield mock_cls


@pytest.fixture
def driver(mock_api_client):
    """Creates a TapoDriver with the embedded tapo library."""
    from inverterscout.devices.tapo import TapoDriver

    return TapoDriver(TAPO_CONFIG)


# ──────────────────────────────────────────────
# Lazy init
# ──────────────────────────────────────────────
class TestLazyInit:
    """Connect to the device only on the first call."""

    def test_device_is_none_after_init(self, driver):
        """After creation _device = None (lazy)."""
        assert driver._device is None

    async def test_first_call_connects(self, driver, mock_api_client, mock_tapo_device):
        """The first state query initializes the API client and device handle."""
        await driver.get_state()

        mock_api_client.assert_called_once_with("synthetic-tapo-account", "secret123")
        mock_client = mock_api_client.return_value
        mock_client.generic_device.assert_awaited_once_with("192.0.2.50")
        assert driver._device is mock_tapo_device

    async def test_second_call_reuses_device(self, driver, mock_api_client, mock_tapo_device):
        """The redial call is not reconnected."""
        await driver.get_state()
        await driver.get_state()

        # The API client is initialized once.
        mock_api_client.assert_called_once()

    async def test_credentials_passed_to_client(self, driver, mock_api_client):
        """Username/password are passed to ApiClient."""
        await driver.get_state()

        mock_api_client.assert_called_once_with("synthetic-tapo-account", "secret123")


# ──────────────────────────────────────────────
# get_state
# ──────────────────────────────────────────────
class TestGetState:
    """Device state parsing."""

    async def test_device_on(self, driver, mock_tapo_device):
        """device_on=True → DeviceState(online=True, on=True)."""
        mock_tapo_device.get_device_info_json = AsyncMock(return_value={"device_on": True})

        state = await driver.get_state()

        assert state == DeviceState(online=True, on=True)

    async def test_device_off(self, driver, mock_tapo_device):
        """device_on=False → DeviceState(online=True, on=False)."""
        mock_tapo_device.get_device_info_json = AsyncMock(return_value={"device_on": False})

        state = await driver.get_state()

        assert state == DeviceState(online=True, on=False)

    async def test_get_device_info_called(self, driver, mock_tapo_device):
        """get_device_info_json() is called to get the state."""
        await driver.get_state()

        mock_tapo_device.get_device_info_json.assert_awaited_once()


# ──────────────────────────────────────────────
# turn_on / turn_off
# ──────────────────────────────────────────────
class TestTurnOnOff:
    """Turn on/off the device."""

    async def test_turn_on_success(self, driver, mock_tapo_device):
        result = await driver.turn_on()

        assert result is True
        mock_tapo_device.on.assert_awaited_once()

    async def test_turn_off_success(self, driver, mock_tapo_device):
        result = await driver.turn_off()

        assert result is True
        mock_tapo_device.off.assert_awaited_once()

    async def test_turn_on_error_returns_false(self, driver, mock_tapo_device):
        """Error when turn_on → False + device reset."""
        mock_tapo_device.on.side_effect = Exception("Tapo(Forbidden)")

        result = await driver.turn_on()

        assert result is False
        assert driver._device is None  # reset for reconnect

    async def test_turn_off_error_returns_false(self, driver, mock_tapo_device):
        """Error with turn_off → False + device reset."""
        mock_tapo_device.off.side_effect = Exception("Tapo(Forbidden)")

        result = await driver.turn_off()

        assert result is False
        assert driver._device is None


# ──────────────────────────────────────────────
# Errors and reconnect
# ──────────────────────────────────────────────
class TestErrors:
    """Error handling and reconnection."""

    async def test_get_state_error_returns_offline(self, driver, mock_tapo_device):
        """Exception in get_device_info_json() → offline."""
        mock_tapo_device.get_device_info_json.side_effect = Exception("connection lost")

        state = await driver.get_state()

        assert state == DeviceState(online=False)
        assert driver._device is None

    async def test_get_state_timeout_returns_offline(self, driver, mock_tapo_device):
        """Timeout in get_device_info_json() → offline."""

        async def slow_get_info():
            await asyncio.sleep(100)

        mock_tapo_device.get_device_info_json = slow_get_info

        import asyncio

        state = await driver.get_state()

        assert state == DeviceState(online=False)
        assert driver._device is None

    async def test_connection_error_resets_device(self, driver, mock_api_client):
        """Connection error → _device remains None."""
        mock_client = mock_api_client.return_value
        mock_client.generic_device = AsyncMock(side_effect=OSError("no route"))

        state = await driver.get_state()

        assert state == DeviceState(online=False)
        assert driver._device is None

    async def test_reconnect_after_error(self, driver, mock_api_client, mock_tapo_device):
        """After an error, the next call is reconnected."""
        # First call - OK
        await driver.get_state()
        assert driver._device is not None

        # Error - reset
        mock_tapo_device.get_device_info_json.side_effect = Exception("lost")
        await driver.get_state()
        assert driver._device is None

        # Next call - reconnect
        mock_tapo_device.get_device_info_json.side_effect = None
        mock_tapo_device.get_device_info_json.return_value = {"device_on": False}
        await driver.get_state()

        # Reconnection initializes a fresh API client.
        assert mock_api_client.call_count == 2


# ──────────────────────────────────────────────
# set_level
# ──────────────────────────────────────────────
class TestSetLevel:
    """set_level is not supported for sockets."""

    async def test_set_level_returns_false(self, driver):
        result = await driver.set_level(50)

        assert result is False


# ──────────────────────────────────────────────
# is_reachable
# ──────────────────────────────────────────────
class TestIsReachable:
    """Availability check via get_device_info_json()."""

    async def test_reachable_on_first_attempt(self, driver, mock_tapo_device):
        """get_device_info_json() without errors → True."""
        result = await driver.is_reachable()

        assert result is True

    async def test_unreachable_after_all_retries(self, driver, mock_api_client, mock_tapo_device):
        """All attempts failed → False."""
        mock_tapo_device.get_device_info_json.side_effect = OSError("timeout")

        with patch("inverterscout.devices.tapo.asyncio.sleep", new_callable=AsyncMock):
            result = await driver.is_reachable()

        assert result is False

    async def test_reachable_after_retry(self, driver, mock_api_client, mock_tapo_device):
        """A successful second attempt reports the device as reachable."""
        call_count = 0

        async def flaky_get_info():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("timeout")
            return {"device_on": True}

        mock_tapo_device.get_device_info_json = flaky_get_info

        with patch("inverterscout.devices.tapo.asyncio.sleep", new_callable=AsyncMock):
            result = await driver.is_reachable()

        assert result is True

    async def test_ping_count_from_config(self, mock_api_client, mock_tapo_device):
        """ping_count from config sets the number of attempts."""
        from inverterscout.devices.tapo import TapoDriver

        config_2 = DeviceConfig(
            id="tapo2",
            name="Tapo 2",
            provider="tapo",
            host="192.0.2.50",
            config={"username": "u", "password": "p", "ping_count": 2},
        )

        mock_tapo_device.get_device_info_json.side_effect = OSError("fail")
        driver = TapoDriver(config_2)

        with patch("inverterscout.devices.tapo.asyncio.sleep", new_callable=AsyncMock):
            result = await driver.is_reachable()

        assert result is False
        # Each failed reachability attempt resets and recreates the API client.
        assert mock_api_client.call_count == 2
