"""Remembering which camera wedged the recorder, so the next start survives it.

A camera that cannot get the USB bandwidth it asks for does not fail cleanly:
it blocks inside libuvc with the GIL held, which stops every thread in the
process (see systemd.py).  systemd kills the recorder and starts it again, the
same camera wedges the same way, and that repeats until systemd gives up and
leaves the service dead.  It was measured doing exactly that, with seven
cameras on one USB 2.0 bus: five streaming, the sixth wedging every start.

Nothing inside the process can break that cycle, because while it happens no
Python runs.  What it can do is leave a note on disk before the open and take
it away after.  A note still there at the next start names the camera that was
mid-open when the process died: that one is left out of this run and reported
instead.  The recorder comes up with the cameras that do fit -- which is what
an operator needs from it -- and the cycle converges, because each start can
quarantine at most one more.

The quarantine is tied to the set of attached cameras.  Unplug a headset, move
one to another port, and the set changes: that is the operator saying "this is
a different problem now" in the only language a file can read, so everything is
retried.  `run.py --clear-quarantine` says it in words.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Next to the other daemons' state files (gpslog/daemon.py, sensorlog).
STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var",
    "cameras.json")


def signature_of(cams) -> str:
    """What counts as "the same set of cameras" between two runs."""
    return ",".join(sorted(cam.usb_path for cam in cams))


class OpenGuard:
    """The note on disk, and the list of cameras it has already condemned."""

    def __init__(self, path: str, signature: str = ""):
        self.path = path
        self.signature = signature
        self.quarantined: dict[str, dict] = {}
        self._in_flight: dict | None = None
        self._load()

    # -- the file --------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            return
        if stored.get("signature", "") != self.signature:
            # Different cameras, or different ports: whatever was learned about
            # the old arrangement says nothing about this one.
            if stored.get("quarantined"):
                logger.info("the cameras changed since last time; "
                            "retrying every one of them")
            return
        self.quarantined = dict(stored.get("quarantined") or {})
        in_flight = stored.get("in_flight")
        if in_flight and in_flight.get("cam_id"):
            # The recorder died between the two writes below, which is the
            # signature of a camera that took the whole process down with it.
            cam_id = in_flight["cam_id"]
            self.quarantined[cam_id] = {
                "usb_path": in_flight.get("usb_path", ""),
                "since_unix": in_flight.get("started_unix", time.time()),
                "reason": "froze the recorder while being opened",
            }
            logger.warning("%s wedged the last start; leaving it out of this "
                           "one (clear with run.py --clear-quarantine)", cam_id)
        self._write()

    def _write(self) -> None:
        payload = {
            "signature": self.signature,
            "quarantined": self.quarantined,
            "in_flight": self._in_flight,
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # Written in place rather than through a temporary file and rename:
            # what matters is that the note is on disk *before* the open, and
            # os.replace is one more thing to do while a camera might wedge.
            with open(self.path, "w") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # Losing the note costs the next start a freeze, not this one a
            # camera.  Never raise into an open.
            logger.warning("could not write %s: %s", self.path, exc)

    # -- what the capture threads use ------------------------------------

    def is_quarantined(self, cam_id: str) -> bool:
        return cam_id in self.quarantined

    @contextlib.contextmanager
    def attempting(self, cam_id: str, usb_path: str):
        """Leave the note for as long as this camera is being opened.

        Only one camera can be inside this at a time -- the caller holds the
        device lock -- so one note is enough to name the camera at fault.
        """
        self._in_flight = {"cam_id": cam_id, "usb_path": usb_path,
                           "started_unix": time.time()}
        self._write()
        try:
            yield
        finally:
            self._in_flight = None
            self._write()

    def clear(self) -> list[str]:
        """Let every quarantined camera be tried again. -> the ones released."""
        released = sorted(self.quarantined)
        self.quarantined = {}
        self._in_flight = None
        self._write()
        return released

    def report(self) -> list[dict]:
        """What the UI shows: one entry per camera being left alone."""
        return [{"id": cam_id, **details}
                for cam_id, details in sorted(self.quarantined.items())]


class NoGuard:
    """Stand-in for when nothing is being remembered: tests, and --list.

    Same shape as OpenGuard so no caller has to check for None.
    """

    quarantined: dict = {}

    def is_quarantined(self, cam_id: str) -> bool:
        return False

    @contextlib.contextmanager
    def attempting(self, cam_id: str, usb_path: str):
        yield

    def clear(self) -> list[str]:
        return []

    def report(self) -> list[dict]:
        return []
