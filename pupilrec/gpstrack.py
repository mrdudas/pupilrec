"""Serving the GNSS day log to the map page.

A day at 10 Hz is close to a million rows, which is neither useful on a map nor
sensible to re-read every second.  So each date is cached: only bytes appended
since the last look are parsed, and points are thinned to those that actually
describe the path.  The client asks for what it does not have yet by index.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import date as date_type

# Thinning: a point is kept once the track has moved this far from the last kept
# one, or this long has passed, whichever comes first.  Standing still therefore
# costs one point every few seconds rather than ten every second.
MIN_MOVE_M = 2.0
MIN_INTERVAL_S = 5.0
MAX_POINTS = 20000          # a hard ceiling per day, to bound memory and payload

EARTH_R = 6371000.0


def approx_distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Good enough for a few-metre threshold, and far cheaper than haversine."""
    mean_lat = math.radians((a_lat + b_lat) * 0.5)
    dx = math.radians(b_lon - a_lon) * math.cos(mean_lat)
    dy = math.radians(b_lat - a_lat)
    return math.hypot(dx, dy) * EARTH_R


class DayTrack:
    """Thinned points for one date, updated incrementally as the log grows."""

    def __init__(self, path: str):
        self.path = path
        self.points: list[tuple[float, float, float]] = []   # lat, lon, unix
        self.rows_seen = 0
        self.fixed_rows = 0
        self.latest: dict | None = None
        self._offset = 0
        self._columns: list[str] | None = None

    def _parse(self, line: str) -> dict | None:
        parts = line.rstrip("\n").split(",")
        if self._columns is None or len(parts) != len(self._columns):
            return None
        return dict(zip(self._columns, parts))

    def refresh(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self._offset:          # file replaced or truncated
            self.__init__(self.path)
        try:
            with open(self.path, "r") as fh:
                if self._columns is None:
                    header = fh.readline()
                    if not header:
                        return
                    self._columns = header.rstrip("\n").split(",")
                    self._offset = fh.tell()
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return

        for line in chunk.splitlines():
            row = self._parse(line)
            if row is None:
                continue
            self.rows_seen += 1
            if row.get("fix_ok") != "1" or not row.get("lat_deg"):
                continue
            try:
                lat, lon = float(row["lat_deg"]), float(row["lon_deg"])
                when = float(row["unix_time"])
            except (ValueError, KeyError):
                continue
            self.fixed_rows += 1
            self.latest = {
                "lat": lat, "lon": lon, "unix_time": when,
                "speed_mps": _as_float(row.get("speed_mps")),
                "heading_deg": _as_float(row.get("heading_deg")),
                "hmsl_m": _as_float(row.get("hmsl_m")),
                "h_acc_m": _as_float(row.get("h_acc_m")),
                "num_sv": _as_int(row.get("num_sv")),
                "fix": row.get("fix", ""),
            }
            if self._keep(lat, lon, when):
                self.points.append((lat, lon, when))
                if len(self.points) > MAX_POINTS:
                    # Drop every other point rather than the oldest: the shape of
                    # the whole day matters more than its most recent detail.
                    self.points = self.points[::2]

    def _keep(self, lat: float, lon: float, when: float) -> bool:
        if not self.points:
            return True
        prev_lat, prev_lon, prev_when = self.points[-1]
        if when - prev_when >= MIN_INTERVAL_S:
            return True
        return approx_distance_m(prev_lat, prev_lon, lat, lon) >= MIN_MOVE_M


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class TrackStore:
    """Caches a DayTrack per date, so repeated polling stays cheap."""

    def __init__(self, directory: str):
        self.directory = directory
        self._days: dict[str, DayTrack] = {}
        self._lock = threading.Lock()

    def path_for(self, day: str) -> str:
        return os.path.join(self.directory, f"{day}_gps.log.csv")

    def available_dates(self) -> list[str]:
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        days = [n[: -len("_gps.log.csv")] for n in names if n.endswith("_gps.log.csv")]
        return sorted(days, reverse=True)

    def track(self, day: str = "", after: int = 0) -> dict:
        day = day or date_type.today().isoformat()
        path = self.path_for(day)
        with self._lock:
            track = self._days.get(day)
            if track is None or track.path != path:
                track = self._days[day] = DayTrack(path)
            track.refresh()
            after = max(0, min(after, len(track.points)))
            return {
                "date": day,
                "exists": os.path.exists(path),
                "total": len(track.points),
                "after": after,
                # Rounded to ~1 cm; full precision would triple the payload.
                "points": [[round(p[0], 7), round(p[1], 7), round(p[2], 1)]
                           for p in track.points[after:]],
                "rows": track.rows_seen,
                "fixed_rows": track.fixed_rows,
                "latest": track.latest,
            }
