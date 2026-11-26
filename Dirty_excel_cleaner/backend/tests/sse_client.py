"""Reading the API's Server-Sent Events in tests, and starting a validation to its end."""

from __future__ import annotations

import json


def read_events(client, url: str) -> list[tuple[str, dict]]:
    """Every event of the stream at ``url``, in order, until the server ends it."""
    events: list[tuple[str, dict]] = []
    name, data = None, []
    with client.stream("GET", url) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"].startswith("no-cache")
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
            elif not line and name:
                events.append((name, json.loads("\n".join(data))))
                name, data = None, []
    return events


def validate(client, job_ids: list[str], batch_id: str | None = None) -> dict:
    """Start validating these jobs, follow the stream to ``done`` and return the review."""
    response = client.post("/api/validations", json={"job_ids": job_ids, "batch_id": batch_id})
    assert response.status_code == 202, response.text
    started = response.json()
    assert started["status"] == "running" and started["progress"]["status"] == "running"
    events = read_events(client, f"/api/validations/{started['id']}/events")
    kind, payload = events[-1]
    assert kind == "done", payload
    return payload["result"]
