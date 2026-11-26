"""Live progress for long work, pushed to the browser as Server-Sent Events.

A :class:`Tracker` belongs to one piece of work running on a worker thread. The work is a
fixed list of phases, each a share of the bar (its weight) and a count of units -- rows,
columns, queries -- known when the phase starts. The worker reports units as it finishes
them; the bar is the weighted share of units done, never a guess from the clock, and it
never moves backwards.

Every change bumps a version and wakes the streams waiting on it. :func:`stream` turns a
tracker into an SSE response body: the current state at once, then each change as it
happens (bursts coalesced), a comment every few seconds so proxies keep the connection
open, and a final ``done`` or ``failed`` event. A browser that reconnects simply gets the
current state again, so nothing has to be replayed.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

PENDING, ACTIVE, DONE, FAILED = "pending", "active", "done", "failed"
RUNNING, SUCCEEDED = "running", "succeeded"

# The longest quiet spell on the wire: a comment line, so proxies keep the stream open.
KEEPALIVE_SECONDS = 10.0
# Bursts (a tick per column) are coalesced into at most this many events a second.
MAX_EVENTS_PER_SECOND = 10
# What the browser waits before reconnecting a dropped stream.
RETRY_MS = 2000


@dataclass
class Phase:
    id: str
    label: str
    weight: float
    total: float = 1.0
    done: float = 0.0
    state: str = PENDING
    detail: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def fraction(self) -> float:
        if self.state == DONE:
            return 1.0
        return min(1.0, self.done / self.total) if self.total > 0 else 0.0

    def as_dict(self, now: float) -> dict:
        end = self.finished_at or (now if self.started_at else None)
        return {"id": self.id, "label": self.label, "state": self.state, "detail": self.detail,
                "done": round(self.done, 3), "total": round(self.total, 3), "fraction": round(self.fraction, 4),
                "seconds": round(end - self.started_at, 2) if self.started_at and end else None}


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    wake: asyncio.Event


@dataclass
class Tracker:
    """Progress of one piece of work: phases, counters, and how it ended."""

    phases: list[Phase]
    status: str = RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Counters the UI shows beside the bar (tables 2 of 5, ...): name -> {done, total}.
    counters: dict[str, dict] = field(default_factory=dict)
    error: dict | None = None
    # How the finished work is shown: called once, when a stream sends the final event.
    result: Callable[[], Any] | None = field(default=None, repr=False)
    _version: int = 0
    _shown: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _subscribers: list[_Subscriber] = field(default_factory=list, repr=False)

    @classmethod
    def of(cls, *phases: tuple[str, str, float]) -> "Tracker":
        """``phases``: (id, label, weight) in the order they run."""
        return cls([Phase(id, label, weight) for id, label, weight in phases])

    # --- what the worker reports -------------------------------------------------

    def start(self, phase_id: str, total: float = 1.0, detail: str | None = None) -> None:
        """Begin a phase of ``total`` units; every earlier phase is done."""
        with self._lock:
            now = time.time()
            reached = False
            for phase in self.phases:
                if phase.id == phase_id:
                    reached = True
                    phase.state, phase.total, phase.done, phase.detail = ACTIVE, max(float(total), 0.0), 0.0, detail
                    phase.started_at, phase.finished_at = now, None
                elif not reached and phase.state != DONE:
                    phase.state, phase.done = DONE, phase.total
                    phase.started_at = phase.started_at or now
                    phase.finished_at = now
            if not reached:
                raise KeyError(phase_id)
        self._changed()

    def advance(self, units: float = 1.0, detail: str | None = None) -> None:
        with self._lock:
            phase = self._active()
            if phase is not None:
                phase.done = min(phase.total, phase.done + units)
                if detail is not None:
                    phase.detail = detail
        self._changed()

    def note(self, detail: str) -> None:
        """What the active phase is doing now, without a unit finished."""
        with self._lock:
            phase = self._active()
            if phase is not None:
                phase.detail = detail
        self._changed()

    def count(self, name: str, done: int, total: int) -> None:
        with self._lock:
            self.counters[name] = {"done": done, "total": total}
        self._changed()

    def succeed(self, result: Callable[[], Any]) -> None:
        with self._lock:
            now = time.time()
            for phase in self.phases:
                if phase.state != DONE:
                    phase.state, phase.done = DONE, phase.total
                    phase.started_at = phase.started_at or now
                    phase.finished_at = now
            self.status, self.finished_at, self.result = SUCCEEDED, now, result
        self._changed()

    def fail(self, error: dict) -> None:
        with self._lock:
            phase = self._active()
            if phase is not None:
                phase.state, phase.finished_at = FAILED, time.time()
            self.status, self.finished_at, self.error = FAILED, time.time(), error
        self._changed()

    def _active(self) -> Phase | None:
        return next((phase for phase in self.phases if phase.state == ACTIVE), None)

    # --- what the streams read ---------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.status != RUNNING

    def snapshot(self) -> tuple[int, dict]:
        with self._lock:
            now = self.finished_at or time.time()
            total = sum(phase.weight for phase in self.phases) or 1.0
            fraction = sum(phase.weight * phase.fraction for phase in self.phases) / total
            # Never backwards: a phase's units can be recounted, the bar can't.
            self._shown = max(self._shown, 1.0 if self.status == SUCCEEDED else min(fraction, 0.999))
            active = self._active()
            failed = next((phase for phase in self.phases if phase.state == FAILED), None)
            current = active or failed
            return self._version, {
                "status": self.status,
                "fraction": round(self._shown, 4),
                "percent": int(self._shown * 100),
                "phase": current.id if current else None,
                "label": current.label if current else None,
                "detail": current.detail if current else None,
                "step": (self.phases.index(current) + 1) if current else len(self.phases),
                "steps": len(self.phases),
                "phases": [phase.as_dict(now) for phase in self.phases],
                "counters": {name: dict(value) for name, value in self.counters.items()},
                "started_at": self.started_at,
                "elapsed": round(now - self.started_at, 2),
                "error": self.error,
            }

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> _Subscriber:
        subscriber = _Subscriber(loop, asyncio.Event())
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def _changed(self) -> None:
        with self._lock:
            self._version += 1
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.wake.set)
            except RuntimeError:  # its event loop is gone: the stream ended without unsubscribing
                self.unsubscribe(subscriber)


# --- Server-Sent Events -------------------------------------------------------------------


def event(name: str, data: Any, event_id: int | None = None) -> str:
    """One SSE message. The data is JSON on one line (JSON escapes newlines in strings)."""
    lines = [f"event: {name}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, default=_json_default, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


async def stream(tracker: Tracker) -> AsyncIterator[str]:
    """The tracker as SSE: ``progress`` events while it runs, then ``done`` (with the result)
    or ``failed`` (with the error), after which the stream ends."""
    loop = asyncio.get_running_loop()
    subscriber = tracker.subscribe(loop)
    sent = None
    try:
        yield f"retry: {RETRY_MS}\n\n"
        while True:
            # Clear first, then read: a change after the read sets the event again.
            subscriber.wake.clear()
            version, state = tracker.snapshot()
            if version != sent:
                sent = version
                if tracker.status == SUCCEEDED:
                    result = tracker.result() if tracker.result else None
                    yield event("done", {"progress": state, "result": result}, version)
                    return
                if tracker.status == FAILED:
                    yield event("failed", {"progress": state, "error": tracker.error}, version)
                    return
                yield event("progress", state, version)
            try:
                await asyncio.wait_for(subscriber.wake.wait(), KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            await asyncio.sleep(1 / MAX_EVENTS_PER_SECOND)
    finally:
        tracker.unsubscribe(subscriber)


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx and similar proxies buffer responses unless told otherwise.
    "X-Accel-Buffering": "no",
}
