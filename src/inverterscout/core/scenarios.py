"""Scenario Engine - smart home scenario engine.

Subscribes to EventBus events and performs actions through DeviceManager.
Supports rollback timers with persistence (survives reboots)."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from inverterscout.core.state import Event, EventBus
from inverterscout.storage.encrypted import secure_json_path

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────
@dataclass
class ScenarioAction:
    """One action per script."""

    device_id: str
    action: str  # turn_on, turn_off, set_level
    params: dict = field(default_factory=dict)


@dataclass
class ScenarioRule:
    """Scenario rule from scenarios.json."""

    id: str
    name: str
    trigger_event: str
    actions: list[ScenarioAction]
    revert_after_seconds: int = 0
    revert_actions: list[ScenarioAction] = field(default_factory=list)
    cancel_event: str = ""
    priority: int = 0
    enabled: bool = True


# ──────────────────────────────────────────────
# ScenarioEngine
# ──────────────────────────────────────────────
SCENARIOS_FILE = secure_json_path("scenarios")
TIMERS_FILE = secure_json_path("scenario.timers")


class ScenarioEngine:
    """Scripting engine: event → actions, timers, rollback."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.rules: list[ScenarioRule] = []
        # Active timers: scenario_id → asyncio.Task
        self._timer_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        # Timer metadata for persistence
        self._timer_meta: dict[str, dict] = {}
        # Tracking EventBus subscriptions (subscription is idempotent, unsubscribe is not)
        self._subscribed_triggers: set[str] = set()
        self._subscribed_cancels: set[str] = set()

        self._load_scenarios()
        self._subscribe_events()
        self._restore_timers()
        self.event_bus.subscribe("device_command_failed", self._on_command_failed)

    def _load_scenarios(self) -> None:
        """Load scenarios from scenarios.json."""
        if not SCENARIOS_FILE.exists():
            logger.info("No scenarios.json file - no scenarios loaded")
            return
        try:
            raw = json.loads(SCENARIOS_FILE.read_text())
            for item in raw:
                actions = [
                    ScenarioAction(
                        device_id=a["device_id"],
                        action=a["action"],
                        params=a.get("params", {}),
                    )
                    for a in item.get("actions", [])
                ]
                revert_actions = [
                    ScenarioAction(
                        device_id=a["device_id"],
                        action=a["action"],
                        params=a.get("params", {}),
                    )
                    for a in item.get("revert_actions", [])
                ]
                rule = ScenarioRule(
                    id=item["id"],
                    name=item["name"],
                    trigger_event=item["trigger_event"],
                    actions=actions,
                    revert_after_seconds=item.get("revert_after_seconds", 0),
                    revert_actions=revert_actions,
                    cancel_event=item.get("cancel_event", ""),
                    priority=item.get("priority", 0),
                    enabled=item.get("enabled", True),
                )
                self.rules.append(rule)
            logger.info("Loaded %d scripts", len(self.rules))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("Error reading scenarios.json: %s", e)

    def _subscribe_events(self) -> None:
        """Subscribe to all unique trigger_event and cancel_event.

        Safe to re-invoke - only subscribes to new events."""
        trigger_events: set[str] = set()
        cancel_events: set[str] = set()

        for rule in self.rules:
            if rule.enabled:
                trigger_events.add(rule.trigger_event)
                if rule.cancel_event:
                    cancel_events.add(rule.cancel_event)

        # Subscribe to new trigger events
        for ev in trigger_events - self._subscribed_triggers:
            self.event_bus.subscribe(ev, self._on_trigger_event)
        self._subscribed_triggers |= trigger_events

        # Subscribe to new cancel events (only those not covered by trigger)
        for ev in cancel_events - self._subscribed_cancels - self._subscribed_triggers:
            self.event_bus.subscribe(ev, self._on_cancel_event)
        self._subscribed_cancels |= cancel_events

        logger.info(
            "Subscriptions: triggers=%s, cancels=%s",
            self._subscribed_triggers,
            self._subscribed_cancels,
        )

    async def _dispatch_command(self, action: ScenarioAction, source_detail: str) -> None:
        """Send a command to the device via EventBus (parallel execution)."""
        await self.event_bus.emit(
            Event(
                type="device_command",
                timestamp=time.time(),
                data={
                    "device_id": action.device_id,
                    "action": action.action,
                    "params": action.params,
                    "source": "scenario",
                    "source_detail": source_detail,
                },
            )
        )

    async def _on_command_failed(self, event: Event) -> None:
        """Logging command errors from scripts."""
        data = event.data or {}
        if data.get("source") == "scenario":
            logger.warning(
                "Script '%s': Command %s for %s failed",
                data.get("source_detail", "?"),
                data.get("action", "?"),
                data.get("device_id", "?"),
            )

    async def _on_trigger_event(self, event: Event) -> None:
        """Trigger event processing."""
        event_type = event.type

        # 1. First cancel the timers with cancel_event == event_type
        await self._cancel_timers_for_event(event_type)

        # 2. Collect the rules for this trigger, sort by priority (more = earlier)
        matching = [r for r in self.rules if r.trigger_event == event_type and r.enabled]
        matching.sort(key=lambda r: r.priority, reverse=True)

        if not matching:
            return

        logger.info("Event '%s': %d scripts", event_type, len(matching))

        for rule in matching:
            await self._execute_rule(rule)

    async def _on_cancel_event(self, event: Event) -> None:
        """Handling a cancellation event (for cancel_event not matching trigger)."""
        await self._cancel_timers_for_event(event.type)

    async def _cancel_timers_for_event(self, event_type: str) -> None:
        """Cancel timers and perform revert_actions on rules with cancel_event."""
        to_cancel = [
            r for r in self.rules if r.cancel_event == event_type and r.id in self._timer_tasks
        ]
        for rule in to_cancel:
            logger.info("Canceling script timer '%s' on event '%s'", rule.name, event_type)
            task = self._timer_tasks.pop(rule.id, None)
            if task and not task.done():
                task.cancel()
            self._timer_meta.pop(rule.id, None)

            # Execute revert_actions
            if rule.revert_actions:
                logger.info("Executing rollback actions for '%s'", rule.name)
                await asyncio.gather(
                    *(
                        self._dispatch_command(a, f"{rule.name}(cancel)")
                        for a in rule.revert_actions
                    )
                )

        if to_cancel:
            self._save_timers()

    async def _execute_rule(self, rule: ScenarioRule) -> None:
        """Execute the actions of one rule."""
        logger.info("Executing scenario '%s'", rule.name)

        await asyncio.gather(*(self._dispatch_command(a, rule.name) for a in rule.actions))

        # Start rollback timer if specified
        if rule.revert_after_seconds > 0 and rule.revert_actions:
            self._start_timer(rule)

    def _start_timer(self, rule: ScenarioRule, remaining: int = 0) -> None:
        """Start the fallback timer for the rule."""
        # Cancel the previous timer if there is one
        old_task = self._timer_tasks.pop(rule.id, None)
        if old_task and not old_task.done():
            old_task.cancel()

        delay = remaining if remaining > 0 else rule.revert_after_seconds
        fires_at = time.time() + delay

        self._timer_meta[rule.id] = {
            "started_at": time.time(),
            "fires_at": fires_at,
        }
        self._save_timers()

        task = asyncio.create_task(self._timer_worker(rule, delay))
        self._timer_tasks[rule.id] = task
        logger.info("Timer '%s': %d sec (rollback to %.0f)", rule.name, delay, fires_at)

    async def _timer_worker(self, rule: ScenarioRule, delay: int) -> None:
        """Background task - waits delay seconds and performs revert_actions."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("Timer '%s' canceled", rule.name)
            return

        logger.info("Timer '%s' expired; executing rollback actions", rule.name)

        await self.event_bus.emit(
            Event(
                type="scenario_timer_fired",
                timestamp=time.time(),
                data={"scenario_id": rule.id, "scenario_name": rule.name},
            )
        )

        await asyncio.gather(
            *(self._dispatch_command(a, f"{rule.name}(timer)") for a in rule.revert_actions)
        )

        # Cleaning
        self._timer_tasks.pop(rule.id, None)
        self._timer_meta.pop(rule.id, None)
        self._save_timers()

    def _save_timers(self) -> None:
        """Save timers metadata to a file."""
        TIMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TIMERS_FILE.write_text(json.dumps(self._timer_meta, ensure_ascii=False, indent=2))

    def _restore_timers(self) -> None:
        """Restore timers after reboot."""
        if not TIMERS_FILE.exists():
            return
        try:
            saved = json.loads(TIMERS_FILE.read_text())
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Error reading device_state.json: %s", e)
            return

        now = time.time()
        rules_by_id = {r.id: r for r in self.rules}

        for scenario_id, meta in saved.items():
            rule = rules_by_id.get(scenario_id)
            if not rule or not rule.enabled:
                continue

            fires_at = meta.get("fires_at", 0)
            if fires_at <= 0:
                continue

            remaining = int(fires_at - now)

            if remaining > 0:
                # The timer has not yet expired - restore it
                logger.info("Restoring the timer '%s': %d seconds left", rule.name, remaining)
                self._timer_meta[scenario_id] = meta
                task = asyncio.create_task(self._timer_worker(rule, remaining))
                self._timer_tasks[scenario_id] = task
            else:
                # The timer expired while the bot was turned off - perform revert immediately
                logger.info(
                    "Timer '%s' expired %.0f seconds ago - perform revert", rule.name, -remaining
                )
                task = asyncio.create_task(self._execute_expired_revert(rule))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def close(self) -> None:
        """Cancel and await every background task owned by this engine."""
        tasks = [*self._timer_tasks.values(), *self._background_tasks]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._timer_tasks.clear()
        self._background_tasks.clear()

    async def _execute_expired_revert(self, rule: ScenarioRule) -> None:
        """Perform revert on expired timer (after reboot)."""
        await self.event_bus.emit(
            Event(
                type="scenario_timer_fired",
                timestamp=time.time(),
                data={"scenario_id": rule.id, "scenario_name": rule.name, "expired": True},
            )
        )
        await asyncio.gather(
            *(self._dispatch_command(a, f"{rule.name}(overdue)") for a in rule.revert_actions)
        )
        self._timer_meta.pop(rule.id, None)
        self._save_timers()

    def reload_from_file(self) -> int:
        """Re-read scenarios.json. Returns the number of loaded rules."""
        self.rules.clear()
        self._load_scenarios()
        self._subscribe_events()
        return len(self.rules)

    def add_scenarios(self, scenarios: list[dict]) -> int:
        """Add scripts programmatically. Returns the number of added."""
        added = 0
        for item in scenarios:
            actions = [
                ScenarioAction(
                    device_id=a["device_id"],
                    action=a["action"],
                    params=a.get("params", {}),
                )
                for a in item.get("actions", [])
            ]
            revert_actions = [
                ScenarioAction(
                    device_id=a["device_id"],
                    action=a["action"],
                    params=a.get("params", {}),
                )
                for a in item.get("revert_actions", [])
            ]
            rule = ScenarioRule(
                id=item["id"],
                name=item["name"],
                trigger_event=item["trigger_event"],
                actions=actions,
                revert_after_seconds=item.get("revert_after_seconds", 0),
                revert_actions=revert_actions,
                cancel_event=item.get("cancel_event", ""),
                priority=item.get("priority", 0),
                enabled=item.get("enabled", True),
            )
            self.rules.append(rule)
            added += 1
        if added > 0:
            self._subscribe_events()
            self._save_scenarios()
            logger.info("Added %d scripts", added)
        return added

    def get_rule(self, scenario_id: str) -> ScenarioRule | None:
        """Get rule by ID."""
        for r in self.rules:
            if r.id == scenario_id:
                return r
        return None

    def set_enabled(self, scenario_id: str, enabled: bool) -> bool:
        """Enable/disable script."""
        rule = self.get_rule(scenario_id)
        if not rule:
            return False
        rule.enabled = enabled
        self._save_scenarios()
        if enabled:
            self._subscribe_events()
        logger.info("Scenario '%s': %s", rule.name, "enabled" if enabled else "disabled")
        return True

    def set_enabled_for_device(self, device_id: str, enabled: bool) -> int:
        """Enable/disable all scenarios in which the device participates.
        Returns the number of modified scripts."""
        count = 0
        for rule in self.rules:
            refs_device = any(a.device_id == device_id for a in rule.actions)
            if not refs_device:
                refs_device = any(a.device_id == device_id for a in rule.revert_actions)
            if refs_device and rule.enabled != enabled:
                rule.enabled = enabled
                count += 1
                logger.info(
                    "Script '%s': %s (device %s)",
                    rule.name,
                    "enabled" if enabled else "disabled",
                    device_id,
                )
        if count > 0:
            self._save_scenarios()
            if enabled:
                self._subscribe_events()
        return count

    def remove_scenarios_for_device(self, device_id: str) -> int:
        """Delete all scenarios in which the device is involved.
        Cancels active timers WITHOUT executing revert_actions.
        Returns the number of removed scripts."""
        to_remove = []
        for rule in self.rules:
            refs = any(a.device_id == device_id for a in rule.actions)
            if not refs:
                refs = any(a.device_id == device_id for a in rule.revert_actions)
            if refs:
                to_remove.append(rule)

        for rule in to_remove:
            # Cancel timer without doing revert
            task = self._timer_tasks.pop(rule.id, None)
            if task and not task.done():
                task.cancel()
            self._timer_meta.pop(rule.id, None)
            self.rules.remove(rule)
            logger.info("Removed script '%s' (device %s)", rule.name, device_id)

        if to_remove:
            self._save_scenarios()
            self._save_timers()
        return len(to_remove)

    def start_timer_with_remaining(self, scenario_id: str, remaining_seconds: int) -> bool:
        """Start a timer with the specified remaining time.
        Public API for the web interface (changing the script type to battery).
        Returns False if the rule is not found or there is no revert."""
        rule = self.get_rule(scenario_id)
        if not rule or not rule.revert_actions:
            return False
        self._start_timer(rule, remaining_seconds)
        return True

    def _save_scenarios(self) -> None:
        """Save the current state of the scenarios in scenarios.json."""
        SCENARIOS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for rule in self.rules:
            item: dict[str, Any] = {
                "id": rule.id,
                "name": rule.name,
                "trigger_event": rule.trigger_event,
                "actions": [
                    {
                        "device_id": a.device_id,
                        "action": a.action,
                        **({"params": a.params} if a.params else {}),
                    }
                    for a in rule.actions
                ],
                "priority": rule.priority,
                "enabled": rule.enabled,
            }
            if rule.revert_after_seconds > 0:
                item["revert_after_seconds"] = rule.revert_after_seconds
            if rule.revert_actions:
                item["revert_actions"] = [
                    {
                        "device_id": a.device_id,
                        "action": a.action,
                        **({"params": a.params} if a.params else {}),
                    }
                    for a in rule.revert_actions
                ]
            if rule.cancel_event:
                item["cancel_event"] = rule.cancel_event
            data.append(item)
        SCENARIOS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def get_scenarios_for_device(self, device_id: str) -> list[dict]:
        """Device-related scripts (via actions or revert_actions)."""
        now = time.time()
        result = []
        for rule in self.rules:
            refs = any(a.device_id == device_id for a in rule.actions)
            if not refs:
                refs = any(a.device_id == device_id for a in rule.revert_actions)
            if not refs:
                continue
            info: dict[str, Any] = {
                "id": rule.id,
                "name": rule.name,
                "trigger_event": rule.trigger_event,
                "enabled": rule.enabled,
                "priority": rule.priority,
            }
            meta = self._timer_meta.get(rule.id)
            if meta:
                fires_at = meta.get("fires_at", 0)
                remaining = max(0, int(fires_at - now))
                info["timer_remaining"] = remaining
                info["timer_fires_at"] = fires_at
            result.append(info)
        return result

    def list_scenarios(self) -> list[dict]:
        """List of scripts for the web interface."""
        now = time.time()
        result = []
        for rule in self.rules:
            info: dict[str, Any] = {
                "id": rule.id,
                "name": rule.name,
                "trigger_event": rule.trigger_event,
                "enabled": rule.enabled,
                "priority": rule.priority,
            }
            # Active timer
            meta = self._timer_meta.get(rule.id)
            if meta:
                fires_at = meta.get("fires_at", 0)
                remaining = max(0, int(fires_at - now))
                info["timer_remaining"] = remaining
                info["timer_fires_at"] = fires_at
            result.append(info)
        return result
