"""Continuous GNSS logging, independent of whether anything is being recorded.

Two rules shape this:

* The receiver is optional. It may be absent at boot, unplugged mid-run or
  plugged in later, and none of that may disturb the rest of the system -- so
  every failure here reduces to "wait and try again", never to an exit.
* A position is worth logging even when there is no fix. Indoors the receiver
  reports fixType 0 with no satellites; writing that row documents the gap
  instead of leaving a silent hole in the timeline.

While a recording runs, every row is additionally written into the recording's
directory, so a recording carries its own copy of the track.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import serial

from . import ublox as U

logger = logging.getLogger(__name__)

DEVICE_GLOBS = (
    "/dev/serial/by-id/*u-blox*",     # stable across replugs; preferred
    "/dev/serial/by-id/*u_blox*",
    "/dev/ttyACM*",
)
RETRY_DELAY_S = 5.0
STATUS_POLL_S = 0.5
HEARTBEAT_S = 1.0

COLUMNS = ("unix_time", "iso_time", "gps_time", "fix", "fix_type", "fix_ok",
           "num_sv", "lat_deg", "lon_deg", "hmsl_m", "height_m", "h_acc_m",
           "v_acc_m", "speed_mps", "heading_deg", "vel_n_mps", "vel_e_mps",
           "vel_d_mps", "pdop", "itow_s")


def find_device() -> str | None:
    for pattern in DEVICE_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


def format_row(row: dict) -> str:
    out = []
    for name in COLUMNS:
        value = row.get(name)
        if value is None or value == "":
            out.append("")
        elif isinstance(value, bool):
            out.append("1" if value else "0")
        elif isinstance(value, float):
            # Degrees need seven decimals to keep centimetre resolution.
            out.append(f"{value:.7f}" if "deg" in name else f"{value:.3f}")
        else:
            out.append(str(value))
    return ",".join(out) + "\n"


class DailyLog:
    """The always-on log, one file per day, named <date>_gps.log.csv."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self._date = ""
        self._fh = None
        self.path = ""
        self.rows = 0

    def _roll(self, when: datetime) -> None:
        date = when.strftime("%Y-%m-%d")
        if date == self._date and self._fh is not None:
            return
        self.close()
        self.path = os.path.join(self.directory, f"{date}_gps.log.csv")
        fresh = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", buffering=1 << 14)
        if fresh:
            self._fh.write(",".join(COLUMNS) + "\n")
        self._date = date
        logger.info("logging to %s", self.path)

    def write(self, when: datetime, line: str) -> None:
        self._roll(when)
        self._fh.write(line)
        self.rows += 1

    def flush(self) -> None:
        if self._fh:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class RecordingCopy:
    """A second copy of the same rows, inside the recording's directory."""

    def __init__(self):
        self.directory = ""
        self.path = ""
        self.rows = 0
        self._fh = None

    def follow(self, directory: str) -> None:
        if directory == self.directory:
            return
        self.close()
        self.directory = directory
        if not directory:
            return
        try:
            os.makedirs(directory, exist_ok=True)
            name = datetime.now().strftime("%Y-%m-%d") + "_gps.log.csv"
            self.path = os.path.join(directory, name)
            self._fh = open(self.path, "a", buffering=1 << 14)
            self._fh.write(",".join(COLUMNS) + "\n")
            self.rows = 0
            logger.info("copying track into %s", self.path)
        except OSError:
            logger.exception("could not open the recording copy")
            self._fh = None

    def write(self, line: str) -> None:
        if self._fh:
            self._fh.write(line)
            self.rows += 1

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        self.directory = ""


class ServerWatcher(threading.Thread):
    """Learns from the recorder where a running recording is writing."""

    def __init__(self, base_url: str):
        super().__init__(name="gps-server-watch", daemon=True)
        self.url = base_url.rstrip("/") + "/api/status"
        self.lock = threading.Lock()
        self.directory = ""
        self._stop = threading.Event()

    def snapshot(self) -> str:
        with self.lock:
            return self.directory

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            directory = ""
            try:
                with urllib.request.urlopen(self.url, timeout=2) as response:
                    payload = json.load(response)
                if payload.get("recording"):
                    directory = payload.get("recording_directory") or ""
            except (urllib.error.URLError, OSError, ValueError):
                directory = ""      # recorder unreachable: keep logging anyway
            with self.lock:
                self.directory = directory
            self._stop.wait(STATUS_POLL_S)
