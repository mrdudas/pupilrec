"""HTTP front-end: live preview of every camera plus recording control.

Preview frames are shipped as multipart MJPEG, which is the same bytes the
cameras produced -- the server never decodes or re-encodes an image.  Recording
always stores every captured frame regardless of what the preview is showing.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import shutil
import subprocess

from .recording import RecordingSession

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
BOUNDARY = "pupilframe"
STREAM_WRITE_TIMEOUT = 20.0     # seconds before a stalled preview client is dropped

# Privileged recovery actions, run through a sudoers rule that allows exactly
# these three arguments and nothing else (setup/pupilrec-sudoers).
RECOVER_HELPER = "/usr/local/sbin/pupilrec-recover"
RECOVER_TARGETS = {
    "sensors": "sensor daemon",
    "server": "camera recorder",
    "board": "sensor board USB power",
}


class AlreadyRecording(Exception):
    """Raised when a client asks to start while another already did."""

    def __init__(self, name: str, started_by: str):
        super().__init__(f"A recording is already running: {name}")
        self.name = name
        self.started_by = started_by


class NotRecording(Exception):
    """Raised when a client asks to stop a recording that is already stopped."""

    def __init__(self):
        super().__init__("No recording is running.")


class AppState:
    """Everything the request handlers share.

    This object is the single source of truth for what the system is doing.
    Clients hold no state of their own: they render whatever /api/status says,
    so a client can disappear and come back, or several can watch at once,
    without any of them disagreeing about whether a recording is running.
    """

    def __init__(self, cfg, workers):
        self.cfg = cfg
        self.workers = {w.cam_id: w for w in workers}
        self.session: RecordingSession | None = None
        self.lock = threading.Lock()
        self.last_result: dict | None = None

        # Recording state lives here and nowhere else, so it survives any
        # client going away and is identical for every client that asks.
        # The version bumps on each change: a client that reconnects, or one
        # that never disconnected, can tell "something changed while I was not
        # looking" apart from "nothing has happened".
        self.state_version = 0
        self.started_by = ""       # client id that started the current recording
        self.stopped_by = ""       # client id that stopped the last one

        # Live preview viewers per camera, only for display.
        self.viewers = collections.Counter()
        self.viewers_lock = threading.Lock()

    @contextlib.contextmanager
    def viewer(self, cam_id: str):
        with self.viewers_lock:
            self.viewers[cam_id] += 1
        try:
            yield
        finally:
            with self.viewers_lock:
                self.viewers[cam_id] -= 1
                if self.viewers[cam_id] <= 0:
                    del self.viewers[cam_id]

    def start_recording(self, name: str, client: str = "") -> dict:
        with self.lock:
            if self.session is not None:
                # Another client won the race; report the running recording
                # rather than a bare failure, so the loser can just display it.
                raise AlreadyRecording(self.session.name, self.started_by)
            os.makedirs(self.cfg.recordings_dir, exist_ok=True)
            self.session = RecordingSession(
                self.cfg.recordings_dir, self.workers.values(), name
            )
            self.started_by = client
            self.state_version += 1
            return {"name": self.session.name, "directory": self.session.directory,
                    "state_version": self.state_version}

    def stop_recording(self, client: str = "") -> dict:
        with self.lock:
            if self.session is None:
                raise NotRecording()
            session, self.session = self.session, None
            self.stopped_by = client
            self.state_version += 1
            version = self.state_version
        result = session.stop()
        result["state_version"] = version
        result["stopped_by"] = client
        self.last_result = result
        return result

    def status(self) -> dict:
        with self.lock:
            session = self.session
        counts = session.live_counts() if session else {}
        with self.viewers_lock:
            viewers = dict(self.viewers)
        cameras = []
        for cam_id, worker in sorted(self.workers.items()):
            stats = worker.stats
            cameras.append({
                "id": cam_id,
                "side": worker.side,
                "role": worker.role,
                "product": worker.product,
                "usb_path": worker.usb_path,
                "width": worker.width,
                "height": worker.height,
                "nominal_fps": worker.fps,
                "connected": stats.connected,
                "fps": round(stats.fps, 1),
                "frames": stats.frames,
                "dropped_stream": stats.dropped_stream,
                "reopens": stats.reopens,
                "error": stats.error,
                "recorded": counts.get(cam_id, {}).get("frames", 0),
                "dropped_queue": counts.get(cam_id, {}).get("dropped_queue", 0),
                "viewers": viewers.get(cam_id, 0),
            })
        return {
            "recording": session is not None,
            "recording_name": session.name if session else None,
            # The sensor daemon writes its CSVs into this same directory.
            "recording_directory": session.directory if session else None,
            "elapsed_s": round(session.elapsed, 1) if session else 0.0,
            "state_version": self.state_version,
            "started_by": self.started_by if session else "",
            "stopped_by": self.stopped_by,
            "cameras": cameras,
            "last_result": self.last_result,
            "server_time": time.time(),
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Reap keep-alive connections whose client vanished without closing, so a
    # client that drops off the network repeatedly does not pile up threads.
    timeout = 60
    state: AppState = None          # injected by serve()

    def log_message(self, fmt, *args):      # quieter than the default
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ---------------------------------------------------------

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status=HTTPStatus.BAD_REQUEST):
        self._send_json({"error": message}, status)

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                return self._send_bytes(fh.read(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send_json(self.state.status())
        if path == "/api/health":
            return self._send_json(self._health())
        if path == "/api/recordings":
            return self._send_json({"recordings": self._list_recordings()})
        if path.startswith("/snapshot/"):
            return self._snapshot(path.rsplit("/", 1)[-1])
        if path.startswith("/stream/"):
            return self._stream(path.rsplit("/", 1)[-1], query)
        return self._error("Not found", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        client = str(payload.get("client", ""))[:64]
        try:
            if url.path == "/api/start":
                return self._send_json(
                    self.state.start_recording(payload.get("name", ""), client))
            if url.path == "/api/stop":
                return self._send_json(self.state.stop_recording(client))
            if url.path == "/api/restart":
                return self._restart(payload)
        except AlreadyRecording as exc:
            # Two clients pressed start at once.  The loser is not in error --
            # it just needs to know a recording is running and who owns it.
            return self._send_json(
                {"error": str(exc), "recording": exc.name, "started_by": exc.started_by,
                 "already": True},
                HTTPStatus.CONFLICT)
        except NotRecording as exc:
            # Someone else stopped it first; the client's next poll will agree.
            return self._send_json({"error": str(exc), "already": True},
                                   HTTPStatus.CONFLICT)
        except Exception as exc:
            logger.exception("request failed")
            return self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._error("Not found", HTTPStatus.NOT_FOUND)

    # -- media -----------------------------------------------------------

    def _snapshot(self, cam_id: str):
        worker = self.state.workers.get(cam_id)
        if worker is None:
            return self._error("Unknown camera", HTTPStatus.NOT_FOUND)
        _, frame = worker.latest_frame(after_seq=-1, timeout=5.0)
        if frame is None:
            return self._error("No frame available", HTTPStatus.SERVICE_UNAVAILABLE)
        return self._send_bytes(frame.jpeg, "image/jpeg")

    def _stream(self, cam_id: str, query):
        worker = self.state.workers.get(cam_id)
        if worker is None:
            return self._error("Unknown camera", HTTPStatus.NOT_FOUND)

        try:
            target_fps = float(query.get("fps", [0])[0])
        except ValueError:
            target_fps = 0.0
        if target_fps <= 0:
            target_fps = self.state.cfg.preview_fps.get(worker.role, 10.0)
        min_interval = 1.0 / target_fps

        # When a viewer disappears mid-stream (Wi-Fi drop, iPad asleep) the write
        # below would otherwise block until TCP gives up, minutes later, holding
        # a thread and a stale viewer count.
        self.connection.settimeout(STREAM_WRITE_TIMEOUT)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        seq = -1
        next_due = 0.0
        try:
            with self.state.viewer(cam_id):
                while True:
                    seq, frame = worker.latest_frame(after_seq=seq, timeout=5.0)
                    if frame is None:
                        continue                   # camera silent; keep waiting
                    now = time.monotonic()
                    if now < next_due:
                        continue                   # throttle: skip this frame
                    next_due = now + min_interval
                    head = (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame.jpeg)}\r\n\r\n"
                    ).encode()
                    self.wfile.write(head)
                    self.wfile.write(frame.jpeg)
                    self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass                                    # viewer navigated away

    def _service_state(self, unit: str) -> str:
        try:
            out = subprocess.run(["systemctl", "is-active", unit],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def _health(self) -> dict:
        from gpslog.daemon import HEARTBEAT_PATH as GPS_HEARTBEAT
        from sensorlog.heartbeat import read as read_heartbeat

        cameras = self.state.status()["cameras"]
        return {
            "cameras": {
                "connected": sum(1 for c in cameras if c["connected"]),
                "total": len(cameras),
                "streaming": all(c["connected"] for c in cameras) and bool(cameras),
            },
            "sensors": read_heartbeat(),
            # The GNSS receiver is optional equipment; absent is a normal state.
            "gps": read_heartbeat(GPS_HEARTBEAT),
            "services": {
                "pupilrec": self._service_state("pupilrec.service"),
                "pupilrec-sensors": self._service_state("pupilrec-sensors.service"),
                "pupilrec-gps": self._service_state("pupilrec-gps.service"),
            },
            "recovery_available": os.access(RECOVER_HELPER, os.X_OK),
            "recording": self.state.status()["recording"],
        }

    def _restart(self, payload) -> None:
        target = str(payload.get("target", ""))
        if target not in RECOVER_TARGETS:
            return self._error("Unknown restart target")
        if not os.access(RECOVER_HELPER, os.X_OK):
            return self._error("Recovery helper is not installed; see setup/install.sh",
                               HTTPStatus.NOT_IMPLEMENTED)
        with self.state.lock:
            recording = self.state.session is not None
        if recording and not payload.get("force"):
            # Restarting mid-recording throws away what has been captured, so
            # it takes a deliberate second press rather than one stray tap.
            return self._send_json(
                {"error": "A recording is running. Stop it first, or confirm to "
                          "restart anyway and lose it.",
                 "needs_force": True}, HTTPStatus.CONFLICT)
        try:
            done = subprocess.run(["sudo", "-n", RECOVER_HELPER, target],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._error(f"Could not run recovery: {exc}",
                               HTTPStatus.INTERNAL_SERVER_ERROR)
        if done.returncode != 0:
            return self._error(
                (done.stderr or done.stdout or "recovery failed").strip()[:300],
                HTTPStatus.INTERNAL_SERVER_ERROR)
        logger.warning("recovery action %r requested from %s", target, self.address_string())
        return self._send_json({
            "target": target,
            "what": RECOVER_TARGETS[target],
            "output": (done.stdout or "").strip()[:500],
        })

    def _list_recordings(self):
        root = self.state.cfg.recordings_dir
        out = []
        if not os.path.isdir(root):
            return out
        for entry in sorted(os.listdir(root), reverse=True)[:50]:
            meta_path = os.path.join(root, entry, "recording.json")
            item = {"name": entry, "complete": False}
            try:
                with open(meta_path) as fh:
                    meta = json.load(fh)
                item.update({
                    "complete": meta.get("complete", False),
                    "duration_s": meta.get("duration_s"),
                    "started_iso": meta.get("started_iso"),
                    "frames": {r["camera"]: r["frames"] for r in meta.get("results", [])},
                })
            except (OSError, json.JSONDecodeError, KeyError):
                pass
            out.append(item)
        return out


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def local_addresses(port: int) -> list[str]:
    """Best-effort list of URLs the iPad can use."""
    urls = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))          # no packet is sent
        urls.append(f"http://{probe.getsockname()[0]}:{port}/")
        probe.close()
    except OSError:
        pass
    urls.append(f"http://{socket.gethostname()}.local:{port}/")
    return urls


def serve(cfg, workers):
    state = AppState(cfg, workers)
    Handler.state = state
    httpd = Server((cfg.host, cfg.port), Handler)
    return httpd, state
