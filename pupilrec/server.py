"""HTTP front-end: live preview of every camera plus recording control.

Preview frames are shipped as multipart MJPEG.  They are the bytes the cameras
produced, except that a role configured with a preview_scale below 1.0 is
decoded and re-encoded smaller first (see preview.py): four full-size cameras
are more than the tablet's Wi-Fi can carry, and the preview only has to show
where a camera is pointed.  Recording is untouched by any of this -- it always
stores every captured frame at full size, whatever the preview is showing.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import re
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import shutil
import subprocess

from .gpstrack import TrackStore
from .preview import PreviewScaler
from .quarantine import NoGuard
from .tiles import TileCache
from .recording import RecordingSession, ffmpeg_path, join_name, split_name

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
BOUNDARY = "pupilframe"
STREAM_WRITE_TIMEOUT = 20.0     # seconds before a stalled preview client is dropped
_SAFE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

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

    def __init__(self, cfg, workers, guard=None):
        self.cfg = cfg
        self.workers = {w.cam_id: w for w in workers}
        # Cameras left out of this run because they froze the last one.  They
        # have no worker, so nothing else in here would know they exist.
        self.guard = guard if guard is not None else NoGuard()
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

        # One scaler per camera, so several viewers of the same camera shrink
        # each frame once between them instead of once each.
        self.scalers = {
            cam_id: PreviewScaler(cfg.scale_for(worker.role))
            for cam_id, worker in self.workers.items()
        }

        # config.json is rewritten whenever a camera control changes; two
        # clients turning knobs at once must not interleave inside the file.
        self.config_lock = threading.Lock()

        self.tracks = TrackStore(cfg.gps_dir)
        # Cameras attached since start-up, which need a restart to be used.
        self.pending_cameras: list[dict] = []
        self.tiles = TileCache(cfg.tiles_dir)

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

    def note_attached(self, attached) -> None:
        """Record which cameras are plugged in but not part of this session.

        Opening one now would freeze the process (see CameraSupervisor), so a
        newly attached camera is only reported; a restart picks it up.
        """
        known = {w.usb_path for w in self.workers.values()}
        self.pending_cameras = [
            {"usb_path": cam.usb_path, "product": cam.product, "role": cam.role,
             "port": cam.root_port}
            for cam in attached if cam.usb_path not in known
        ]

    def recording_path(self, name: str) -> str:
        """The directory of one stored recording, or a refusal.

        The name arrives from a client, so it is checked rather than trusted:
        it has to be a single path component naming a directory that really
        sits in the recordings directory.  realpath closes the last hole, a
        symlink there pointing somewhere else entirely.
        """
        if not name or name != os.path.basename(name) or name in (".", ".."):
            raise LookupError(f"Not a recording name: {name!r}")
        root = os.path.realpath(self.cfg.recordings_dir)
        path = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(path) != root or not os.path.isdir(path):
            raise LookupError(f"No such recording: {name}")
        return path

    def _refuse_if_running(self, name: str) -> None:
        if self.session is not None and self.session.name == name:
            raise ValueError(
                "This recording is still running. Stop it first.")

    def rename_recording(self, name: str, label: str) -> dict:
        """Change the label of a stored recording, keeping its timestamp.

        Only the label moves.  The timestamp is what orders the listing and
        what ties the directory to the times written inside it, so it is not
        the operator's to overwrite by accident.
        """
        with self.lock:
            self._refuse_if_running(name)
            path = self.recording_path(name)
            stamp, _ = split_name(name)
            new_name = join_name(stamp, label)
            if not new_name:
                raise ValueError("A recording needs a name.")
            target = os.path.join(os.path.dirname(path), new_name)
            if new_name != name and os.path.exists(target):
                raise ValueError(f"There is already a recording called {new_name}.")
            if new_name != name:
                os.rename(path, target)
        logger.info("recording renamed: %s -> %s", name, new_name)
        return {"name": new_name, "was": name,
                "metadata_updated": _rewrite_meta_name(target, new_name)}

    def delete_recording(self, name: str) -> dict:
        with self.lock:
            self._refuse_if_running(name)
            path = self.recording_path(name)
            freed = _directory_bytes(path)
            shutil.rmtree(path)
        logger.warning("recording deleted: %s (%d bytes)", name, freed)
        if self.last_result and self.last_result.get("name") == name:
            self.last_result = None     # do not report a recording that is gone
        return {"name": name, "freed_bytes": freed}

    def camera_controls(self, cam_id: str, refresh: bool = False) -> dict:
        """Every image control of one camera.

        Cached values by default; `refresh` re-reads them from the camera,
        which is slow enough to be the operator's decision rather than a
        side effect of opening the panel.
        """
        worker = self.workers[cam_id]
        return {"camera": cam_id, "side": worker.side, "role": worker.role,
                "product": worker.product, "refreshed": refresh,
                "controls": worker.list_controls(refresh_all=refresh)}

    def set_camera_control(self, cam_id: str, name: str, value: int) -> dict:
        """Write one control and persist it, so it survives a replug."""
        worker = self.workers[cam_id]
        controls = worker.set_control(name, value)
        with self.config_lock:
            # Store what the camera ended up with, not what was asked for: it
            # clamps values it cannot reach, and the stored value has to be the
            # one that will be written back on the next open.
            applied = next((c for c in controls if c["name"] == name), None)
            self.cfg.remember_control(
                cam_id, name, applied["value"] if applied else value)
            self.cfg.save()
        return {"camera": cam_id, "controls": controls}

    def reset_camera_controls(self, cam_id: str) -> dict:
        worker = self.workers[cam_id]
        controls = worker.reset_controls()
        with self.config_lock:
            self.cfg.forget_controls(cam_id)
            self.cfg.save()
        return {"camera": cam_id, "controls": controls}

    def start_recording(self, name: str, client: str = "") -> dict:
        with self.lock:
            if self.session is not None:
                # Another client won the race; report the running recording
                # rather than a bare failure, so the loser can just display it.
                raise AlreadyRecording(self.session.name, self.started_by)
            # Checked here so the refusal says what is wrong, before a directory
            # is made for a recording that cannot happen.  Without it the first
            # camera's muxer raises FileNotFoundError and the operator is told
            # "No such file or directory: 'ffmpeg'" with an empty directory left
            # behind.
            if not ffmpeg_path():
                raise RuntimeError(
                    "ffmpeg is not installed, or not on this service's PATH, "
                    "so nothing can be recorded. The live preview is unaffected."
                )
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


def _directory_bytes(path: str) -> int:
    """Bytes held by one recording.  Flat by construction, so one level does."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        logger.exception("could not measure %s", path)
    return total


def _rewrite_meta_name(path: str, new_name: str) -> bool:
    """Keep recording.json's own idea of its name in step with the directory.

    Best effort: the rename has already happened and is the real change, so a
    metadata file that cannot be updated is reported, not rolled back.
    """
    meta_path = os.path.join(path, "recording.json")
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
        meta["name"] = new_name
        with open(meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)
            fh.write("\n")
        return True
    except (OSError, json.JSONDecodeError):
        logger.warning("renamed the directory but could not update %s", meta_path)
        return False


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

    def _send_static(self, name: str, content_type: str):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                return self._send_bytes(fh.read(), content_type)
        except OSError:
            return self._error("Not found", HTTPStatus.NOT_FOUND)

    def _send_tile(self, spec: str):
        """/tiles/<z>/<x>/<y>.png -- from disk, or fetched once and kept."""
        parts = spec.split("/")
        if len(parts) != 3 or not parts[2].endswith(".png"):
            return self._error("Not found", HTTPStatus.NOT_FOUND)
        try:
            z, x, y = int(parts[0]), int(parts[1]), int(parts[2][:-4])
        except ValueError:
            return self._error("Not found", HTTPStatus.NOT_FOUND)

        body, source = self.state.tiles.get(z, x, y)
        if body is None:
            return self._error("Not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Tile-Source", source)
        # A cached tile is immutable for our purposes; a placeholder is not,
        # because the real one should be fetched next time there is a network.
        self.send_header("Cache-Control",
                         "public, max-age=604800" if source != "missing" else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_vendor(self, name: str):
        # Leaflet is served from here rather than a CDN so the map page needs
        # nothing from the internet but the tiles themselves.
        types = {".js": "application/javascript", ".css": "text/css"}
        suffix = os.path.splitext(name)[1]
        if suffix not in types or "/" in name or ".." in name:
            return self._error("Not found", HTTPStatus.NOT_FOUND)
        path = os.path.join(STATIC_DIR, "vendor", name)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error("Not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", types[suffix])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
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
            return self._send_static("index.html", "text/html; charset=utf-8")
        if path in ("/map", "/map.html"):
            return self._send_static("map.html", "text/html; charset=utf-8")
        if path.startswith("/tiles/"):
            return self._send_tile(path[len("/tiles/"):])
        if path.startswith("/vendor/"):
            return self._send_vendor(path[len("/vendor/"):])
        if path == "/api/gps/dates":
            return self._send_json({"dates": self.state.tracks.available_dates()})
        if path == "/api/gps/track":
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            day = query.get("date", [""])[0]
            if day and not _SAFE_DATE.fullmatch(day):
                return self._error("Bad date")
            return self._send_json(self.state.tracks.track(day, after))
        if path == "/api/status":
            return self._send_json(self.state.status())
        if path == "/api/health":
            return self._send_json(self._health())
        if path == "/api/recordings":
            return self._send_json({"recordings": self._list_recordings()})
        if path.startswith("/api/controls/"):
            return self._controls(path[len("/api/controls/"):], query)
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
            if url.path.startswith("/api/controls/"):
                return self._set_control(
                    url.path[len("/api/controls/"):], payload)
            if url.path in ("/api/recordings/rename", "/api/recordings/delete"):
                return self._edit_recording(url.path.rsplit("/", 1)[-1], payload)
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

    # -- stored recordings -------------------------------------------------

    def _edit_recording(self, action: str, payload):
        name = str(payload.get("name", ""))
        try:
            if action == "rename":
                return self._send_json(self.state.rename_recording(
                    name, str(payload.get("label", ""))))
            return self._send_json(self.state.delete_recording(name))
        except LookupError as exc:
            # Already gone, or never existed -- another client may have got
            # there first, which is not this one's fault.
            return self._error(str(exc), HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            return self._error(str(exc), HTTPStatus.CONFLICT)
        except OSError as exc:
            return self._error(f"Could not {action} it: {exc}",
                               HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- image controls ---------------------------------------------------

    def _controls(self, cam_id: str, query):
        if cam_id not in self.state.workers:
            return self._error("Unknown camera", HTTPStatus.NOT_FOUND)
        refresh = query.get("refresh", ["0"])[0] not in ("0", "", "false")
        try:
            return self._send_json(self.state.camera_controls(cam_id, refresh))
        except (RuntimeError, TimeoutError) as exc:
            # A camera that is unplugged or wedged cannot answer; that is a
            # state to report, not a server fault.
            return self._error(str(exc), HTTPStatus.SERVICE_UNAVAILABLE)

    def _set_control(self, cam_id: str, payload):
        if cam_id not in self.state.workers:
            return self._error("Unknown camera", HTTPStatus.NOT_FOUND)
        try:
            if payload.get("reset"):
                return self._send_json(self.state.reset_camera_controls(cam_id))
            name = str(payload.get("name", ""))
            if not name:
                return self._error("Which control?")
            try:
                value = int(payload["value"])
            except (KeyError, TypeError, ValueError):
                return self._error("A control value must be a whole number")
            return self._send_json(
                self.state.set_camera_control(cam_id, name, value))
        except TimeoutError as exc:
            return self._error(str(exc), HTTPStatus.SERVICE_UNAVAILABLE)
        except RuntimeError as exc:
            return self._error(str(exc), HTTPStatus.CONFLICT)

    # -- media -----------------------------------------------------------

    def _snapshot(self, cam_id: str):
        worker = self.state.workers.get(cam_id)
        if worker is None:
            return self._error("Unknown camera", HTTPStatus.NOT_FOUND)
        seq, frame = worker.latest_frame(after_seq=-1, timeout=5.0)
        if frame is None:
            return self._error("No frame available", HTTPStatus.SERVICE_UNAVAILABLE)
        jpeg = self.state.scalers[cam_id].scaled(seq, frame.jpeg)
        return self._send_bytes(jpeg, "image/jpeg")

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

        scaler = self.state.scalers[cam_id]
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
                    # After the throttle, so a frame nobody will see is never
                    # scaled: at 10 fps of 60 that is five sixths of the work.
                    jpeg = scaler.scaled(seq, frame.jpeg)
                    head = (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode()
                    self.wfile.write(head)
                    self.wfile.write(jpeg)
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
            "pending_cameras": self.state.pending_cameras,
            # The UI shows this before anyone presses record, not after.
            "ffmpeg": ffmpeg_path(),
            "quarantined_cameras": self.state.guard.report(),
            "disconnected_cameras": [c["id"] for c in cameras if not c["connected"]],
            "tiles": self.state.tiles.stats(),
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
        if target == "server":
            # Restarting the recorder kills the process answering this request,
            # so the reply has to go out first; otherwise a restart that worked
            # is reported to the operator as a failure.
            self._send_json({"target": target, "what": RECOVER_TARGETS[target],
                             "output": "restarting"})
            logger.warning("restart requested from %s", self.address_string())
            threading.Timer(0.5, subprocess.call,
                            args=(["sudo", "-n", RECOVER_HELPER, target],)).start()
            return None

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
        running = self.state.session.name if self.state.session else None
        for entry in sorted(os.listdir(root), reverse=True)[:50]:
            directory = os.path.join(root, entry)
            if not os.path.isdir(directory):
                continue
            meta_path = os.path.join(directory, "recording.json")
            stamp, label = split_name(entry)
            item = {"name": entry, "complete": False, "label": label,
                    "stamp": stamp, "running": entry == running,
                    "bytes": _directory_bytes(directory)}
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


def serve(cfg, workers, guard=None):
    state = AppState(cfg, workers, guard)
    Handler.state = state
    httpd = Server((cfg.host, cfg.port), Handler)
    return httpd, state
