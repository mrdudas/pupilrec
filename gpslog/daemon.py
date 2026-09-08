"""The always-on GNSS logging process."""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime

import serial

from sensorlog.heartbeat import Heartbeat

from . import ublox as U
from .logger import (DailyLog, RecordingCopy, RETRY_DELAY_S, ServerWatcher,
                     find_device, format_row)

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var", "gps.json")
HEARTBEAT_S = 1.0

# 100 ms with every constellation enabled.  The receiver can reach 55 ms, but
# only with GPS alone; keeping GLONASS, Galileo and BeiDou buys a far better
# chance of a fix, which matters more than the extra 8 Hz.
DEFAULT_PERIOD_MS = 100


class GpsDaemon:
    def __init__(self, base_url: str, directory: str, period_ms: int = DEFAULT_PERIOD_MS,
                 gps_only: bool = False, device: str = ""):
        self.watcher = ServerWatcher(base_url)
        self.daily = DailyLog(directory)
        self.copy = RecordingCopy()
        self.period_ms = period_ms
        self.gnss = U.GPS_ONLY if gps_only else U.ALL_GNSS
        self.device_override = device
        self.heartbeat = Heartbeat(HEARTBEAT_PATH)

        self._stop = False
        self._serial: serial.Serial | None = None
        self.device_path = ""
        self.last_fix: dict | None = None
        self.last_row_at = 0.0
        self.rate_hz = 0.0
        self.last_error = ""

    def stop(self, *_):
        self._stop = True

    # -- device ----------------------------------------------------------

    def _open_device(self) -> bool:
        path = self.device_override or find_device()
        if not path:
            self.last_error = "no GNSS receiver found"
            return False
        try:
            # Baud rate is irrelevant over USB CDC-ACM but pyserial wants one.
            self._serial = serial.Serial(path, 115200, timeout=0.2)
            self._serial.write(U.cfg_gnss(self.gnss))
            time.sleep(1.0)             # the receiver restarts its GNSS engine
            self._serial.write(U.cfg_rate(self.period_ms))
            self._serial.write(U.cfg_message(U.CLS_NAV, U.MSG_PVT, 1))
            self._serial.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            self.last_error = f"{path}: {exc}"
            self._close_device()
            return False
        self.device_path = path
        self.last_error = ""
        logger.info("GNSS receiver on %s at %d ms", path, self.period_ms)
        return True

    def _close_device(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.device_path = ""

    # -- state for the UI -------------------------------------------------

    def _publish(self) -> None:
        fix = self.last_fix or {}
        self.heartbeat.write({
            "pid": os.getpid(),
            "device": self.device_path,
            "connected": self._serial is not None,
            "rate_hz": round(self.rate_hz, 1),
            "period_ms": self.period_ms,
            "fix": fix.get("fix", ""),
            "fix_ok": bool(fix.get("fix_ok")),
            "num_sv": fix.get("num_sv", 0),
            "lat_deg": fix.get("lat_deg"),
            "lon_deg": fix.get("lon_deg"),
            "h_acc_m": fix.get("h_acc_m"),
            "log_path": self.daily.path,
            "rows": self.daily.rows,
            "copy_path": self.copy.path if self.copy.directory else "",
            "copy_rows": self.copy.rows if self.copy.directory else 0,
            "last_error": self.last_error,
        })

    # -- main loop --------------------------------------------------------

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.watcher.start()
        logger.info("writing to %s", self.daily.directory)

        reader = U.Reader()
        window_start, window_rows = time.monotonic(), 0
        last_beat = 0.0
        last_flush = time.monotonic()

        try:
            while not self._stop:
                if self._serial is None:
                    if not self._open_device():
                        # No receiver is a normal state, not a failure: say so
                        # once, then keep looking without any noise.
                        self._publish()
                        for _ in range(int(RETRY_DELAY_S * 10)):
                            if self._stop:
                                break
                            time.sleep(0.1)
                        continue
                    reader = U.Reader()

                try:
                    data = self._serial.read(4096)
                except (serial.SerialException, OSError) as exc:
                    logger.warning("receiver went away: %s", exc)
                    self.last_error = str(exc)
                    self._close_device()
                    continue

                now = time.time()
                stamp = datetime.fromtimestamp(now)
                for cls, msg, payload in reader.feed(data or b""):
                    if (cls, msg) != (U.CLS_NAV, U.MSG_PVT):
                        continue
                    fix = U.parse_pvt(payload)
                    if fix is None:
                        continue
                    row = dict(fix, unix_time=f"{now:.3f}",
                               iso_time=stamp.isoformat(timespec="milliseconds"))
                    line = format_row(row)
                    self.daily.write(stamp, line)
                    self.copy.write(line)
                    self.last_fix = fix
                    self.last_row_at = now
                    window_rows += 1

                self.copy.follow(self.watcher.snapshot())

                elapsed = time.monotonic() - window_start
                if elapsed >= 2.0:
                    self.rate_hz = window_rows / elapsed
                    window_start, window_rows = time.monotonic(), 0
                if time.monotonic() - last_beat >= HEARTBEAT_S:
                    self._publish()
                    last_beat = time.monotonic()
                if time.monotonic() - last_flush >= 2.0:
                    # Keep the on-disk log close to live without a write per row.
                    self.daily.flush()
                    last_flush = time.monotonic()
        finally:
            self.watcher.stop()
            self.copy.close()
            self.daily.close()
            self._close_device()
            self.heartbeat.clear()
            logger.info("gps daemon stopped after %d rows", self.daily.rows)
        return 0
