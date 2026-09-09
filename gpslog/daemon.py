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

# What the always-on log costs when nothing is being recorded.  At 10 Hz it was
# 535,000 rows and 68 MB in one day, nearly all of it "no fix" from sitting
# indoors -- a rate that only earns its keep inside a recording.  So when no
# recording is running the log keeps one row every ten seconds.
#
# The receiver itself is left at full rate rather than reconfigured.  Slowing it
# down would save a little USB traffic, but a recording would then start on a
# stale position and wait for the receiver to speed up again; keeping it at 10 Hz
# means the first row of a recording is as fresh as every other one.
IDLE_PERIOD_S = 10.0

# The receiver rate is measured over two seconds, which is useless for the log
# rate once it drops to a tenth of a hertz: a two second window can only ever
# report nothing or five times the truth.  Half a minute holds three rows at
# 0.1 Hz, which averages out to the right answer.
LOG_RATE_WINDOW_S = 30.0

# How far apart two messages in one read may claim to be before their iTOW
# difference is treated as nonsense -- a week rollover, or a garbled field --
# and the read's own time is used instead.
MAX_BATCH_SPREAD_S = 5.0


def host_times(fixes: list[dict], arrival: float,
               max_spread_s: float = MAX_BATCH_SPREAD_S) -> list[tuple]:
    """Give every message from one read its own arrival time.

    A single read can hand back more than one message, and stamping them all
    with the moment the read returned made consecutive rows share a timestamp:
    measured on the rig, 99 gaps of 0.000 s alternating with 99 of 0.200 s,
    while the receiver's own clock said every message was 0.100 s apart.

    The receiver is the better clock here, so it is the one asked how far apart
    the messages are.  The batch is spread backwards from its last message,
    which is the one that had just arrived.  A row whose iTOW cannot be
    trusted keeps the read's own time rather than a fabricated one.
    """
    if not fixes:
        return []
    last_itow = fixes[-1].get("itow_s")
    out = []
    for fix in fixes:
        itow = fix.get("itow_s")
        behind = 0.0
        if last_itow is not None and itow is not None:
            delta = last_itow - itow
            if 0.0 <= delta <= max_spread_s:
                behind = delta
        out.append((fix, arrival - behind))
    return out


class GpsDaemon:
    def __init__(self, base_url: str, directory: str, period_ms: int = DEFAULT_PERIOD_MS,
                 gps_only: bool = False, device: str = "",
                 idle_period_s: float = IDLE_PERIOD_S):
        self.watcher = ServerWatcher(base_url)
        self.daily = DailyLog(directory)
        self.copy = RecordingCopy()
        self.period_ms = period_ms
        self.gnss = U.GPS_ONLY if gps_only else U.ALL_GNSS
        self.device_override = device
        self.idle_period_s = idle_period_s
        self.heartbeat = Heartbeat(HEARTBEAT_PATH)

        self._stop = False
        self._serial: serial.Serial | None = None
        self.device_path = ""
        self.last_fix: dict | None = None
        self.last_row_at = 0.0
        self.rate_hz = 0.0          # what the receiver delivers
        # What reaches the always-on log, seeded with the rate the idle period
        # asks for so the first half minute does not read as "nothing logged".
        self.log_rate_hz = 1.0 / idle_period_s if idle_period_s else 0.0
        self.throttled = False
        self.last_error = ""
        self._last_kept = 0.0
        self._last_kept_fix = None

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

    # -- what gets logged --------------------------------------------------

    def keep_row(self, now: float, fix: dict, recording: bool) -> bool:
        """Whether this row belongs in the always-on log.

        Every row while a recording runs.  Otherwise one every idle period --
        plus any row where the fix state changed, so the log still says when a
        fix was gained or lost instead of reporting it up to ten seconds late.
        """
        keep = (recording
                or fix.get("fix") != self._last_kept_fix
                or now - self._last_kept >= self.idle_period_s)
        if keep:
            self._last_kept = now
            self._last_kept_fix = fix.get("fix")
        return keep

    # -- state for the UI -------------------------------------------------

    def _publish(self) -> None:
        fix = self.last_fix or {}
        self.heartbeat.write({
            "pid": os.getpid(),
            "device": self.device_path,
            "connected": self._serial is not None,
            "rate_hz": round(self.rate_hz, 1),
            "log_rate_hz": round(self.log_rate_hz, 2),
            "throttled": self.throttled,
            "idle_period_s": self.idle_period_s,
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
        log_start, log_rows = time.monotonic(), 0
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
                    # Block for the first byte of a chunk and take the time
                    # right there, then sweep up whatever else is already
                    # buffered.  read(4096) returned on the 0.2 s timeout
                    # instead, so every row was stamped with the read cycle
                    # rather than with when its message arrived.
                    head = self._serial.read(1)
                    arrival = time.time()
                    data = head + self._serial.read(self._serial.in_waiting or 0)
                except (serial.SerialException, OSError) as exc:
                    logger.warning("receiver went away: %s", exc)
                    self.last_error = str(exc)
                    self._close_device()
                    continue

                # Ask where a recording is writing before handling this batch
                # rather than after, so its very first rows are copied too --
                # and so this batch is logged at the rate the recording wants.
                self.copy.follow(self.watcher.snapshot())
                recording = bool(self.copy.directory)
                self.throttled = not recording

                batch = []
                for cls, msg, payload in reader.feed(data or b""):
                    if (cls, msg) != (U.CLS_NAV, U.MSG_PVT):
                        continue
                    fix = U.parse_pvt(payload)
                    if fix is not None:
                        batch.append(fix)

                for fix, now in host_times(batch, arrival):
                    stamp = datetime.fromtimestamp(now)
                    row = dict(fix, unix_time=f"{now:.3f}",
                               iso_time=stamp.isoformat(timespec="milliseconds"))
                    line = format_row(row)
                    # The recording's own copy always gets every row; only the
                    # always-on log is thinned out while nothing is recording.
                    if self.keep_row(now, fix, recording):
                        self.daily.write(stamp, line)
                        log_rows += 1
                    self.copy.write(line)
                    self.last_fix = fix
                    self.last_row_at = now
                    window_rows += 1

                elapsed = time.monotonic() - window_start
                if elapsed >= 2.0:
                    self.rate_hz = window_rows / elapsed
                    window_start, window_rows = time.monotonic(), 0
                log_elapsed = time.monotonic() - log_start
                if log_elapsed >= LOG_RATE_WINDOW_S:
                    self.log_rate_hz = log_rows / log_elapsed
                    log_start, log_rows = time.monotonic(), 0
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
