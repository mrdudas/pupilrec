"""Records the STWIN board's sensors alongside the camera recordings.

This runs as its own process on purpose.  The cameras are timing sensitive and
share a USB bus; nothing here touches them, and if the board misbehaves the
recording of video is unaffected.  Coordination happens over the recorder's own
HTTP API: this process watches what the server says it is doing and follows.

The board must never be left streaming.  A process killed mid-log wedges its USB
stack until someone presses the reset button, so every exit path stops the log.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from .parser import ComponentStream
from .stwin import INTERFACE_USB, Stwin, StwinError
from .writer import WAV_COMPONENTS, make_writer

logger = logging.getLogger(__name__)

STATUS_POLL_S = 0.4        # how often to ask the recorder what it is doing
DRAIN_INTERVAL_S = 0.02    # how often to empty the board's USB buffers
STATUS_REFRESH_S = 15.0    # how often to re-read the board config while idle
CONTROL_COMPONENTS = {"log_controller", "tags_info", "acquisition_info",
                      "firmware_info", "DeviceInformation", "automode"}


def sensor_components(status: dict) -> dict:
    """The streamable sensors from a device status document."""
    devices = status.get("devices") or []
    if not devices:
        return {}
    out = {}
    for component in devices[0].get("components", []):
        for name, body in component.items():
            if name in CONTROL_COMPONENTS or not isinstance(body, dict):
                continue
            if "data_type" in body and body.get("enable"):
                out[name] = body
    return out


class ServerWatcher(threading.Thread):
    """Polls the recorder's status endpoint on its own thread.

    Kept off the drain loop deliberately: an HTTP call that blocks for a second
    would be a second in which the board's USB buffers are not being emptied.
    """

    def __init__(self, base_url: str):
        super().__init__(name="server-watch", daemon=True)
        self.url = base_url.rstrip("/") + "/api/status"
        self.lock = threading.Lock()
        self.recording = False
        self.directory = ""
        self.name = ""
        self.reachable = False
        self._stop = threading.Event()

    def snapshot(self):
        with self.lock:
            return self.recording, self.directory, self.name, self.reachable

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=2) as response:
                    payload = json.load(response)
                with self.lock:
                    self.recording = bool(payload.get("recording"))
                    self.directory = payload.get("recording_directory") or ""
                    self.name = payload.get("recording_name") or ""
                    self.reachable = True
            except (urllib.error.URLError, OSError, ValueError):
                with self.lock:
                    self.reachable = False
            self._stop.wait(STATUS_POLL_S)


class SensorDaemon:
    def __init__(self, base_url: str, lib_path: str = "", fallback_dir: str = "",
                 wav_components=WAV_COMPONENTS):
        self.board = Stwin(lib_path)
        self.wav_components = tuple(wav_components)
        self.watcher = ServerWatcher(base_url)
        self.fallback_dir = fallback_dir
        self.logging_active = False
        self.streams: dict[str, ComponentStream] = {}
        self.writers: dict = {}
        self.session_dir = ""
        self.started_unix = 0.0
        self._stop = threading.Event()
        # Reading the board's configuration takes most of a second over USB and
        # cannot be done while it streams, so it is fetched ahead of time.  This
        # is what keeps sensor data starting close to the video rather than a
        # second behind it.
        self._cached_status: dict | None = None
        self._cached_at = 0.0

    # -- board sessions ---------------------------------------------------

    def _refresh_status(self) -> None:
        try:
            self._cached_status = self.board.device_status()
            self._cached_at = time.monotonic()
        except StwinError:
            logger.exception("could not read board status")

    def _begin(self, directory: str, name: str) -> None:
        # The control channel does not answer while the board streams, so the
        # configuration must already be in hand.
        status = self._cached_status
        if status is None:
            self._refresh_status()
            status = self._cached_status
        if status is None:
            raise StwinError("board status unavailable")
        components = sensor_components(status)
        if not components:
            raise StwinError("board reports no enabled sensors")

        os.makedirs(directory, exist_ok=True)
        self.streams = {n: ComponentStream(n, cfg) for n, cfg in components.items()}
        self.started_unix = time.time()
        reply = self.board.start_log(INTERFACE_USB)
        self.logging_active = True
        self.session_dir = directory

        self.writers = {n: make_writer(directory, s, self.started_unix, self.wav_components)
                        for n, s in self.streams.items()}
        with open(os.path.join(directory, "stwin_board.json"), "w") as fh:
            json.dump({
                "recording": name,
                "started_unix": self.started_unix,
                "started_iso": datetime.fromtimestamp(self.started_unix).isoformat(timespec="microseconds"),
                "note": "device_time in each CSV is seconds since started_unix",
                "firmware": _firmware_info(status),
                "components": components,
                "start_log_reply": reply,
            }, fh, indent=2)
        logger.info("sensor logging started: %s (%d components)",
                    directory, len(self.streams))

    def _end(self) -> None:
        if not self.logging_active:
            return
        try:
            self.board.stop_log()
        except StwinError:
            logger.exception("stop_log failed")
        self.logging_active = False

        self._drain(final=True)
        results = [w.close() for w in self.writers.values()]
        stopped = time.time()
        summary_path = os.path.join(self.session_dir, "stwin_recording.json")
        with open(summary_path, "w") as fh:
            json.dump({
                "started_unix": self.started_unix,
                "started_iso": datetime.fromtimestamp(self.started_unix).isoformat(timespec="microseconds"),
                "stopped_unix": stopped,
                "duration_s": round(stopped - self.started_unix, 3),
                "components": results,
            }, fh, indent=2)
        total = sum(r["rows"] for r in results)
        logger.info("sensor logging stopped: %d rows across %d files",
                    total, len(results))
        self.writers, self.streams, self.session_dir = {}, {}, ""

    def _drain(self, final: bool = False) -> None:
        """Move whatever the board has buffered into the CSV files."""
        rounds = 20 if final else 1
        for _ in range(rounds):
            moved = 0
            for name, stream in self.streams.items():
                try:
                    size = self.board.available(name)
                    if size <= 0:
                        continue
                    raw = self.board.read(name, size)
                except StwinError:
                    logger.exception("read failed for %s", name)
                    continue
                moved += len(raw)
                values, times = stream.feed(raw)
                self.writers[name].write(values, times)
            if final and moved == 0:
                break

    # -- main loop --------------------------------------------------------

    def stop(self, *_):
        self._stop.set()

    def run(self) -> int:
        self.board.open()
        # A previous run that died mid-log leaves the board streaming; clearing
        # it here is harmless when it was already idle.
        try:
            self.board.stop_log()
        except StwinError:
            pass

        self.watcher.start()
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        logger.info("watching %s", self.watcher.url)

        try:
            while not self._stop.is_set():
                recording, directory, name, reachable = self.watcher.snapshot()
                target = directory or (
                    os.path.join(self.fallback_dir, name) if name and self.fallback_dir else "")

                if recording and not self.logging_active and target:
                    try:
                        self._begin(target, name)
                    except StwinError:
                        logger.exception("could not start sensor logging")
                        self._stop.wait(2.0)
                elif not recording and self.logging_active:
                    self._end()

                if self.logging_active:
                    self._drain()
                    time.sleep(DRAIN_INTERVAL_S)
                else:
                    if time.monotonic() - self._cached_at > STATUS_REFRESH_S:
                        self._refresh_status()
                    self._stop.wait(STATUS_POLL_S)
        finally:
            # Whatever happened, the board must not be left streaming.
            try:
                self._end()
            finally:
                self.watcher.stop()
                self.board.close()
                logger.info("sensor daemon stopped")
        return 0


def _firmware_info(status: dict) -> dict:
    for component in status.get("devices", [{}])[0].get("components", []):
        if "firmware_info" in component:
            return component["firmware_info"]
    return {}
