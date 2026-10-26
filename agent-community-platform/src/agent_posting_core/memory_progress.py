"""Observe memory-update calls without changing their batching or write behavior."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .memory import MemoryEvent


class MemoryUpdateProgress:
    """Count unique source events only after their update call has returned.

    A successful update can retain, summarize, or ignore input; these counts are
    processed source events, not the number of facts retained by a strategy.
    Black-box batch calls stay pending until the whole call succeeds.
    """

    def __init__(
        self, *, events: list[MemoryEvent], known_event_ids: set[str],
        strategy: str, phase: str, callback: Callable[[dict[str, Any]], None],
        remote_live: bool = False,
    ) -> None:
        self.event_ids = list(dict.fromkeys(
            event.event_id for event in events
            if event.event_id not in known_event_ids
        ))
        self._pending = set(self.event_ids)
        self._completed: list[str] = []
        self._skipped: dict[str, dict[str, Any]] = {}
        self.strategy = strategy
        self.phase = phase
        self.callback = callback
        self.batch_events = 0
        self.current_event_id: str | None = None
        self.progress_granularity = "event"
        self.status = "running"
        self.rolled_back = False
        self.remote_live = remote_live

    def emit(self) -> None:
        self.callback({
            "strategy": self.strategy,
            "phase": self.phase,
            "status": self.status,
            "completed_events": 0 if self.rolled_back else len(self._completed),
            "processed_events": len(self._completed) + len(self._skipped),
            "skipped_events": len(self._skipped),
            "skipped_event_details": list(self._skipped.values()),
            "total_events": len(self.event_ids),
            "current_event_id": self.current_event_id,
            "last_completed_event_id": (
                self._completed[-1] if self._completed else None
            ),
            "progress_granularity": self.progress_granularity,
            "batch_events": self.batch_events,
            "rolled_back": self.rolled_back,
            "rollback_scope": "local_snapshot" if self.rolled_back else None,
            "remote_state_may_have_changed": self.remote_live and (
                self.rolled_back or bool(self._skipped)
            ),
        })

    def start(self) -> None:
        self.current_event_id = self.event_ids[0] if self.event_ids else None
        self.emit()

    def begin_batch(
        self, events: list[MemoryEvent], *, buffered_ids: frozenset[str] = frozenset(),
    ) -> None:
        pending = list(dict.fromkeys(
            event_id for event_id in [
                *(value for value in self.event_ids if value in buffered_ids),
                *(event.event_id for event in events),
            ] if event_id in self._pending
        ))
        self.batch_events = len(pending)
        self.progress_granularity = "batch" if len(pending) > 1 else "event"
        self.current_event_id = pending[0] if pending else None
        if pending:
            self.emit()

    def complete_batch(
        self, events: list[MemoryEvent], *, buffered_before: frozenset[str] = frozenset(),
        buffered_after: frozenset[str] = frozenset(),
        skipped: tuple[dict[str, Any], ...] = (),
    ) -> None:
        batch_ids = {event.event_id for event in events} | buffered_before
        for item in skipped:
            event_id = item["source_event"]["event_id"]
            if event_id in self._pending and event_id in batch_ids:
                self._pending.remove(event_id)
                self._skipped[event_id] = item
        for event_id in self.event_ids:
            if (event_id in self._pending and event_id in batch_ids
                    and event_id not in buffered_after):
                self._pending.remove(event_id)
                self._completed.append(event_id)
        self.current_event_id = None
        self.batch_events = 0
        self.emit()

    def finish(self) -> None:
        self.status = (
            "pending" if self._pending
            else "completed_with_errors" if self._skipped else "completed"
        )
        self.current_event_id = None
        self.batch_events = 0
        self.emit()

    def fail(self, *, rolled_back: bool = False) -> None:
        self.status = "failed"
        self.rolled_back = rolled_back
        self.batch_events = 0
        self.emit()
