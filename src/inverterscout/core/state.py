"""State Manager - event architecture for inverter monitoring.

EventBus: async pub/sub + request/response.
StateManager: owns states, detects transitions, emits events.
poll_loop: inverter polling cycle with confirmation of switching."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from inverterscout.inverter.luxpower import InverterData, read_inverter
from inverterscout.storage.encrypted import secure_json_path

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Event + EventBus
# ──────────────────────────────────────────────
@dataclass
class Event:
    """Event in the system."""

    type: str
    timestamp: float
    data: dict = field(default_factory=dict)
    _future: asyncio.Future | None = field(default=None, repr=False)

    def respond(self, result: Any) -> None:
        """Response to request (for request/response via EventBus)."""
        if self._future and not self._future.done():
            self._future.set_result(result)


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async pub/sub + request/response."""

    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscription to event type. '*' — subscription to everything."""
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Unsubscribing a handler from an event."""
        handlers = self._handlers.get(event_type, [])
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    async def emit(self, event: Event) -> None:
        """Broadcast: Call all subscribers sequentially."""
        # Specific subscribers
        for handler in self._handlers.get(event.type, []):
            try:
                await handler(event)
            except Exception as e:
                logger.error("Error in %s handler for %s: %s", handler.__name__, event.type, e)
        # Wildcard subscribers
        for handler in self._handlers.get("*", []):
            try:
                await handler(event)
            except Exception as e:
                logger.error("Error in wildcard handler %s: %s", handler.__name__, e)

    async def request(self, event_type: str, data: dict | None = None) -> Any:
        """Request/response: issues a request and waits for a response via Future."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        event = Event(
            type=event_type,
            timestamp=time.time(),
            data=data or {},
            _future=future,
        )
        await self.emit(event)
        if not future.done():
            raise RuntimeError(f"No handler responded to request: {event_type}")
        return future.result()


# ──────────────────────────────────────────────
# State dataclasses
# ──────────────────────────────────────────────
@dataclass
class BatteryState:
    on_battery: bool = False
    soc: int = 0
    battery_voltage: float = 0.0
    charging: bool = False
    discharging: bool = False
    charge_power: int = 0
    discharge_power: int = 0


@dataclass
class GridState:
    status: str = "grid_ok"  # grid_ok | grid_low_voltage | grid_off
    voltage: float = 0.0
    frequency: float = 0.0
    grid_lost_time: float = 0
    grid_restored_time: float = 0
    # Baseline for the “enough more” forecast when the generator is running:
    # last SOC and time measured while no_grid + gen_off.
    pre_gen_soc: int = 0
    pre_gen_time: float = 0


@dataclass
class GeneratorState:
    status: str = "gen_off"  # gen_off | gen_on
    voltage: float = 0.0
    frequency: float = 0.0
    power: int = 0
    gen_started_time: float = 0
    gen_stopped_time: float = 0


@dataclass
class HouseState:
    house_power: int = 0
    eps_power: int = 0
    grid_import: int = 0
    grid_export: int = 0


# Mapping power_source -> GridState.status
_GRID_STATUS_MAP = {
    "grid": "grid_ok",
    "low_voltage": "grid_low_voltage",
    "no_grid": "grid_off",
}


# ──────────────────────────────────────────────
# StateManager
# ──────────────────────────────────────────────
class StateManager:
    """Central state manager. Detects transitions and issues events."""

    STATE_FILE = secure_json_path("inverter.last_state")

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.battery = BatteryState()
        self.grid = GridState()
        self.generator = GeneratorState()
        self.house = HouseState()
        self.last_data: InverterData | None = None
        self.last_data_time: float = 0
        self._low_battery_notified: bool = False
        self._critical_battery_notified: bool = False
        # First reading: silently establishing a state without events
        # (similar to the old last_power_source is None logic)
        self._initialized: bool = False
        # Counters for diagnostics
        self.poll_count: int = 0
        self.error_count: int = 0
        self.last_error: str | None = None

        # Subscribe to get_status requests
        self.event_bus.subscribe("get_status", self.handle_get_status)

    async def handle_get_status(self, event: Event) -> None:
        """Request handler get_status - returns the current status."""
        event.respond(
            {
                "battery": self.battery,
                "grid": self.grid,
                "generator": self.generator,
                "house": self.house,
                "last_data": self.last_data,
                "last_data_time": self.last_data_time,
                "poll_count": self.poll_count,
                "error_count": self.error_count,
                "last_error": self.last_error,
            }
        )

    async def process_data(self, data: InverterData) -> None:
        """Apply one inverter reading and emit state-transition events."""
        self.last_data = data
        self.last_data_time = time.time()

        if not self._initialized:
            # Establish the initial state without emitting transition events.
            # A process restart must not emit a new outage notification.
            self._initialized = True
            self.grid.status = self._classify_grid(data)
            self.grid.voltage = data.grid_voltage
            self.grid.frequency = data.grid_frequency
            self.generator.status = "gen_on" if data.generator_on else "gen_off"
            self.generator.voltage = data.gen_voltage
            self.generator.frequency = data.gen_frequency
            self.generator.power = data.gen_power
            self.battery.on_battery = data.on_battery
            self.battery.soc = data.soc
            self.battery.battery_voltage = data.battery_voltage
            self.battery.charging = data.battery_charge > 0
            self.battery.discharging = data.battery_discharge > 0
            self.battery.charge_power = data.battery_charge
            self.battery.discharge_power = data.battery_discharge
            self._update_house_state(data)
            logger.info(
                "First reading - state set silently: grid=%s, gen=%s, soc=%d%%",
                self.grid.status,
                self.generator.status,
                data.soc,
            )
            self.save_state()
            await self.event_bus.emit(
                Event(type="data_updated", timestamp=self.last_data_time, data={})
            )
            return

        await self._update_grid_state(data)
        await self._update_generator_state(data)
        await self._update_battery_state(data)
        self._update_house_state(data)
        self._update_pre_gen_baseline(data)
        await self._check_battery_alerts(data)

        self.save_state()
        await self.event_bus.emit(
            Event(type="data_updated", timestamp=self.last_data_time, data={})
        )

    def _classify_grid(self, data: InverterData) -> str:
        """grid_ok / grid_low_voltage / grid_off"""
        return _GRID_STATUS_MAP.get(data.power_source, "grid_off")

    async def _update_grid_state(self, data: InverterData) -> None:
        """Update grid state and emit transition events."""
        new_status = self._classify_grid(data)
        old_status = self.grid.status

        self.grid.voltage = data.grid_voltage
        self.grid.frequency = data.grid_frequency

        if old_status == new_status:
            return

        self.grid.status = new_status
        now = time.time()

        # grid_ok/grid_low_voltage -> grid_off
        if new_status == "grid_off" and old_status in ("grid_ok", "grid_low_voltage"):
            # Preserve the first abnormal-grid timestamp for accurate outage duration.
            if old_status == "grid_ok" or self.grid.grid_lost_time <= 0:
                self.grid.grid_lost_time = now
            self._low_battery_notified = False
            self._critical_battery_notified = False
            await self.event_bus.emit(
                Event(
                    type="grid_lost",
                    timestamp=now,
                    data={
                        "voltage": data.grid_voltage,
                        "soc": data.soc,
                        "battery_voltage": data.battery_voltage,
                        "prev_status": old_status,
                    },
                )
            )

        # grid_off -> grid_ok/grid_low_voltage
        elif old_status == "grid_off" and new_status in ("grid_ok", "grid_low_voltage"):
            self.grid.grid_restored_time = now
            self.grid.pre_gen_soc = 0
            self.grid.pre_gen_time = 0
            self._low_battery_notified = False
            self._critical_battery_notified = False
            outage_seconds = (
                int(now - self.grid.grid_lost_time) if self.grid.grid_lost_time > 0 else 0
            )
            await self.event_bus.emit(
                Event(
                    type="grid_restored",
                    timestamp=now,
                    data={
                        "voltage": data.grid_voltage,
                        "soc": data.soc,
                        "outage_seconds": outage_seconds,
                        "generator_on": data.generator_on,
                    },
                )
            )

        # grid_ok -> grid_low_voltage
        elif old_status == "grid_ok" and new_status == "grid_low_voltage":
            self.grid.grid_lost_time = now
            self._low_battery_notified = False
            self._critical_battery_notified = False
            await self.event_bus.emit(
                Event(
                    type="grid_low_voltage",
                    timestamp=now,
                    data={
                        "voltage": data.grid_voltage,
                        "soc": data.soc,
                        "battery_voltage": data.battery_voltage,
                    },
                )
            )

        # grid_low_voltage -> grid_ok
        elif old_status == "grid_low_voltage" and new_status == "grid_ok":
            self.grid.grid_restored_time = now
            low_voltage_seconds = (
                int(now - self.grid.grid_lost_time) if self.grid.grid_lost_time > 0 else 0
            )
            await self.event_bus.emit(
                Event(
                    type="grid_voltage_normal",
                    timestamp=now,
                    data={
                        "voltage": data.grid_voltage,
                        "soc": data.soc,
                        "low_voltage_seconds": low_voltage_seconds,
                    },
                )
            )

    async def _update_generator_state(self, data: InverterData) -> None:
        """Update generator state and emit events."""
        new_on = data.generator_on
        old_status = self.generator.status

        self.generator.voltage = data.gen_voltage
        self.generator.frequency = data.gen_frequency
        self.generator.power = data.gen_power

        new_status = "gen_on" if new_on else "gen_off"
        if old_status == new_status:
            return

        self.generator.status = new_status
        now = time.time()

        if new_status == "gen_on":
            self.generator.gen_started_time = now
            await self.event_bus.emit(
                Event(
                    type="generator_started",
                    timestamp=now,
                    data={
                        "gen_voltage": data.gen_voltage,
                        "gen_frequency": data.gen_frequency,
                        "gen_power": data.gen_power,
                        "soc": data.soc,
                    },
                )
            )
        else:
            self.generator.gen_stopped_time = now
            run_seconds = (
                int(now - self.generator.gen_started_time)
                if self.generator.gen_started_time > 0
                else 0
            )
            await self.event_bus.emit(
                Event(
                    type="generator_stopped",
                    timestamp=now,
                    data={
                        "run_seconds": run_seconds,
                        "soc": data.soc,
                    },
                )
            )

    async def _update_battery_state(self, data: InverterData) -> None:
        """Battery status update + on_battery/off_battery events."""
        was_on_battery = self.battery.on_battery
        now_on_battery = data.on_battery

        self.battery.on_battery = now_on_battery
        self.battery.soc = data.soc
        self.battery.battery_voltage = data.battery_voltage
        self.battery.charging = data.battery_charge > 0
        self.battery.discharging = data.battery_discharge > 0
        self.battery.charge_power = data.battery_charge
        self.battery.discharge_power = data.battery_discharge

        if was_on_battery == now_on_battery:
            return

        now = time.time()

        if now_on_battery:
            # Switched to battery (no_grid)
            await self.event_bus.emit(
                Event(
                    type="on_battery",
                    timestamp=now,
                    data={
                        "soc": data.soc,
                        "battery_voltage": data.battery_voltage,
                        "grid_voltage": data.grid_voltage,
                        "generator_on": data.generator_on,
                    },
                )
            )
        else:
            # External power has returned from any supported source.
            await self.event_bus.emit(
                Event(
                    type="off_battery",
                    timestamp=now,
                    data={
                        "soc": data.soc,
                        "battery_voltage": data.battery_voltage,
                        "grid_voltage": data.grid_voltage,
                        "generator_on": data.generator_on,
                        "power_source": data.power_source,
                    },
                )
            )

    def _update_house_state(self, data: InverterData) -> None:
        """Updating the condition of the house."""
        self.house.house_power = data.house_power
        self.house.eps_power = data.eps_power
        self.house.grid_import = data.grid_power_import
        self.house.grid_export = data.grid_power_export

    def _update_pre_gen_baseline(self, data: InverterData) -> None:
        """Baseline update for the 'no more' forecast when the generator is running.

        While grid power is unavailable and the generator is off, retain the last known
        SOC and time. When the generator turns on, the forecast will be calculated according to rate
        this baseline period (i.e., as if the generator was now turned off)."""
        if (
            data.power_source == "no_grid"
            and not data.generator_on
            and self.grid.grid_lost_time > 0
        ):
            self.grid.pre_gen_soc = data.soc
            self.grid.pre_gen_time = self.last_data_time

    async def _check_battery_alerts(self, data: InverterData) -> None:
        """Emit each battery threshold alert once per grid outage."""
        if not data.on_battery:
            return

        now = time.time()

        if data.soc <= 15 and not self._critical_battery_notified:
            self._critical_battery_notified = True
            await self.event_bus.emit(
                Event(
                    type="battery_critical",
                    timestamp=now,
                    data={
                        "soc": data.soc,
                        "battery_voltage": data.battery_voltage,
                        "grid_lost_time": self.grid.grid_lost_time,
                        "generator_on": data.generator_on,
                        "pre_gen_soc": self.grid.pre_gen_soc,
                        "pre_gen_time": self.grid.pre_gen_time,
                    },
                )
            )
        elif data.soc <= 30 and not self._low_battery_notified:
            self._low_battery_notified = True
            await self.event_bus.emit(
                Event(
                    type="battery_low",
                    timestamp=now,
                    data={
                        "soc": data.soc,
                        "battery_voltage": data.battery_voltage,
                        "grid_lost_time": self.grid.grid_lost_time,
                        "generator_on": data.generator_on,
                        "pre_gen_soc": self.grid.pre_gen_soc,
                        "pre_gen_time": self.grid.pre_gen_time,
                    },
                )
            )

    def load_state(self) -> None:
        """Loading state from file (survives reboot)."""
        if not self.STATE_FILE.exists():
            logger.info("No saved state - first launch")
            return
        try:
            state = json.loads(self.STATE_FILE.read_text())
            # Grid
            ps = state.get("power_source")
            if ps:
                self.grid.status = _GRID_STATUS_MAP.get(ps, "grid_ok")
            self.grid.grid_lost_time = state.get("grid_lost_time", 0)
            self.grid.grid_restored_time = state.get("grid_restored_time", 0)
            self.grid.voltage = state.get("grid_voltage", 0)
            self.grid.frequency = state.get("grid_frequency", 0)
            self.grid.pre_gen_soc = state.get("pre_gen_soc", 0)
            self.grid.pre_gen_time = state.get("pre_gen_time", 0)
            # Battery
            self._low_battery_notified = state.get("low_battery_notified", False)
            self._critical_battery_notified = state.get("critical_battery_notified", False)
            self.battery.soc = state.get("soc", 0)
            self.battery.battery_voltage = state.get("battery_voltage", 0)
            # on_battery = True if grid_off (no_grid)
            self.battery.on_battery = self.grid.status == "grid_off"
            # Generator
            gen_status = state.get("generator_status", "gen_off")
            self.generator.status = gen_status
            self.generator.gen_started_time = state.get("gen_started_time", 0)
            self.generator.gen_stopped_time = state.get("gen_stopped_time", 0)

            # A loaded state is already initialized.
            self._initialized = True

            logger.info(
                "Loaded state: grid=%s, gen=%s (saved %.0f sec ago)",
                self.grid.status,
                self.generator.status,
                time.time() - state.get("timestamp", 0),
            )
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Error reading last_state.json: %s", e)

    def save_state(self) -> None:
        """Saving the current state to a file."""
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Backward compatible format (power_source retained for backward compatibility)
        ps_map = {"grid_ok": "grid", "grid_low_voltage": "low_voltage", "grid_off": "no_grid"}
        state = {
            "power_source": ps_map.get(self.grid.status, "grid"),
            "grid_voltage": round(self.grid.voltage, 1),
            "grid_frequency": round(self.grid.frequency, 2),
            "soc": self.battery.soc,
            "battery_voltage": round(self.battery.battery_voltage, 1),
            "battery_charge": self.battery.charge_power,
            "battery_discharge": self.battery.discharge_power,
            "grid_lost_time": self.grid.grid_lost_time,
            "grid_restored_time": self.grid.grid_restored_time,
            "pre_gen_soc": self.grid.pre_gen_soc,
            "pre_gen_time": self.grid.pre_gen_time,
            "low_battery_notified": self._low_battery_notified,
            "critical_battery_notified": self._critical_battery_notified,
            # Generator
            "generator_status": self.generator.status,
            "gen_started_time": self.generator.gen_started_time,
            "gen_stopped_time": self.generator.gen_stopped_time,
            # Inverter data
            "timestamp": time.time(),
        }

        if self.last_data:
            state.update(
                {
                    "status": self.last_data.status,
                    "status_name": self.last_data.status_name,
                    "pv1_power": self.last_data.pv1_power,
                    "pv2_power": self.last_data.pv2_power,
                    "total_pv_power": self.last_data.total_pv_power,
                    "grid_power_import": self.last_data.grid_power_import,
                    "grid_power_export": self.last_data.grid_power_export,
                    "eps_power": self.last_data.eps_power,
                }
            )

        self.STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# poll_loop — inverter polling cycle
# ──────────────────────────────────────────────
async def poll_loop(
    state_mgr: StateManager,
    host: str,
    port: int,
    interval: int,
) -> None:
    """Background inverter polling cycle (runs forever).

    - Reads data via read_inverter()
    - Confirms grid-state transitions with up to three additional reads
    - Generator: confirmation is NOT needed (hardware bit flip)
    - Exponential backoff for errors
    - Reading errors are an internal matter and are not emitted"""
    logger.info("Starting inverter polling: %s:%d every %ds", host, port, interval)
    state_mgr.load_state()

    consecutive_errors = 0

    while True:
        state_mgr.poll_count += 1
        try:
            data = await read_inverter(host, port)

            if data is not None:
                if not data.is_valid:
                    logger.warning("Invalid data (battery 0V/0%%) - skip")
                    consecutive_errors += 1
                    state_mgr.error_count += 1
                    state_mgr.last_error = "Invalid data from the inverter (zeros)"
                    wait = min(interval * 2, 300) if consecutive_errors > 3 else interval
                    await asyncio.sleep(wait)
                    continue

                consecutive_errors = 0
                state_mgr.last_error = None

                # Confirm a grid-state change with another read.
                old_grid = state_mgr.grid.status
                new_grid = state_mgr._classify_grid(data)

                if old_grid != new_grid:
                    logger.info("Network change detected: %s -> %s, confirm...", old_grid, new_grid)
                    confirmed_data = data
                    confirmed_grid = new_grid

                    for attempt in range(3):
                        await asyncio.sleep(5)
                        confirm_data = await read_inverter(host, port)
                        if confirm_data is None or not confirm_data.is_valid:
                            logger.warning("Confirmation attempt %d: invalid data", attempt + 1)
                            continue
                        confirmed_grid = state_mgr._classify_grid(confirm_data)
                        confirmed_data = confirm_data
                        break
                    else:
                        logger.warning(
                            "All three confirmations are invalid; ignoring the state change"
                        )
                        confirmed_grid = old_grid

                    if confirmed_grid != old_grid:
                        logger.info("Network change confirmed: %s -> %s", old_grid, confirmed_grid)
                        await state_mgr.process_data(confirmed_data)
                    else:
                        logger.info(
                            "Bounce: %s -> %s -> %s, ignore", old_grid, new_grid, confirmed_grid
                        )
                        # Keep fresh measurements without changing the grid state.
                        state_mgr.last_data = confirmed_data
                        state_mgr.last_data_time = time.time()
                        await state_mgr._update_battery_state(confirmed_data)
                        state_mgr._update_house_state(confirmed_data)
                        await state_mgr._update_generator_state(confirmed_data)
                        await state_mgr._check_battery_alerts(confirmed_data)
                        state_mgr.save_state()
                        await state_mgr.event_bus.emit(
                            Event(type="data_updated", timestamp=state_mgr.last_data_time, data={})
                        )
                else:
                    # No grid change - just processing (generator, battery, etc.)
                    await state_mgr.process_data(data)

                logger.debug(
                    "Status: grid=%s, generator=%s, grid_voltage=%.0fV, battery=%d%%, pv=%dW",
                    state_mgr.grid.status,
                    state_mgr.generator.status,
                    data.grid_voltage,
                    data.soc,
                    data.total_pv_power,
                )
            else:
                consecutive_errors += 1
                state_mgr.error_count += 1
                state_mgr.last_error = f"No data from inverter (attempt #{consecutive_errors})"
                logger.warning("Failed to retrieve data (error #%d)", consecutive_errors)

        except Exception as e:
            consecutive_errors += 1
            state_mgr.error_count += 1
            state_mgr.last_error = f"Error:{e}"
            logger.error("Error in poll_loop (#%d): %s", consecutive_errors, e)

        wait = min(interval * 2, 300) if consecutive_errors > 3 else interval
        await asyncio.sleep(wait)
