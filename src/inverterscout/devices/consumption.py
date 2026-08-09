"""Monitor enabled smart plugs for an unexpected zero-power condition.

Example: a battery-powered appliance connected through a Tapo P115 may stop
charging while the plug remains on, eventually interrupting dependent services.
The heuristic requires available grid power, an enabled socket, and near-zero
current for a configured window. The first alert power-cycles the plug once. A
second failed window sends another alert without further automatic action.

Device configuration fields:
  monitor_consumption: enable monitoring.
  consumption_threshold_w: no-load ceiling in watts (default 3 W).
  consumption_window_min: trigger window in minutes (default 30)."""

import asyncio
import json
import logging
import time
from typing import Any

from inverterscout.core.state import Event, EventBus
from inverterscout.devices.manager import DeviceManager
from inverterscout.storage.encrypted import secure_json_path

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_W = 3
DEFAULT_WINDOW_MIN = 30
POLL_INTERVAL_SEC = 60  # Power polling interval.
RECYCLE_PAUSE_SEC = 10  # Delay between power-off and power-on during automatic recycling.
STATE_FILE = secure_json_path("consumption.state")


class ConsumptionMonitor:
    """Background task: Tapo polling with monitor_consumption and 0-W-streak logic."""

    def __init__(
        self,
        event_bus: EventBus,
        device_manager: DeviceManager,
        notify_all,
        poll_interval_sec: int = POLL_INTERVAL_SEC,
        recycle_pause_sec: int = RECYCLE_PAUSE_SEC,
        state_file: Any | None = None,
    ):
        self.event_bus = event_bus
        self.dm = device_manager
        self.notify_all = notify_all
        self.poll_interval_sec = poll_interval_sec
        self.recycle_pause_sec = recycle_pause_sec
        self.state_file = state_file if state_file is not None else STATE_FILE
        # Per-device incident state:
        # no_load_since: first below-threshold reading, or 0 with no active incident
        # recycle_done: whether a power cycle was already attempted for this incident
        # second_alert_sent: whether the failed-recovery alert was already sent
        # last_power: most recent power reading for the UI and diagnostics
        # last_seen: timestamp of the most recent reading
        self.state: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._load_state()

    # ───────── lifecycle ─────────
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        logger.info("[CONSUMPTION] Background monitor started (poll=%ds)", self.poll_interval_sec)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    # ───────── main cycle ─────────
    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    logger.exception("[CONSUMPTION] tick error: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_sec)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        targets = self._monitored_devices()
        if not targets:
            return
        power_source = await self._current_power_source()
        for device_id, cfg in targets:
            await self._process_one(device_id, cfg, power_source)
        self._save_state()

    def _monitored_devices(self) -> list[tuple[str, Any]]:
        result = []
        for did, cfg in self.dm.devices.items():
            if not cfg.enabled:
                continue
            if cfg.config.get("monitor_consumption"):
                result.append((did, cfg))
        return result

    async def _current_power_source(self) -> str | None:
        try:
            status = await self.event_bus.request("get_status")
        except Exception:
            return None
        data = status.get("last_data") if status else None
        return data.power_source if data else None

    async def _process_one(self, device_id: str, cfg, power_source: str | None) -> None:
        threshold = int(cfg.config.get("consumption_threshold_w", DEFAULT_THRESHOLD_W))
        window_sec = int(cfg.config.get("consumption_window_min", DEFAULT_WINDOW_MIN)) * 60

        st = self.state.setdefault(
            device_id,
            {
                "no_load_since": 0,
                "recycle_done": False,
                "second_alert_sent": False,
                "last_power": None,
                "last_seen": 0,
            },
        )

        # Do not count streaks without grid power.
        if power_source != "grid":
            self._reset_streak(st)
            return

        # An intentionally disabled outlet cannot have a no-consumption incident.
        cached = self.dm.get_cached_state(device_id)
        if cached.on is False:
            self._reset_streak(st)
            return

        driver = self.dm.get_driver(device_id)
        if driver is None:
            return

        try:
            power = await driver.get_current_power_w()
        except Exception as e:
            logger.warning("[CONSUMPTION] %s: Power poll error: %s", device_id, e)
            return

        now = time.time()
        st["last_seen"] = now
        if power is None:
            return  # The driver or model does not expose energy monitoring.
        st["last_power"] = power

        if power >= threshold:
            if st["no_load_since"]:
                logger.info(
                    "[CONSUMPTION] %s: consumption returned (%dW) - streak reset", device_id, power
                )
            self._reset_streak(st)
            return

        if st["no_load_since"] == 0:
            st["no_load_since"] = now
            logger.info("[CONSUMPTION] %s: 0W streak has started", device_id)
            return

        elapsed = now - st["no_load_since"]
        if elapsed < window_sec:
            return

        # The no-consumption window expired; start recovery.
        if not st["recycle_done"]:
            await self._trigger_recycle(device_id, cfg, st, elapsed_sec=int(elapsed))
        elif not st["second_alert_sent"]:
            await self._trigger_second_alert(device_id, cfg, st, elapsed_sec=int(elapsed))

    def _reset_streak(self, st: dict[str, Any]) -> None:
        st["no_load_since"] = 0
        st["recycle_done"] = False
        st["second_alert_sent"] = False

    async def _trigger_recycle(
        self, device_id: str, cfg, st: dict[str, Any], elapsed_sec: int
    ) -> None:
        logger.warning(
            "[CONSUMPTION] %s: no consumption for %ds; starting automatic power cycle",
            device_id,
            elapsed_sec,
        )
        st["recycle_done"] = True
        # Start a fresh window to verify whether the power cycle restored consumption.
        st["no_load_since"] = time.time()
        self._save_state()

        await self.event_bus.emit(
            Event(
                type="device_no_consumption",
                timestamp=time.time(),
                data={
                    "device_id": device_id,
                    "device_name": cfg.name,
                    "elapsed_sec": elapsed_sec,
                },
            )
        )

        try:
            await self.dm.execute_command(
                device_id,
                "turn_off",
                source="consumption_monitor",
                source_detail="auto recycle",
            )
            await asyncio.sleep(self.recycle_pause_sec)
            await self.dm.execute_command(
                device_id,
                "turn_on",
                source="consumption_monitor",
                source_detail="auto recycle",
            )
        except Exception as e:
            logger.error("[CONSUMPTION] %s: auto-flick error: %s", device_id, e)

    async def _trigger_second_alert(
        self, device_id: str, cfg, st: dict[str, Any], elapsed_sec: int
    ) -> None:
        logger.warning("[CONSUMPTION] %s: after clicking again 0 - repeated alert", device_id)
        st["second_alert_sent"] = True
        self._save_state()
        await self.event_bus.emit(
            Event(
                type="device_no_consumption_after_recycle",
                timestamp=time.time(),
                data={
                    "device_id": device_id,
                    "device_name": cfg.name,
                    "elapsed_sec": elapsed_sec,
                },
            )
        )

    # ───────── persistence ─────────
    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            self.state = json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("[CONSUMPTION] Error reading %s: %s", self.state_file, e)
            self.state = {}

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error("[CONSUMPTION] Error writing %s: %s", self.state_file, e)

    # ───────── helpers for UI ─────────
    def get_last_power(self, device_id: str) -> int | None:
        return (self.state.get(device_id) or {}).get("last_power")

    def get_streak_seconds(self, device_id: str) -> int:
        st = self.state.get(device_id) or {}
        if not st.get("no_load_since"):
            return 0
        return max(0, int(time.time() - st["no_load_since"]))
