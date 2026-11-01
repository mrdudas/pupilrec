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
import shutil
import subprocess
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

CSV_HEADER = "frame_index,unix_time,iso_time,monotonic_time,device_time,uvc_index,jpeg_bytes\n"

FFMPEG = "ffmpeg"

# How often a sink pushes its timestamp table to the kernel.  The table is
# buffered in this process, and a recorder that freezes is killed by the
# watchdog with the buffer still in it: the video would keep frames whose
# times were lost.  Once a second bounds that loss to a second.
TABLE_FLUSH_INTERVAL = 1.0

# The note that a recording is running, next to the quarantine note.  A run
# that finds it at start-up was killed mid-recording -- by the watchdog, as a
# rule, after a camera froze the process -- and carries on in the same
# directory rather than leaving the operator with a stopped recording that
# nobody stopped.
ACTIVE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var",
    "active_recording.json")
# The note is refreshed this often while the recording runs...
HEARTBEAT_INTERVAL = 5.0
# ...and is only acted on if it was refreshed this recently.  A restart within
# seconds of a freeze is the case this is for.  A machine switched back on
# the next morning is not: resuming a recording nobody is attending would
# quietly fill the disk.
RESUME_WINDOW = 600.0


def ffmpeg_path() -> str:
    """Where ffmpeg is, or "" if it is not on PATH.

    Worth asking before a recording rather than during one.  A recorder with no
    ffmpeg streams, previews and reports itself healthy in every way -- and then
    fails the moment someone presses record, which is the one moment that cannot
    be repeated.  It happened on the macOS port, where launchd hands a daemon a
    PATH without /usr/local/bin in it.
    """
    return shutil.which(FFMPEG) or ""

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

    def __init__(self, directory: str, cam_id: str, width: int, height: int, fps: int,
                 segment: int = 1):
        self.cam_id = cam_id
        self.fps = fps
        # The first segment keeps the plain names every existing recording
        # has; one resumed after a restart must not overwrite them.
        stem = segment_stem(cam_id, segment)
        self.video_path = os.path.join(directory, f"{stem}.mkv")
        self.table_path = os.path.join(directory, f"{stem}.csv")

        self.written = 0
        self.dropped_queue = 0
        self.first_unix = None
        self.last_unix = None

        self._queue: queue.Queue = queue.Queue(maxsize=max(60, fps * 2))
        self._stop = object()          # sentinel

        self._ffmpeg = subprocess.Popen(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error",
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
        flushed = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=TABLE_FLUSH_INTERVAL)
            except queue.Empty:
                item = None             # a camera that is away: flush anyway
            now = time.monotonic()
            if now - flushed >= TABLE_FLUSH_INTERVAL:
                flushed = now
                try:
                    self._table.flush()
                except (OSError, ValueError):
                    logger.exception("%s: could not flush the timestamp table",
                                     self.cam_id)
            if item is None:
                continue
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


def segment_stem(cam_id: str, segment: int) -> str:
    """File name, without extension, of one camera's part of one segment."""
    return cam_id if segment <= 1 else f"{cam_id}.part{segment}"


def _table_summary(path: str) -> tuple[int, float | None]:
    """(rows, last unix_time) of a timestamp table a killed process left behind.

    Tolerates a last line cut off mid-write: it is not counted.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0, None
    lines = data.split(b"\n")
    complete = [line for line in lines[1:-1] if line]   # header, and the unterminated tail
    last = None
    if complete:
        try:
            last = float(complete[-1].split(b",")[1])
        except (IndexError, ValueError):
            pass
    return len(complete), last


class ActiveNote:
    """The note on disk that says a recording is running, and where.

    Written when a recording starts, refreshed while it runs, removed when it
    is stopped.  Only a process that dies without stopping leaves it behind.
    """

    def __init__(self, path: str | None):
        self.path = path
        self._payload: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, directory: str, started_by: str) -> None:
        if not self.path:
            return
        self._payload = {"directory": directory, "started_by": started_by}
        self._write()
        self._thread = threading.Thread(target=self._beat, name="recording-note",
                                        daemon=True)
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            self._write()

    def _write(self) -> None:
        payload = dict(self._payload, alive_unix=time.time())
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            # Costs the resume after a crash, never the recording itself.
            logger.warning("could not write %s: %s", self.path, exc)

    def clear(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.path:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("could not remove %s: %s", self.path, exc)


def find_resumable(recordings_dir: str, path: str = ACTIVE_PATH,
                   window: float = RESUME_WINDOW) -> dict | None:
    """The recording a killed run left unfinished, if it should be continued.

    -> {"directory", "started_by", "meta", "cameras"} or None.  A note that is
    too old, or that points at something that is not an unfinished recording
    in this recordings directory, is removed and ignored.
    """
    try:
        with open(path) as fh:
            note = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable %s: %s", path, exc)
        note = {}

    def discard(why: str) -> None:
        logger.warning("not resuming the interrupted recording: %s", why)
        try:
            os.remove(path)
        except OSError:
            pass

    directory = note.get("directory") or ""
    age = time.time() - float(note.get("alive_unix") or 0)
    if age > window:
        discard(f"it stopped {age / 60:.0f} minutes ago")
        return None
    root = os.path.realpath(recordings_dir)
    if not directory or os.path.dirname(os.path.realpath(directory)) != root:
        discard(f"{directory!r} is not in {recordings_dir}")
        return None
    try:
        with open(os.path.join(directory, "recording.json")) as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as exc:
        discard(f"{directory}: {exc}")
        return None
    if meta.get("complete"):
        discard(f"{directory} was finished")
        return None
    cameras = {c["camera"]: c for c in meta.get("cameras", []) if c.get("camera")}
    for segment in meta.get("segments", []):
        for cam in segment.get("cameras", []):
            if cam.get("camera"):
                cameras.setdefault(cam["camera"], cam)
    return {"directory": directory, "started_by": note.get("started_by", ""),
            "alive_unix": note.get("alive_unix"), "meta": meta,
            "cameras": list(cameras.values())}


class RecordingSession:
    """One directory holding a synchronised set of recordings.

    Normally one segment.  A recording interrupted by the process dying -- the
    watchdog killing a recorder a camera froze -- is continued by the next run
    as a further segment in the same directory: `<camera>.part2.mkv` and its
    table, and so on.  Each segment's frame_index starts at 0; unix_time and
    monotonic_time run on across segments, so the tables still line up.
    """

    def __init__(self, root: str, workers, name: str = "",
                 note_path: str | None = None, started_by: str = "",
                 resume: dict | None = None):
        if resume:
            self.directory = resume["directory"]
            self.name = os.path.basename(self.directory)
            meta = resume["meta"]
            self.started_unix = meta.get("started_unix") or time.time()
            self._known_cameras = list(resume.get("cameras") or meta.get("cameras", []))
            self._segments = self._close_interrupted(meta, resume.get("alive_unix"))
        else:
            self.name = join_name(datetime.now().strftime(STAMP_FORMAT), name)
            self.directory = os.path.join(root, self.name)
            os.makedirs(self.directory, exist_ok=False)
            self.started_unix = None
            self._known_cameras = []
            self._segments = []

        self.segment = len(self._segments) + 1
        self.segment_started_unix = time.time()
        self.started_monotonic = time.monotonic()
        if self.started_unix is None:
            self.started_unix = self.segment_started_unix
        # So that elapsed counts from the start of the whole recording.
        self._elapsed_base = self.segment_started_unix - self.started_unix
        self.stopped_unix = None
        self._workers = list(workers)
        self._sinks: dict[str, CameraSink] = {}
        # Frames already on disk from earlier segments, for the live counts.
        self._prior_frames: dict[str, int] = {}
        for seg in self._segments:
            for r in seg.get("results") or []:
                self._prior_frames[r["camera"]] = (
                    self._prior_frames.get(r["camera"], 0) + int(r.get("frames") or 0))

        for worker in self._workers:
            sink = CameraSink(self.directory, worker.cam_id,
                              worker.width, worker.height, worker.fps,
                              segment=self.segment)
            self._sinks[worker.cam_id] = sink
            worker.attach_sink(sink)

        self._write_metadata(final=False)
        self._note = ActiveNote(note_path)
        self._note.start(self.directory, started_by)
        if resume:
            logger.warning("recording resumed after an interruption: %s (segment %d)",
                           self.directory, self.segment)
        else:
            logger.info("recording started: %s", self.directory)

    @property
    def resumed(self) -> bool:
        return self.segment > 1

    def _close_interrupted(self, meta: dict, alive_unix) -> list[dict]:
        """Earlier segments, with the one the dead process left open filled in."""
        segments = [dict(seg) for seg in meta.get("segments") or []]
        if not segments:
            # Written before recordings had segments: it was all one.
            segments = [{
                "index": 1,
                "started_unix": meta.get("started_unix"),
                "clock_offset_unix_minus_monotonic":
                    meta.get("clock_offset_unix_minus_monotonic"),
                "cameras": meta.get("cameras", []),
                "stopped_unix": None,
            }]
        last = segments[-1]
        if last.get("stopped_unix") is None:
            results, ends = [], []
            for cam in last.get("cameras", []):
                table = os.path.join(self.directory, cam["table"])
                video = os.path.join(self.directory, cam["video"])
                rows, last_unix = _table_summary(table)
                if last_unix is not None:
                    ends.append(last_unix)
                results.append({
                    "camera": cam["camera"], "video": cam["video"],
                    "table": cam["table"], "frames": rows,
                    "last_unix": last_unix,
                    "size_bytes": os.path.getsize(video) if os.path.exists(video) else 0,
                })
            last["results"] = results
            last["stopped_unix"] = max(ends) if ends else alive_unix
            last["ended"] = "interrupted"
        return segments

    @property
    def elapsed(self) -> float:
        return self._elapsed_base + time.monotonic() - self.started_monotonic

    def live_counts(self) -> dict[str, dict]:
        return {
            cam_id: {"frames": self._prior_frames.get(cam_id, 0) + s.written,
                     "dropped_queue": s.dropped_queue}
            for cam_id, s in self._sinks.items()
        }

    def _camera_meta(self, segment: int) -> list[dict]:
        return [
            {
                "camera": w.cam_id, "side": w.side, "role": w.role,
                "product": w.product, "usb_path": w.usb_path, "uid": w.uid,
                "width": w.width, "height": w.height, "nominal_fps": w.fps,
                "video": f"{segment_stem(w.cam_id, segment)}.mkv",
                "table": f"{segment_stem(w.cam_id, segment)}.csv",
            }
            for w in self._workers
        ]

    def _all_cameras(self) -> list[dict]:
        """Every camera of the recording under its first segment's file names."""
        cams = {c["camera"]: c for c in self._known_cameras if c.get("camera")}
        for cam in self._camera_meta(1):
            cams.setdefault(cam["camera"], cam)
        return [cams[k] for k in sorted(cams)]

    def _current_segment(self, results=None) -> dict:
        seg = {
            "index": self.segment,
            "started_unix": self.segment_started_unix,
            "clock_offset_unix_minus_monotonic":
                self.segment_started_unix - self.started_monotonic,
            "cameras": self._camera_meta(self.segment),
            "stopped_unix": self.stopped_unix,
        }
        if results is not None:
            seg["ended"] = "stopped"
            seg["results"] = results
        return seg

    def _write_metadata(self, final: bool, results=None) -> None:
        segments = self._segments + [self._current_segment(results if final else None)]
        meta = {
            "name": self.name,
            "started_unix": self.started_unix,
            "started_iso": datetime.fromtimestamp(self.started_unix).isoformat(timespec="microseconds"),
            # Lets the monotonic column in each CSV be mapped onto wall clock time
            # even if the system clock is adjusted mid-recording.  Per segment
            # in "segments"; this one is the first segment's.
            "clock_offset_unix_minus_monotonic":
                segments[0].get("clock_offset_unix_minus_monotonic"),
            "video_codec": "mjpeg (stream copy, no re-encode)",
            "container": "matroska",
            "cameras": self._all_cameras(),
            "segments": segments,
            "complete": final,
        }
        if final:
            meta["stopped_unix"] = self.stopped_unix
            meta["stopped_iso"] = datetime.fromtimestamp(self.stopped_unix).isoformat(timespec="microseconds")
            meta["duration_s"] = round(self.stopped_unix - self.started_unix, 3)
            meta["interruptions"] = sum(1 for s in segments if s.get("ended") == "interrupted")
            meta["results"] = (results if len(segments) == 1
                               else self._combined_results(segments))
        path = os.path.join(self.directory, "recording.json")
        tmp = path + ".tmp"
        # Through a rename: this file is rewritten on a resume, and a process
        # killed half way through writing it would otherwise lose the record of
        # every earlier segment.
        with open(tmp, "w") as fh:
            json.dump(meta, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)

    @staticmethod
    def _combined_results(segments: list[dict]) -> list[dict]:
        """One entry per camera, summed over every segment it appears in."""
        combined: dict[str, dict] = {}
        for seg in segments:
            for r in seg.get("results") or []:
                c = combined.setdefault(r["camera"], {
                    "camera": r["camera"], "frames": 0, "dropped_queue": 0,
                    "size_bytes": 0, "videos": [], "tables": []})
                c["frames"] += int(r.get("frames") or 0)
                c["dropped_queue"] += int(r.get("dropped_queue") or 0)
                c["size_bytes"] += int(r.get("size_bytes") or 0)
                c["videos"].append(r.get("video"))
                c["tables"].append(r.get("table"))
        return [combined[k] for k in sorted(combined)]

    def stop(self) -> dict:
        for worker in self._workers:
            worker.detach_sink()
        results = [self._sinks[w.cam_id].close() for w in self._workers]
        self.stopped_unix = time.time()
        self._write_metadata(final=True, results=results)
        self._note.clear()
        logger.info("recording stopped: %s", self.directory)
        totals = {cam_id: c["frames"] for cam_id, c in self.live_counts().items()}
        cameras = [dict(r, frames=totals.get(r["camera"], r["frames"]))
                   for r in results]
        return {
            "name": self.name,
            "directory": self.directory,
            "duration_s": round(self.stopped_unix - self.started_unix, 3),
            "segments": self.segment,
            "cameras": cameras,
        }
