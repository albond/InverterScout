"""Provider-neutral smart-device management.

Device providers implement the DeviceDriver interface. DeviceManager owns the
registry and verified command execution. StubDriver supplies an in-memory test
implementation."""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from inverterscout.core.state import Event, EventBus
from inverterscout.storage.encrypted import secure_json_path

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────
@dataclass
class DeviceState:
    """The current state of the device."""

    online: bool = False
    on: bool | None = None
    level: int | None = None


@dataclass
class DeviceConfig:
    """Device configuration from devices.json."""

    id: str
    name: str
    provider: str  # stub, tuya, tapo, ...
    device_type: str = "switch"  # switch, dimmer, ...
    host: str = ""
    config: dict = field(default_factory=dict)
    enabled: bool = True
    test_mode: bool = False  # True = notifications to admin only (for stub/tests)


# ──────────────────────────────────────────────
# DeviceDriver ABC
# ──────────────────────────────────────────────
class DeviceDriver(ABC):
    """Abstract device driver interface."""

    def __init__(self, device_config: DeviceConfig):
        self.config = device_config

    @abstractmethod
    async def turn_on(self) -> bool:
        """Turn on the device. Returns True if the command was sent."""

    @abstractmethod
    async def turn_off(self) -> bool:
        """Turn off the device. Returns True if the command was sent."""

    @abstractmethod
    async def get_state(self) -> DeviceState | None:
        """Request the actual status of the device. None = no data (not offline)."""

    @abstractmethod
    async def set_level(self, level: int) -> bool:
        """Set the level (brightness, power). 0-100."""

    async def get_current_power_w(self) -> int | None:
        """Instantaneous load power (W). Not supported by all drivers/models.
        Returns None if not supported or failed to read."""
        return None

    async def is_reachable(self) -> bool:
        """Check network reachability with up to ``ping_count`` ICMP packets."""
        host = self.config.host
        if not host:
            return False
        timeout = self.config.config.get("ping_timeout", 4)
        count = self.config.config.get("ping_count", 3)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c",
                str(count),
                "-W",
                str(timeout),
                "-s",
                "1",
                host,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except Exception:
            return False


# ──────────────────────────────────────────────
# In-memory test driver
# ──────────────────────────────────────────────
class StubDriver(DeviceDriver):
    """Stub driver for testing (runs in memory)."""

    def __init__(self, device_config: DeviceConfig):
        super().__init__(device_config)
        self._on: bool = False
        self._level: int = 100
        self._online: bool = True
        # Number of consecutive stale states returned by failure-injection tests.
        self._fail_count: int = device_config.config.get("fail_count", 0)
        self._current_fails: int = 0

    async def turn_on(self) -> bool:
        if not self._online:
            return False
        self._on = True
        self._current_fails = self._fail_count
        logger.info("[STUB] %s: turn_on", self.config.id)
        return True

    async def turn_off(self) -> bool:
        if not self._online:
            return False
        self._on = False
        self._current_fails = self._fail_count
        logger.info("[STUB] %s: turn_off", self.config.id)
        return True

    async def get_state(self) -> DeviceState:
        if not self._online:
            return DeviceState(online=False)
        # Failure injection: return the previous state for the first N checks.
        if self._current_fails > 0:
            self._current_fails -= 1
            return DeviceState(online=True, on=not self._on, level=self._level)
        return DeviceState(online=True, on=self._on, level=self._level)

    async def set_level(self, level: int) -> bool:
        if not self._online:
            return False
        self._level = max(0, min(100, level))
        self._current_fails = self._fail_count
        logger.info("[STUB] %s: set_level(%d)", self.config.id, self._level)
        return True

    async def is_reachable(self) -> bool:
        return self._online


# ──────────────────────────────────────────────
# Provider registry
# ──────────────────────────────────────────────
_DRIVER_REGISTRY: dict[str, type[DeviceDriver]] = {
    "stub": StubDriver,
}


def register_driver(provider: str, driver_class: type[DeviceDriver]) -> None:
    """Register a device-provider implementation."""
    _DRIVER_REGISTRY[provider] = driver_class


# ──────────────────────────────────────────────
# DeviceManager
# ──────────────────────────────────────────────
DEVICES_FILE = secure_json_path("devices")
EVENTS_FILE = secure_json_path("device.events")
MAX_EVENTS = 500

# retry options
MAX_RETRIES = 5
VERIFY_DELAY = 3  # seconds between command and check
RETRY_PAUSE = 5  # seconds between attempts


class DeviceManager:
    """Device registry with retried and verified command execution."""

    def __init__(self, event_bus: EventBus, notify_all, notify_admin=None):
        self.event_bus = event_bus
        self.notify_all = notify_all
        self.notify_admin = notify_admin  # for test_mode devices
        self.devices: dict[str, DeviceConfig] = {}
        self.drivers: dict[str, DeviceDriver] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_states: dict[str, DeviceState] = {}  # device_id → state cache
        self._last_state_times: dict[str, float] = {}  # device_id → monotonic timestamp
        self._load_devices()
        self.event_bus.subscribe("device_command", self._on_device_command)

    def _load_devices(self) -> None:
        """Load devices from devices.json."""
        if not DEVICES_FILE.exists():
            logger.info("No device configuration is available")
            return
        try:
            raw = json.loads(DEVICES_FILE.read_text())
            for item in raw:
                provider = item.get("provider", "stub")
                # Stub devices default to test mode unless explicitly configured otherwise.
                test_mode = item.get("test_mode", provider == "stub")
                cfg = DeviceConfig(
                    id=item["id"],
                    name=item["name"],
                    provider=provider,
                    device_type=item.get("device_type", "switch"),
                    host=item.get("host", ""),
                    config=item.get("config", {}),
                    enabled=item.get("enabled", True),
                    test_mode=test_mode,
                )
                self.devices[cfg.id] = cfg
                self._locks[cfg.id] = asyncio.Lock()
                # Construct the provider driver.
                driver_cls = _DRIVER_REGISTRY.get(cfg.provider)
                if driver_cls:
                    self.drivers[cfg.id] = driver_cls(cfg)
                else:
                    logger.warning("Unknown provider '%s' for device '%s'", cfg.provider, cfg.id)
            logger.info("Loaded %d devices", len(self.devices))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("Error reading devices.json: %s", e)

    def get_device(self, device_id: str) -> DeviceConfig | None:
        return self.devices.get(device_id)

    def get_driver(self, device_id: str) -> DeviceDriver | None:
        return self.drivers.get(device_id)

    def get_cached_state(self, device_id: str) -> DeviceState:
        """Get cached state (instantly, without network)."""
        return self._last_states.get(device_id, DeviceState(online=False))

    def is_cache_fresh(self, device_id: str, max_age: float = 60.0) -> bool:
        """Is the cache fresh (not older than max_age seconds)?"""
        ts = self._last_state_times.get(device_id)
        if ts is None:
            return False
        return (time.monotonic() - ts) <= max_age

    def get_cached_state_age(self, device_id: str) -> float | None:
        """Cache age in seconds. None if there is no cache."""
        ts = self._last_state_times.get(device_id)
        if ts is None:
            return None
        return time.monotonic() - ts

    STATE_RETRIES = 3
    STATE_RETRY_PAUSE = 2

    async def get_device_state(self, device_id: str) -> DeviceState | None:
        """Read a device state with retries for incomplete provider responses."""
        driver = self.drivers.get(device_id)
        if not driver:
            logger.warning("[STATE] %s: no driver, skip", device_id)
            return None
        logger.info(
            "[STATE] %s: requesting on/off (up to %d attempts)...", device_id, self.STATE_RETRIES
        )
        t0 = time.monotonic()
        for attempt in range(1, self.STATE_RETRIES + 1):
            try:
                state = await driver.get_state()
                elapsed = int((time.monotonic() - t0) * 1000)
                if state is None or not state.online or state.on is None:
                    # Retry missing, offline, timeout, and partial-DPS responses.
                    if state is None:
                        reason = "None (904?)"
                    elif not state.online:
                        reason = "offline"
                    else:
                        reason = "partial (on=None)"
                    logger.info(
                        "[STATE] %s: attempt %d/%d returned %s after %dms",
                        device_id,
                        attempt,
                        self.STATE_RETRIES,
                        reason,
                        elapsed,
                    )
                    if attempt < self.STATE_RETRIES:
                        await asyncio.sleep(self.STATE_RETRY_PAUSE)
                    continue
                # A complete response confirms both reachability and relay state.
                merged = DeviceState(online=True, on=state.on, level=state.level)
                self._last_states[device_id] = merged
                self._last_state_times[device_id] = time.monotonic()
                logger.info(
                    "[STATE] %s: OK on=%s (attempt %d, %dms)", device_id, state.on, attempt, elapsed
                )
                return merged
            except Exception as e:
                elapsed = int((time.monotonic() - t0) * 1000)
                logger.error(
                    "[STATE] %s: attempt %d/%d failed with %s after %dms",
                    device_id,
                    attempt,
                    self.STATE_RETRIES,
                    e,
                    elapsed,
                )
                if attempt < self.STATE_RETRIES:
                    await asyncio.sleep(self.STATE_RETRY_PAUSE)
        # Every state-query attempt failed.
        total_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            "[STATE] %s: all %d attempts failed (%dms), offline",
            device_id,
            self.STATE_RETRIES,
            total_ms,
        )
        return DeviceState(online=False)

    async def _on_device_command(self, event: Event) -> None:
        """Dispatch a device command event without blocking the event bus."""
        data = event.data or {}
        device_id = data.get("device_id", "")
        action = data.get("action", "")
        params = data.get("params") or {}
        source = data.get("source", "")
        source_detail = data.get("source_detail", "")
        asyncio.create_task(
            self._execute_command_task(device_id, action, params, source, source_detail)
        )

    async def _execute_command_task(
        self, device_id: str, action: str, params: dict, source: str, source_detail: str
    ) -> None:
        """Execute a background command and contain unexpected exceptions."""
        try:
            await self.execute_command(
                device_id, action, params, source=source, source_detail=source_detail
            )
        except Exception as e:
            logger.error("[%s] Error in _execute_command_task: %s", device_id, e)

    async def execute_command(
        self,
        device_id: str,
        action: str,
        params: dict | None = None,
        source: str = "",
        source_detail: str = "",
    ) -> bool:
        """Execute a command with retry and state verification.

        action: turn_on, turn_off, set_level
        source: "web", "scenario", "detected"
        Returns True if the command is confirmed."""
        cfg = self.devices.get(device_id)
        driver = self.drivers.get(device_id)
        if not cfg or not driver:
            logger.error("Device '%s' not found", device_id)
            return False

        if not cfg.enabled:
            logger.info("Device '%s' is disabled, skip", device_id)
            return False

        lock = self._locks.get(device_id)
        if not lock:
            lock = asyncio.Lock()
            self._locks[device_id] = lock

        async with lock:
            return await self._execute_with_retry(
                device_id, driver, cfg, action, params, source, source_detail
            )

    async def _execute_with_retry(
        self,
        device_id: str,
        driver: DeviceDriver,
        cfg: DeviceConfig,
        action: str,
        params: dict | None,
        source: str = "",
        source_detail: str = "",
    ) -> bool:
        """Internal retry/verify loop."""
        params = params or {}

        # Skip the command if the device already has the desired state.
        try:
            pre_state = await driver.get_state()
            if pre_state and self._verify_state(pre_state, action, params):
                logger.info(
                    "[%s] Already in the desired state, %s is not required", device_id, action
                )
                self._last_states[device_id] = pre_state
                self._last_state_times[device_id] = time.monotonic()
                return True
        except Exception:
            pass  # A failed pre-check must not prevent the command attempt.

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Send the provider command.
                cmd_ok = await self._send_command(driver, action, params)
                if not cmd_ok:
                    logger.warning(
                        "[%s] Attempt %d/%d: %s command not sent",
                        device_id,
                        attempt,
                        MAX_RETRIES,
                        action,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_PAUSE)
                    continue

                # Allow the physical device to apply the command.
                await asyncio.sleep(VERIFY_DELAY)

                # Verify the resulting state.
                state = await driver.get_state()
                if state and self._verify_state(state, action, params):
                    self._last_states[device_id] = state
                    self._last_state_times[device_id] = time.monotonic()
                    logger.info(
                        "[%s] Command %s confirmed (attempt %d)", device_id, action, attempt
                    )
                    await self.event_bus.emit(
                        Event(
                            type="device_command_ok",
                            timestamp=time.time(),
                            data={
                                "device_id": device_id,
                                "device_name": cfg.name,
                                "action": action,
                                "attempt": attempt,
                                "source": source,
                                "source_detail": source_detail,
                            },
                        )
                    )
                    self._log_device_event(device_id, cfg.name, action, True, source, source_detail)
                    return True

                logger.warning(
                    "[%s] Attempt %d/%d: status not confirmed (on=%s, level=%s)",
                    device_id,
                    attempt,
                    MAX_RETRIES,
                    state.on,
                    state.level,
                )

            except Exception as e:
                logger.error("[%s] Trying %d/%d: error - %s", device_id, attempt, MAX_RETRIES, e)

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_PAUSE)

        # Every command attempt failed verification.
        action_text = {
            "turn_off": "turn off",
            "turn_on": "turn on",
            "set_level": "change level",
        }.get(action, action)

        logger.error("[%s] ALL %d ATTEMPTS FAILED for %s", device_id, MAX_RETRIES, action)

        await self.event_bus.emit(
            Event(
                type="device_command_failed",
                timestamp=time.time(),
                data={
                    "device_id": device_id,
                    "device_name": cfg.name,
                    "action": action,
                    "source": source,
                    "source_detail": source_detail,
                },
            )
        )
        self._log_device_event(device_id, cfg.name, action, False, source, source_detail)

        alert = f"*Failed to {action_text} {cfg.name}!*\nCheck the device manually."
        if cfg.test_mode and self.notify_admin:
            await self.notify_admin(alert)
        else:
            await self.notify_all(alert)
        return False

    @staticmethod
    async def _send_command(driver: DeviceDriver, action: str, params: dict) -> bool:
        """Send a command to the driver."""
        if action == "turn_on":
            return await driver.turn_on()
        elif action == "turn_off":
            return await driver.turn_off()
        elif action == "set_level":
            level = params.get("level", 100)
            return await driver.set_level(level)
        else:
            logger.error("Unknown action: %s", action)
            return False

    @staticmethod
    def _verify_state(state: DeviceState, action: str, params: dict) -> bool:
        """Check that the device is in the expected condition."""
        if not state.online:
            return False
        if action == "turn_on":
            return state.on is True
        elif action == "turn_off":
            return state.on is False
        elif action == "set_level":
            expected = params.get("level", 100)
            return state.level == expected
        return False

    def add_device(
        self,
        device_id: str,
        name: str,
        provider: str,
        host: str = "",
        config: dict = None,
        device_type: str = "switch",
        test_mode: bool = False,
    ) -> bool:
        """Add a new device programmatically. Returns True on success."""
        if device_id in self.devices:
            logger.warning("Device '%s' already exists", device_id)
            return False

        driver_cls = _DRIVER_REGISTRY.get(provider)
        if not driver_cls:
            logger.error("Unknown provider '%s'", provider)
            return False

        cfg = DeviceConfig(
            id=device_id,
            name=name,
            provider=provider,
            device_type=device_type,
            host=host,
            config=config or {},
            enabled=True,
            test_mode=test_mode,
        )
        self.devices[cfg.id] = cfg
        self.drivers[cfg.id] = driver_cls(cfg)
        self._locks[cfg.id] = asyncio.Lock()
        self._save_devices()
        logger.info("Device '%s' (%s) added", name, device_id)
        return True

    def rename_device(self, device_id: str, new_name: str) -> bool:
        """Rename the device + save to devices.json."""
        cfg = self.devices.get(device_id)
        if not cfg:
            return False
        cfg.name = new_name
        self._save_devices()
        return True

    def set_monitor_consumption(self, device_id: str, enabled: bool) -> bool:
        """Enable/disable consumption monitoring for the device (Tapo P110/P115).

        When turned on, the default parameters are set (3 W / 30 min), if they are not already there.
        Saves devices.json. Returns True on success."""
        cfg = self.devices.get(device_id)
        if not cfg:
            return False
        if enabled:
            cfg.config["monitor_consumption"] = True
            cfg.config.setdefault("consumption_threshold_w", 3)
            cfg.config.setdefault("consumption_window_min", 30)
        else:
            cfg.config["monitor_consumption"] = False
        logger.info("Device '%s': consumption monitoring = %s", cfg.name, enabled)
        self._save_devices()
        return True

    def set_device_enabled(self, device_id: str, enabled: bool) -> bool:
        """Enable/disable the device + save to devices.json."""
        cfg = self.devices.get(device_id)
        if not cfg:
            return False
        cfg.enabled = enabled
        logger.info("Device '%s': %s", cfg.name, "activated" if enabled else "deactivated")
        self._save_devices()
        return True

    def _save_devices(self) -> None:
        """Save the current state of devices in devices.json."""
        DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for cfg in self.devices.values():
            data.append(
                {
                    "id": cfg.id,
                    "name": cfg.name,
                    "provider": cfg.provider,
                    "device_type": cfg.device_type,
                    "host": cfg.host,
                    "config": cfg.config,
                    "enabled": cfg.enabled,
                    "test_mode": cfg.test_mode,
                }
            )
        DEVICES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # ──────────────────────────────────────────────
    # Device event log
    # ──────────────────────────────────────────────
    def _log_device_event(
        self,
        device_id: str,
        device_name: str,
        action: str,
        ok: bool,
        source: str = "",
        details: str = "",
    ) -> None:
        """Add an entry to data/device_events.json (FIFO, max MAX_EVENTS)."""
        now = time.time()
        entry = {
            "time": datetime.fromtimestamp(now).strftime("%d.%m %H:%M:%S"),
            "ts": int(now),
            "device_id": device_id,
            "device_name": device_name,
            "action": action,
            "ok": ok,
            "source": source,
            "details": details,
        }
        try:
            EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            events: list[dict] = []
            if EVENTS_FILE.exists():
                events = json.loads(EVENTS_FILE.read_text())
            events.append(entry)
            if len(events) > MAX_EVENTS:
                events = events[-MAX_EVENTS:]
            EVENTS_FILE.write_text(json.dumps(events, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error("Error writing device_events.json: %s", e)

    @staticmethod
    def load_events() -> list[dict]:
        """Load the event history for the Web UI."""
        if not EVENTS_FILE.exists():
            return []
        try:
            return json.loads(EVENTS_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return []

    def reload_from_file(self) -> dict:
        """Re-read devices.json. Returns {added, removed, updated}."""
        if not DEVICES_FILE.exists():
            logger.warning("reload_from_file: devices.json file not found")
            return {"added": [], "removed": [], "updated": []}

        try:
            raw = json.loads(DEVICES_FILE.read_text())
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("reload_from_file: error reading devices.json: %s", e)
            return {"added": [], "removed": [], "updated": []}

        # Parse the new configuration.
        new_configs: dict[str, DeviceConfig] = {}
        for item in raw:
            provider = item.get("provider", "stub")
            test_mode = item.get("test_mode", provider == "stub")
            cfg = DeviceConfig(
                id=item["id"],
                name=item["name"],
                provider=provider,
                device_type=item.get("device_type", "switch"),
                host=item.get("host", ""),
                config=item.get("config", {}),
                enabled=item.get("enabled", True),
                test_mode=test_mode,
            )
            new_configs[cfg.id] = cfg

        old_ids = set(self.devices.keys())
        new_ids = set(new_configs.keys())

        added_ids = sorted(new_ids - old_ids)
        removed_ids = sorted(old_ids - new_ids)
        updated_ids = []

        # Remove devices absent from the new configuration.
        for did in removed_ids:
            del self.devices[did]
            self.drivers.pop(did, None)
            self._locks.pop(did, None)
            self._last_states.pop(did, None)
            self._last_state_times.pop(did, None)
            logger.info("reload: device '%s' removed", did)

        # Add newly configured devices.
        for did in added_ids:
            cfg = new_configs[did]
            self.devices[cfg.id] = cfg
            self._locks[cfg.id] = asyncio.Lock()
            driver_cls = _DRIVER_REGISTRY.get(cfg.provider)
            if driver_cls:
                self.drivers[cfg.id] = driver_cls(cfg)
            else:
                logger.warning("reload: unknown provider '%s' for '%s'", cfg.provider, cfg.id)
            logger.info("reload: device '%s' added", did)

        # Update existing devices.
        for did in old_ids & new_ids:
            old_cfg = self.devices[did]
            new_cfg = new_configs[did]
            # Identify changes that require driver reconstruction.
            changed = (
                old_cfg.name != new_cfg.name
                or old_cfg.provider != new_cfg.provider
                or old_cfg.host != new_cfg.host
                or old_cfg.config != new_cfg.config
                or old_cfg.enabled != new_cfg.enabled
                or old_cfg.test_mode != new_cfg.test_mode
            )
            if changed:
                updated_ids.append(did)
                # Recreate the driver only when provider configuration changed.
                driver_changed = (
                    old_cfg.provider != new_cfg.provider
                    or old_cfg.host != new_cfg.host
                    or old_cfg.config != new_cfg.config
                )
                self.devices[did] = new_cfg
                if driver_changed:
                    driver_cls = _DRIVER_REGISTRY.get(new_cfg.provider)
                    if driver_cls:
                        self.drivers[did] = driver_cls(new_cfg)
                    else:
                        self.drivers.pop(did, None)
                    self._last_states.pop(did, None)
                    self._last_state_times.pop(did, None)
                logger.info("reload: device '%s' updated (driver_changed=%s)", did, driver_changed)

        updated_ids.sort()
        logger.info(
            "reload: added=%d, deleted=%d, updated=%d",
            len(added_ids),
            len(removed_ids),
            len(updated_ids),
        )
        return {"added": added_ids, "removed": removed_ids, "updated": updated_ids}

    def list_devices(self) -> list[dict]:
        """List of devices for the web interface."""
        result = []
        for did, cfg in self.devices.items():
            result.append(
                {
                    "id": cfg.id,
                    "name": cfg.name,
                    "provider": cfg.provider,
                    "device_type": cfg.device_type,
                    "enabled": cfg.enabled,
                    "test_mode": cfg.test_mode,
                }
            )
        return result
