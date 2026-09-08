"""A small state file the sensor daemon publishes for the web UI.

The camera server and the sensor daemon are deliberately separate processes with
no channel between them.  A file is enough: the daemon rewrites it once a second
and the server reads it when a browser asks, so neither can block the other, and
a daemon that dies simply leaves a file that goes stale -- which is exactly the
signal the UI needs.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var", "sensors.json")


class Heartbeat:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, state: dict) -> None:
        state = dict(state, updated_unix=time.time())
        directory = os.path.dirname(self.path)
        try:
            # Replace atomically so a reader never sees a half-written file.
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass            # publishing state must never break recording

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


def read(path: str = DEFAULT_PATH, stale_after: float = 5.0) -> dict:
    """What the server reports to the UI. Never raises."""
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {"running": False, "reason": "the sensor daemon is not running"}
    age = time.time() - state.get("updated_unix", 0)
    state["age_s"] = round(age, 1)
    state["running"] = age < stale_after
    if not state["running"]:
        state["reason"] = f"no sign of the sensor daemon for {age:.0f}s"
    return state
