"""Writing captured JPEG frames to disk without re-encoding them.

The cameras already produce JPEG, so recording means muxing those exact bytes
into a container -- ffmpeg runs with `-c:v copy`, which costs almost no CPU and
leaves the image data bit-identical to what the sensor sent.  Files are large by
design; that is the trade being made for the CPU.

Alongside each video goes a CSV table of one row per stored frame, so a frame
number in the video can be turned back into an exact wall clock time.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

CSV_HEADER = "frame_index,unix_time,iso_time,monotonic_time,device_time,uvc_index,jpeg_bytes\n"

# A recording directory is "<date>_<time>" plus an optional label.  The stamp is
# what orders the listing and ties the directory to the times inside it, so only
# the label is ever the operator's to choose -- when starting a recording and
# when renaming one later, by the same rule in both cases.
STAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
STAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
LABEL_MAX = 40


def safe_label(name: str) -> str:
    """The part of a directory name a person gets to pick, made safe.

    Anything that could change what path this points at is dropped rather than
    rejected: a name is a convenience, and refusing one mid-session would cost
    a recording.
    """
    # Spaces become underscores rather than vanishing, so "két kamera" reads as
    # "két_kamera" instead of "kétkamera".
    collapsed = "_".join(name.split())
    return "".join(c for c in collapsed if c.isalnum() or c in "-_")[:LABEL_MAX]


def split_name(directory: str) -> tuple[str, str]:
    """A recording directory name as (timestamp, label). -> ("", name) if odd."""
    match = STAMP_PATTERN.match(directory)
    if not match or match.start() != 0:
        return "", directory
    return match.group(), directory[match.end():].lstrip("_")


def join_name(stamp: str, label: str) -> str:
    label = safe_label(label)
    if not stamp:
        return label
    return f"{stamp}_{label}" if label else stamp


class CameraSink:
    """Receives frames from one capture thread and persists them.

    The capture thread must never block on disk, so frames go through a bounded
    queue.  If the queue fills (disk stall), frames are dropped and counted
    rather than stalling the camera, which would corrupt timing for every other
    camera sharing the USB bus.
    """

    def __init__(self, directory: str, cam_id: str, width: int, height: int, fps: int):
        self.cam_id = cam_id
        self.fps = fps
        self.video_path = os.path.join(directory, f"{cam_id}.mkv")
        self.table_path = os.path.join(directory, f"{cam_id}.csv")

        self.written = 0
        self.dropped_queue = 0
        self.first_unix = None
        self.last_unix = None

        self._queue: queue.Queue = queue.Queue(maxsize=max(60, fps * 2))
        self._stop = object()          # sentinel

        self._ffmpeg = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "mjpeg", "-framerate", str(fps),
                "-i", "pipe:0",
                "-c:v", "copy",          # no transcode: the JPEGs are stored as-is
                "-f", "matroska",
                "-y", self.video_path,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self._table = open(self.table_path, "w", buffering=1 << 16)
        self._table.write(CSV_HEADER)

        self._thread = threading.Thread(target=self._run, name=f"sink-{cam_id}", daemon=True)
        self._thread.start()

    def write(self, frame) -> None:
        """Called from the capture thread. Never blocks."""
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self.dropped_queue += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._stop:
                break
            try:
                self._ffmpeg.stdin.write(item.jpeg)
            except (BrokenPipeError, ValueError):
                logger.error("%s: ffmpeg pipe closed early", self.cam_id)
                break
            iso = datetime.fromtimestamp(item.unix_time).isoformat(timespec="microseconds")
            self._table.write(
                f"{self.written},{item.unix_time:.6f},{iso},{item.monotonic:.6f},"
                f"{item.device_time:.6f},{item.uvc_index},{len(item.jpeg)}\n"
            )
            if self.first_unix is None:
                self.first_unix = item.unix_time
            self.last_unix = item.unix_time
            self.written += 1

    def close(self) -> dict:
        self._queue.put(self._stop)
        self._thread.join(timeout=30)

        try:
            self._ffmpeg.stdin.close()
        except Exception:
            pass
        try:
            self._ffmpeg.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.error("%s: ffmpeg did not exit, terminating", self.cam_id)
            self._ffmpeg.terminate()
        stderr = self._ffmpeg.stderr.read().decode(errors="replace").strip()
        self._ffmpeg.stderr.close()
        self._table.close()

        if stderr:
            logger.warning("%s: ffmpeg said: %s", self.cam_id, stderr)

        duration = (self.last_unix - self.first_unix) if self.written > 1 else 0.0
        return {
            "camera": self.cam_id,
            "video": os.path.basename(self.video_path),
            "table": os.path.basename(self.table_path),
            "frames": self.written,
            "dropped_queue": self.dropped_queue,
            "first_unix": self.first_unix,
            "last_unix": self.last_unix,
            "duration_s": round(duration, 3),
            "measured_fps": round(self.written / duration, 2) if duration > 0 else 0.0,
            "size_bytes": os.path.getsize(self.video_path) if os.path.exists(self.video_path) else 0,
            "ffmpeg_stderr": stderr,
        }


class RecordingSession:
    """One directory holding a synchronised set of recordings."""

    def __init__(self, root: str, workers, name: str = ""):
        self.name = join_name(datetime.now().strftime(STAMP_FORMAT), name)
        self.directory = os.path.join(root, self.name)
        os.makedirs(self.directory, exist_ok=False)

        self.started_unix = time.time()
        self.started_monotonic = time.monotonic()
        self.stopped_unix = None
        self._workers = list(workers)
        self._sinks: dict[str, CameraSink] = {}

        for worker in self._workers:
            sink = CameraSink(self.directory, worker.cam_id,
                              worker.width, worker.height, worker.fps)
            self._sinks[worker.cam_id] = sink
            worker.attach_sink(sink)

        self._write_metadata(final=False)
        logger.info("recording started: %s", self.directory)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def live_counts(self) -> dict[str, dict]:
        return {
            cam_id: {"frames": s.written, "dropped_queue": s.dropped_queue}
            for cam_id, s in self._sinks.items()
        }

    def _camera_meta(self) -> list[dict]:
        return [
            {
                "camera": w.cam_id, "side": w.side, "role": w.role,
                "product": w.product, "usb_path": w.usb_path, "uid": w.uid,
                "width": w.width, "height": w.height, "nominal_fps": w.fps,
                "video": f"{w.cam_id}.mkv", "table": f"{w.cam_id}.csv",
            }
            for w in self._workers
        ]

    def _write_metadata(self, final: bool, results=None) -> None:
        meta = {
            "name": self.name,
            "started_unix": self.started_unix,
            "started_iso": datetime.fromtimestamp(self.started_unix).isoformat(timespec="microseconds"),
            # Lets the monotonic column in each CSV be mapped onto wall clock time
            # even if the system clock is adjusted mid-recording.
            "clock_offset_unix_minus_monotonic": self.started_unix - self.started_monotonic,
            "video_codec": "mjpeg (stream copy, no re-encode)",
            "container": "matroska",
            "cameras": self._camera_meta(),
            "complete": final,
        }
        if final:
            meta["stopped_unix"] = self.stopped_unix
            meta["stopped_iso"] = datetime.fromtimestamp(self.stopped_unix).isoformat(timespec="microseconds")
            meta["duration_s"] = round(self.stopped_unix - self.started_unix, 3)
            meta["results"] = results
        with open(os.path.join(self.directory, "recording.json"), "w") as fh:
            json.dump(meta, fh, indent=2)
            fh.write("\n")

    def stop(self) -> dict:
        for worker in self._workers:
            worker.detach_sink()
        results = [self._sinks[w.cam_id].close() for w in self._workers]
        self.stopped_unix = time.time()
        self._write_metadata(final=True, results=results)
        logger.info("recording stopped: %s", self.directory)
        return {
            "name": self.name,
            "directory": self.directory,
            "duration_s": round(self.stopped_unix - self.started_unix, 3),
            "cameras": results,
        }
