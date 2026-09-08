"""HTTP front-end: live preview of every camera plus recording control.

Preview frames are shipped as multipart MJPEG, which is the same bytes the
cameras produced -- the server never decodes or re-encodes an image.  Recording
always stores every captured frame regardless of what the preview is showing.
"""

from __future__ import annotations

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

from .recording import RecordingSession

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
BOUNDARY = "pupilframe"


class AppState:
    """Everything the request handlers share."""

    def __init__(self, cfg, workers):
        self.cfg = cfg
        self.workers = {w.cam_id: w for w in workers}
        self.session: RecordingSession | None = None
        self.lock = threading.Lock()
        self.last_result: dict | None = None

    def start_recording(self, name: str) -> dict:
        with self.lock:
            if self.session is not None:
                raise RuntimeError("A recording is already running.")
            os.makedirs(self.cfg.recordings_dir, exist_ok=True)
            self.session = RecordingSession(
                self.cfg.recordings_dir, self.workers.values(), name
            )
            return {"name": self.session.name, "directory": self.session.directory}

    def stop_recording(self) -> dict:
        with self.lock:
            if self.session is None:
                raise RuntimeError("No recording is running.")
            session, self.session = self.session, None
        result = session.stop()
        self.last_result = result
        return result

    def status(self) -> dict:
        with self.lock:
            session = self.session
        counts = session.live_counts() if session else {}
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
            })
        return {
            "recording": session is not None,
            "recording_name": session.name if session else None,
            "elapsed_s": round(session.elapsed, 1) if session else 0.0,
            "cameras": cameras,
            "last_result": self.last_result,
            "server_time": time.time(),
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
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

        try:
            if url.path == "/api/start":
                return self._send_json(self.state.start_recording(payload.get("name", "")))
            if url.path == "/api/stop":
                return self._send_json(self.state.stop_recording())
        except RuntimeError as exc:
            return self._error(str(exc), HTTPStatus.CONFLICT)
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

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        seq = -1
        next_due = 0.0
        try:
            while True:
                seq, frame = worker.latest_frame(after_seq=seq, timeout=5.0)
                if frame is None:
                    continue                       # camera silent; keep waiting
                now = time.monotonic()
                if now < next_due:
                    continue                       # throttle: skip this frame
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
