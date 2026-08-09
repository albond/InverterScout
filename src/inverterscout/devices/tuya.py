"""TuyaDriver is a driver for managing Tuya devices over LAN.

Uses tinytuya for local management (no cloud).
All tinytuya calls are synchronous and run through asyncio.to_thread()."""

import asyncio
import logging

import tinytuya

from inverterscout.devices.manager import DeviceConfig, DeviceDriver, DeviceState

logger = logging.getLogger(__name__)

# Device connection timeout in seconds.
CONNECT_TIMEOUT = 5


class TuyaDriver(DeviceDriver):
    """Driver for Tuya devices (sockets, switches) via LAN."""

    def __init__(self, device_config: DeviceConfig):
        super().__init__(device_config)
        cfg = device_config.config
        self._device = tinytuya.OutletDevice(
            dev_id=cfg["device_id"],
            address=device_config.host,
            local_key=cfg["local_key"],
            version=float(cfg.get("version", 3.5)),
        )
        self._device.set_socketPersistent(True)
        self._device.set_socketTimeout(CONNECT_TIMEOUT)
        logger.info("[TUYA] Initialized %s (%s)", device_config.id, device_config.host)

    def _status(self) -> dict:
        """Synchronous status via persistent socket.

        If the response is incomplete (no DPS 1) - updatedps([1]) wakes up the MCU,
        then repeat status() to get fresh data."""
        result = self._device.status()
        if isinstance(result, dict) and "dps" in result and "1" not in result["dps"]:
            logger.debug(
                "[TUYA] %s: partial DPS %s, updatedps([1]) + retry", self.config.id, result["dps"]
            )
            try:
                self._device.updatedps([1])
            except Exception:
                pass
            result = self._device.status()
        return result

    def _turn_on(self) -> dict:
        return self._device.turn_on()

    def _turn_off(self) -> dict:
        return self._device.turn_off()

    @staticmethod
    def _is_error(result: dict) -> bool:
        """Check tinytuya's answer for the error."""
        return "Error" in result or "Err" in result

    @staticmethod
    def _error_code(result: dict) -> str:
        """Return only a vendor error code, never an entire response payload."""
        return str(result.get("Err", result.get("Error", "unknown"))).split()[0][:32]

    def _reset_socket(self) -> None:
        """Reset persistent socket (in case of timeout/error, to reconnect)."""
        try:
            self._device.close()
        except Exception:
            pass

    async def _call(self, fn, label: str, timeout: float | None = None):
        """Wrapper: to_thread + timeout. Resets the socket when hanging."""
        t = timeout or CONNECT_TIMEOUT + 2
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=t)
        except asyncio.TimeoutError:
            logger.error("[TUYA] %s %s: timeout %.0fs", self.config.id, label, t)
            self._reset_socket()
            return {"Error": "Timeout"}
        except Exception as e:
            logger.error("[TUYA] %s %s exception: %s", self.config.id, label, type(e).__name__)
            self._reset_socket()
            return {"Error": type(e).__name__}

    async def turn_on(self) -> bool:
        result = await self._call(self._turn_on, "turn_on")
        if self._is_error(result):
            logger.error("[TUYA] %s turn_on error: %s", self.config.id, self._error_code(result))
            return False
        logger.info("[TUYA] %s: turn_on OK", self.config.id)
        return True

    async def turn_off(self) -> bool:
        result = await self._call(self._turn_off, "turn_off")
        if self._is_error(result):
            logger.error("[TUYA] %s turn_off error: %s", self.config.id, self._error_code(result))
            return False
        logger.info("[TUYA] %s: turn_off OK", self.config.id)
        return True

    # Protocol errors that require renegotiation rather than offline handling.
    _RETRYABLE_ERRORS = {"904", "914"}

    async def get_state(self) -> DeviceState | None:
        result = await self._call(self._status, "get_state")
        if self._is_error(result):
            err_code = self._error_code(result)
            # 904/914 = device responded, but the protocol is buggy - heartbeat to reset the session
            if result.get("Err") in self._RETRYABLE_ERRORS:
                logger.info(
                    "[TUYA] %s get_state: error %s, heartbeat → None (retry)",
                    self.config.id,
                    err_code,
                )
                try:
                    await asyncio.to_thread(self._device.heartbeat, nowait=True)
                except Exception:
                    logger.warning("[TUYA] %s heartbeat failed, reset socket", self.config.id)
                    self._reset_socket()
                return None
            logger.warning("[TUYA] %s get_state error: %s", self.config.id, err_code)
            return DeviceState(online=False)
        dps = result.get("dps", {})
        # DPS 1 = relay (on/off)
        relay = dps.get("1")
        on = bool(relay) if relay is not None else None
        logger.info("[TUYA] %s get_state: OK dps=%s on=%s", self.config.id, dps, on)
        return DeviceState(online=True, on=on)

    async def set_level(self, level: int) -> bool:
        logger.warning("[TUYA] %s: set_level is not supported for sockets", self.config.id)
        return False

    # ──────────────────────────────────────────────
    # Use a protocol-level status refresh instead of ICMP for reachability.
    # ──────────────────────────────────────────────
    def _ping_updatedps(self, timeout: int) -> bool:
        """One updateps attempt on a fresh socket (READ-ONLY, 0x12).

        Uses a separate OutletDevice to avoid overlap
        with main self._device(thread safety).
        At 904/914 - reconnect and try again."""
        cfg = self.config.config
        d = tinytuya.OutletDevice(
            dev_id=cfg["device_id"],
            address=self.config.host,
            local_key=cfg["local_key"],
            version=float(cfg.get("version", 3.5)),
        )
        d.set_socketPersistent(False)
        d.set_socketTimeout(timeout)
        try:
            result = d.updatedps([1])
            if isinstance(result, dict) and not self._is_error(result):
                return True
            # 904/914 - reset the socket and try again
            if isinstance(result, dict) and result.get("Err") in self._RETRYABLE_ERRORS:
                d.close()
                result = d.updatedps([1])
                return isinstance(result, dict) and not self._is_error(result)
            return False
        except Exception:
            return False
        finally:
            try:
                d.close()
            except Exception:
                pass

    async def is_reachable(self) -> bool:
        """Availability check via Tuya updatedps (instead of ICMP ping).

        updatedps([1]) - READ-ONLY command (0x12), does not switch the relay.
        Any valid Tuya response confirms that the device is reachable."""
        timeout = self.config.config.get("ping_timeout", CONNECT_TIMEOUT)
        retries = self.config.config.get("ping_count", 3)

        for attempt in range(retries):
            try:
                ok = await asyncio.wait_for(
                    asyncio.to_thread(self._ping_updatedps, timeout),
                    timeout=timeout + 2,
                )
                if ok:
                    return True
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

            if attempt < retries - 1:
                await asyncio.sleep(1)

        logger.info("[TUYA] %s is_reachable: FAIL after %d attempts", self.config.id, retries)
        return False
