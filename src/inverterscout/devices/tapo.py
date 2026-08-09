"""TapoDriver is a driver for controlling TP-Link Tapo devices over LAN.

Uses the tapo library (mihai-dinculescu) for local control.
Supports Tapo P100/P110/P115, L530 and other devices via generic_device().
Works with TPAP (lv=2) encryption of new firmware."""

import asyncio
import logging

from tapo import ApiClient

from inverterscout.devices.manager import DeviceConfig, DeviceDriver, DeviceState

logger = logging.getLogger(__name__)

# Connection and operation timeout in seconds.
CONNECT_TIMEOUT = 5


class TapoDriver(DeviceDriver):
    """Driver for Tapo devices via LAN (tapo library)."""

    # Energy-capable models and the matching ApiClient factory method.
    ENERGY_HANDLERS = {"P110": "p110", "P115": "p115"}

    def __init__(self, device_config: DeviceConfig):
        super().__init__(device_config)
        self._device = None  # Initialized lazily because the provider API is asynchronous.
        self._energy_device = None  # Dedicated energy API handle for P110/P115 models.
        self._username = device_config.config.get("username", "")
        self._password = device_config.config.get("password", "")
        self._model: str | None = None
        logger.info("[TAPO] Initialized %s (%s)", device_config.id, device_config.host)

    async def _ensure_device(self):
        """Lazy-init: Connect to the device on the first call."""
        if self._device is not None:
            return
        try:
            client = ApiClient(self._username, self._password)
            self._device = await asyncio.wait_for(
                client.generic_device(self.config.host),
                timeout=CONNECT_TIMEOUT,
            )
            logger.info("[TAPO] %s: connected to %s", self.config.id, self.config.host)
        except Exception as e:
            logger.error("[TAPO] %s: connection error: %s", self.config.id, type(e).__name__)
            self._device = None
            raise

    def _reset_device(self):
        """Reset the connection (if there is an error, it will be recreated on the next call)."""
        self._device = None
        self._energy_device = None
        self._model = None

    async def turn_on(self) -> bool:
        try:
            await self._ensure_device()
            await asyncio.wait_for(self._device.on(), timeout=CONNECT_TIMEOUT)
            logger.info("[TAPO] %s: turn_on OK", self.config.id)
            return True
        except asyncio.TimeoutError:
            logger.error("[TAPO] %s turn_on: timeout", self.config.id)
            self._reset_device()
            return False
        except Exception as e:
            logger.error("[TAPO] %s turn_on error: %s", self.config.id, type(e).__name__)
            self._reset_device()
            return False

    async def turn_off(self) -> bool:
        try:
            await self._ensure_device()
            await asyncio.wait_for(self._device.off(), timeout=CONNECT_TIMEOUT)
            logger.info("[TAPO] %s: turn_off OK", self.config.id)
            return True
        except asyncio.TimeoutError:
            logger.error("[TAPO] %s turn_off: timeout", self.config.id)
            self._reset_device()
            return False
        except Exception as e:
            logger.error("[TAPO] %s turn_off error: %s", self.config.id, type(e).__name__)
            self._reset_device()
            return False

    async def get_state(self) -> DeviceState | None:
        try:
            await self._ensure_device()
            info = await asyncio.wait_for(
                self._device.get_device_info_json(),
                timeout=CONNECT_TIMEOUT,
            )
            on = info.get("device_on", False)
            logger.info("[TAPO] %s get_state: OK on=%s", self.config.id, on)
            return DeviceState(online=True, on=on)
        except asyncio.TimeoutError:
            logger.error("[TAPO] %s get_state: timeout", self.config.id)
            self._reset_device()
            return DeviceState(online=False)
        except Exception as e:
            logger.error("[TAPO] %s get_state error: %s", self.config.id, type(e).__name__)
            self._reset_device()
            return DeviceState(online=False)

    async def set_level(self, level: int) -> bool:
        logger.warning("[TAPO] %s: set_level is not supported for sockets", self.config.id)
        return False

    async def get_current_power_w(self) -> int | None:
        """Instantaneous power (W) for P110/P115. None if model without energy monitoring
        or an error has occurred."""
        try:
            await self._ensure_device()
            if self._model is None:
                info = await asyncio.wait_for(
                    self._device.get_device_info_json(),
                    timeout=CONNECT_TIMEOUT,
                )
                self._model = info.get("model")
            handler_name = self.ENERGY_HANDLERS.get(self._model or "")
            if handler_name is None:
                return None
            if self._energy_device is None:
                client = ApiClient(self._username, self._password)
                self._energy_device = await asyncio.wait_for(
                    getattr(client, handler_name)(self.config.host),
                    timeout=CONNECT_TIMEOUT,
                )
            result = await asyncio.wait_for(
                self._energy_device.get_current_power(),
                timeout=CONNECT_TIMEOUT,
            )
            if hasattr(result, "current_power"):
                return int(result.current_power)
            if hasattr(result, "to_dict"):
                return int(result.to_dict().get("current_power", 0))
            return int(getattr(result, "__dict__", {}).get("current_power", 0))
        except asyncio.TimeoutError:
            logger.warning("[TAPO] %s get_current_power_w: timeout", self.config.id)
            self._reset_device()
            return None
        except Exception as e:
            logger.warning("[TAPO] %s get_current_power_w: %s", self.config.id, type(e).__name__)
            self._reset_device()
            return None

    async def is_reachable(self) -> bool:
        """Availability check: get_device_info_json() no errors = device online."""
        retries = self.config.config.get("ping_count", 3)
        timeout = self.config.config.get("ping_timeout", CONNECT_TIMEOUT)

        for attempt in range(retries):
            try:
                await self._ensure_device()
                await asyncio.wait_for(
                    self._device.get_device_info_json(),
                    timeout=timeout,
                )
                return True
            except Exception:
                self._reset_device()
                if attempt < retries - 1:
                    await asyncio.sleep(1)

        logger.info("[TAPO] %s is_reachable: FAIL after %d attempts", self.config.id, retries)
        return False
