"""Live progress: the tracker's arithmetic and the Server-Sent Events stream (no API)."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
from api import progress  # noqa: E402


def _tracker():
    return progress.Tracker.of(("a", "First", 1), ("b", "Second", 3))


def test_the_bar_is_the_weighted_share_of_units_done():
    tracker = _tracker()
    assert tracker.snapshot()[1]["fraction"] == 0
    tracker.start("a", 2, "reading")
    tracker.advance(1)
    assert tracker.snapshot()[1]["fraction"] == 0.125  # half of a quarter
    tracker.start("b", 4)  # a is done
    _, state = tracker.snapshot()
    assert state["fraction"] == 0.25 and [phase["state"] for phase in state["phases"]] == ["done", "active"]
    tracker.advance(2, "half way")
    _, state = tracker.snapshot()
    assert state["fraction"] == 0.625 and state["percent"] == 62
    assert (state["phase"], state["label"], state["detail"], state["step"], state["steps"]) == \
        ("b", "Second", "half way", 2, 2)
    tracker.succeed(lambda: {"ok": True})
    _, state = tracker.snapshot()
    assert state["status"] == "succeeded" and state["fraction"] == 1 and state["percent"] == 100
    assert all(phase["state"] == "done" for phase in state["phases"])


def test_the_bar_never_goes_back_and_stops_short_of_full_until_done():
    tracker = _tracker()
    tracker.start("b", 4)
    tracker.advance(4)
    assert tracker.snapshot()[1]["fraction"] == 0.999  # every unit done, but not finished
    tracker.start("b", 10)  # recounted: fewer of more units done
    assert tracker.snapshot()[1]["fraction"] == 0.999
    tracker.advance(100)  # never past a phase's total
    assert tracker.phases[1].done == 10


def test_a_failure_marks_the_step_it_stopped_at():
    tracker = _tracker()
    tracker.start("a")
    tracker.start("b", 5, "matching")
    tracker.fail({"code": "boom", "message": "It broke."})
    _, state = tracker.snapshot()
    assert state["status"] == "failed" and state["error"]["code"] == "boom"
    assert [phase["state"] for phase in state["phases"]] == ["done", "failed"]
    assert state["phase"] == "b" and state["detail"] == "matching"


def test_every_change_is_a_new_version_and_seconds_are_measured():
    tracker = _tracker()
    first, _ = tracker.snapshot()
    tracker.start("a")
    tracker.note("still going")
    second, state = tracker.snapshot()
    assert second == first + 2
    assert state["phases"][0]["seconds"] is not None and state["phases"][1]["seconds"] is None
    with pytest.raises(KeyError):
        tracker.start("nope")


def test_an_event_is_one_sse_message():
    text = progress.event("progress", {"detail": "two\nlines", "when": __import__("datetime").date(2026, 1, 2)}, 7)
    assert text.endswith("\n\n") and text.count("\n") == 4
    lines = text.splitlines()
    assert lines[:2] == ["event: progress", "id: 7"]
    assert json.loads(lines[2][len("data: "):]) == {"detail": "two\nlines", "when": "2026-01-02"}


def _collect(tracker, work=None, keepalive=None):
    """Run ``work`` on a thread while the stream is read; the stream's messages, parsed."""

    async def read():
        chunks = []
        if work is not None:
            threading.Thread(target=work, daemon=True).start()
        async for chunk in progress.stream(tracker):
            chunks.append(chunk)
        return chunks

    if keepalive is not None:
        saved, progress.KEEPALIVE_SECONDS = progress.KEEPALIVE_SECONDS, keepalive
    try:
        chunks = asyncio.run(asyncio.wait_for(read(), 10))
    finally:
        if keepalive is not None:
            progress.KEEPALIVE_SECONDS = saved
    events = []
    for chunk in chunks:
        fields = dict(line.split(": ", 1) for line in chunk.strip().splitlines() if not line.startswith(":"))
        events.append((fields.get("event"), json.loads(fields["data"]) if "data" in fields else fields))
    return chunks, events


def test_the_stream_follows_the_work_to_done():
    tracker = _tracker()

    def work():
        tracker.start("a", 3)
        for _ in range(3):
            time.sleep(0.15)
            tracker.advance(1)
        tracker.start("b", 2)
        for _ in range(2):
            time.sleep(0.15)
            tracker.advance(1)
        tracker.succeed(lambda: {"answer": 42})

    chunks, events = _collect(tracker, work)
    assert chunks[0] == f"retry: {progress.RETRY_MS}\n\n"
    names = [name for name, _ in events[1:]]
    assert names[:-1] == ["progress"] * (len(names) - 1) and names[-1] == "done"
    assert len(names) >= 4  # the steps arrive as they happen, not all at the end
    fractions = [data["fraction"] for name, data in events[1:-1]]
    assert fractions == sorted(fractions) and fractions[0] < 0.5
    done = events[-1][1]
    assert done["result"] == {"answer": 42} and done["progress"]["percent"] == 100
    ids = [int(line.split(": ")[1]) for chunk in chunks for line in chunk.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_bursts_are_coalesced():
    tracker = _tracker()

    def work():
        tracker.start("a", 2000)
        for _ in range(2000):
            tracker.advance(1)
        tracker.succeed(lambda: None)

    _, events = _collect(tracker, work)
    assert len(events) < 50 and events[-1][0] == "done"


def test_a_stream_opened_after_the_end_answers_at_once():
    tracker = _tracker()
    tracker.succeed(lambda: {"late": True})
    _, events = _collect(tracker)
    assert [name for name, _ in events[1:]] == ["done"] and events[-1][1]["result"] == {"late": True}

    failed = _tracker()
    failed.start("a")
    failed.fail({"code": "x", "message": "No."})
    _, events = _collect(failed)
    assert events[-1][0] == "failed" and events[-1][1]["error"]["code"] == "x"


def test_a_quiet_stream_keeps_the_connection_alive():
    tracker = _tracker()

    def work():
        tracker.start("a")
        time.sleep(0.35)
        tracker.succeed(lambda: None)

    chunks, _ = _collect(tracker, work, keepalive=0.1)
    assert ": keep-alive\n\n" in chunks


def test_a_closed_stream_stops_listening():
    tracker = _tracker()

    async def read_one():
        stream = progress.stream(tracker)
        await stream.__anext__()  # retry
        await stream.__anext__()  # the current state
        assert len(tracker._subscribers) == 1
        await stream.aclose()

    asyncio.run(read_one())
    assert tracker._subscribers == []
    tracker.advance()  # nobody to wake: no error
