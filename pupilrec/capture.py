"""One capture thread per camera, fanning JPEG frames out to preview and recording.

Frames are never decoded.  The cameras emit MJPEG, the browser eats MJPEG and
the recorder muxes MJPEG, so a frame is copied out of the driver buffer once and
then only ever passed around as bytes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import uvc

logger = logging.getLogger(__name__)

# libuvc hands back a view into a buffer it reuses on the next call, so a frame
# has to be copied before asking for the following one.  This is the only copy.
GRAB_TIMEOUT = 1.0        # seconds; bounds how long stop() waits on a dead camera
REOPEN_DELAY = 2.0        # seconds between reconnect attempts
# A camera that has been unplugged does not raise; it simply stops delivering,
# and libuvc keeps timing out.  Without a deadline the worker would report a
# healthy camera forever and record an empty video.
STALL_TIMEOUT = 3.0       # seconds without a frame before declaring it gone
FIRST_FRAME_TIMEOUT = 4.0  # bounds the one call that could otherwise never return

# libuvc shares one libusb context across all cameras, and opening or closing a
# device from several threads at once races inside it: the symptom is one camera
# failing with "Can't start isochronous stream" while the others wedge.  Grabbing
# frames in parallel is fine -- only setup and teardown need serialising.
_device_lock = threading.Lock()


@dataclass
class CameraStats:
    frames: int = 0             # frames delivered since the worker started
    dropped_stream: int = 0     # frames libuvc reported as corrupt/incomplete
    timeouts: int = 0
    reopens: int = 0
    fps: float = 0.0            # measured, updated once a second
    last_frame_at: float = 0.0  # unix time
    connected: bool = False
    error: str = ""


@dataclass
class Frame:
    """A single JPEG plus everything needed to place it in time."""
    jpeg: bytes
    unix_time: float        # host clock when the frame was handed to us
    monotonic: float        # host monotonic clock, immune to clock steps
    device_time: float      # the camera's own clock (starts at 0 per stream)
    uvc_index: int          # device frame counter; gaps mean the device dropped
    width: int
    height: int


class CameraWorker(threading.Thread):
    """Owns one camera: opens it, keeps it streaming, publishes every frame."""

    def __init__(self, cam_id: str, uid: str, role: str, side: str,
                 usb_path: str, product: str,
                 width: int, height: int, fps: int, bandwidth_factor: float):
        super().__init__(name=f"cam-{cam_id}", daemon=True)
        self.cam_id = cam_id
        self.uid = uid
        self.role = role
        self.side = side
        self.usb_path = usb_path
        self.product = product
        self.width, self.height, self.fps = width, height, fps
        self.bandwidth_factor = bandwidth_factor

        self.stats = CameraStats()
        self._stop = threading.Event()
        self._cap = None
        self._last_frame_mono = 0.0

        # Latest frame for preview.  Readers wait on the condition for a new
        # sequence number rather than polling, so an idle preview costs nothing.
        self._latest: Frame | None = None
        self._seq = 0
        self._new_frame = threading.Condition()

        # Set while a recording is running; the sink is written from this thread.
        self._sink = None
        self._sink_lock = threading.Lock()

    # -- preview ---------------------------------------------------------

    def latest_frame(self, after_seq: int, timeout: float = 5.0):
        """Block until a frame newer than `after_seq` exists. -> (seq, Frame)."""
        with self._new_frame:
            if self._seq <= after_seq:
                self._new_frame.wait(timeout)
            if self._latest is None or self._seq <= after_seq:
                return self._seq, None
            return self._seq, self._latest

    # -- recording -------------------------------------------------------

    def attach_sink(self, sink) -> None:
        with self._sink_lock:
            self._sink = sink

    def detach_sink(self):
        with self._sink_lock:
            sink, self._sink = self._sink, None
        return sink

    # -- lifecycle -------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def _resolve_uid(self) -> str:
        """Find the camera's current libuvc uid from its physical port.

        The uid is bus:address and changes every time a camera is replugged,
        while the port it hangs off does not.  Re-resolving here is what lets a
        camera that was unplugged and put back come alive again.
        """
        from .usbmap import discover_sysfs

        for cam in discover_sysfs():
            if cam.usb_path == self.usb_path:
                return cam.uid
        raise RuntimeError(f"{self.cam_id}: not attached at {self.usb_path}")

    def _open(self) -> None:
        wanted = (self.width, self.height, self.fps)
        uid = self._resolve_uid()
        if uid != self.uid:
            logger.info("%s: moved from uid %s to %s", self.cam_id, self.uid, uid)
            self.uid = uid
        with _device_lock:
            cap = uvc.Capture(self.uid)
            try:
                cap.bandwidth_factor = self.bandwidth_factor
                try:
                    mode = next(m for m in cap.available_modes
                                if (m[0], m[1], m[2]) == wanted and m[4] == "MJPG")
                except StopIteration:
                    raise RuntimeError(
                        f"{self.cam_id}: no MJPEG mode "
                        f"{wanted[0]}x{wanted[1]}@{wanted[2]}")
                cap.frame_mode = mode
                # Start streaming inside the lock: this is where libuvc reserves
                # USB bandwidth, which must not overlap another camera doing the same.
                #
                # The timeout is essential.  get_frame_robust() waits forever,
                # and a camera that never delivers a first frame -- a bandwidth
                # refusal, or a device left in a bad state by a replug -- then
                # blocks inside the library with the GIL held, stopping every
                # thread in the process, HTTP included.  That was measured:
                # three cameras streaming and the fourth wedged the recorder
                # until systemd had to kill it.
                try:
                    cap.get_frame(timeout=FIRST_FRAME_TIMEOUT)
                except TimeoutError:
                    raise RuntimeError(
                        f"{self.cam_id}: no first frame within "
                        f"{FIRST_FRAME_TIMEOUT:.0f}s") from None
            except Exception:
                cap.close()
                raise
        self._cap = cap
        self._last_frame_mono = time.monotonic()
        self.stats.connected = True
        self.stats.error = ""
        logger.info("%s: streaming %dx%d@%d (uid %s, usb %s)",
                    self.cam_id, self.width, self.height, self.fps,
                    self.uid, self.usb_path)

    def _close(self) -> None:
        if self._cap is not None:
            try:
                with _device_lock:
                    self._cap.close()
            except Exception:
                logger.exception("%s: error closing capture", self.cam_id)
            self._cap = None
        self.stats.connected = False

    def run(self) -> None:
        window_start = time.monotonic()
        window_frames = 0

        while not self._stop.is_set():
            if self._cap is None:
                try:
                    self._open()
                except Exception as exc:
                    self.stats.error = str(exc)
                    self.stats.connected = False
                    logger.error("%s: open failed: %s", self.cam_id, exc)
                    if self._stop.wait(REOPEN_DELAY):
                        break
                    self.stats.reopens += 1
                    continue

            try:
                frame = self._cap.get_frame(timeout=GRAB_TIMEOUT)
            except TimeoutError:
                self.stats.timeouts += 1
                silent = time.monotonic() - self._last_frame_mono
                if silent > STALL_TIMEOUT and self.stats.connected:
                    # Report it, but do not try to reopen: see CameraSupervisor
                    # for why opening a camera mid-session is not safe here.
                    self.stats.connected = False
                    self.stats.error = f"no frames for {silent:.0f}s"
                    logger.warning("%s: %s", self.cam_id, self.stats.error)
                continue
            except uvc.StreamError as exc:
                # Corrupt or truncated frame -- pyuvc already rejected it.
                self.stats.dropped_stream += 1
                if self.stats.dropped_stream % 100 == 1:
                    logger.warning("%s: stream error: %s", self.cam_id, exc)
                continue
            except Exception as exc:
                self.stats.error = str(exc)
                logger.error("%s: capture failed, reopening: %s", self.cam_id, exc)
                self._close()
                continue

            now = time.time()
            record = Frame(
                jpeg=bytes(frame.jpeg_buffer),
                unix_time=now,
                monotonic=time.monotonic(),
                device_time=float(frame.timestamp),
                uvc_index=int(frame.index),
                width=self.width,
                height=self.height,
            )

            self.stats.frames += 1
            self.stats.last_frame_at = now
            self._last_frame_mono = record.monotonic
            window_frames += 1
            elapsed = record.monotonic - window_start
            if elapsed >= 1.0:
                self.stats.fps = window_frames / elapsed
                window_start, window_frames = record.monotonic, 0

            with self._sink_lock:
                sink = self._sink
            if sink is not None:
                sink.write(record)

            with self._new_frame:
                self._latest = record
                self._seq += 1
                self._new_frame.notify_all()

        self._close()
        logger.info("%s: stopped after %d frames", self.cam_id, self.stats.frames)


def build_workers(cfg) -> list[CameraWorker]:
    """Discover attached Pupil cameras and create a worker for each."""
    from .usbmap import discover_sysfs, group_by_port

    # sysfs, not libuvc: see discover_sysfs for why enumerating through the
    # library is not safe once cameras are streaming.
    cams = discover_sysfs()
    if not cams:
        raise RuntimeError(
            "No Pupil Core cameras found. Are they plugged in, and has "
            "uvcvideo been detached (see setup/install.sh)?"
        )

    workers = []
    for index, (root_port, port_cams) in enumerate(sorted(group_by_port(cams).items())):
        side = cfg.side_for(root_port, index)
        # A headset may carry more than one camera of a role -- a Pupil Core can
        # have two eye cameras.  The first of a role keeps the plain name so
        # existing recordings stay comparable; further ones are numbered in USB
        # port order, which is stable across replugs.
        seen: dict[str, int] = {}
        for cam in sorted(port_cams, key=lambda c: c.usb_path):
            seen[cam.role] = seen.get(cam.role, 0) + 1
            suffix = "" if seen[cam.role] == 1 else str(seen[cam.role])
            width, height, fps = cfg.mode_for(cam.role)
            workers.append(CameraWorker(
                cam_id=f"{side}_{cam.role}{suffix}",
                uid=cam.uid, role=cam.role, side=side,
                usb_path=cam.usb_path, product=cam.product,
                width=width, height=height, fps=fps,
                bandwidth_factor=cfg.bandwidth_factor,
            ))
    return sorted(workers, key=lambda w: (w.side, w.role, w.usb_path))


class CameraSupervisor(threading.Thread):
    """Reports cameras that appear after start-up, without touching them.

    It cannot simply adopt one.  pyuvc enumerates devices inside both
    uvc.device_list() and the Capture constructor, and that call blocks
    indefinitely while other cameras are streaming -- with the GIL held, so the
    whole process stops answering.  It was measured doing exactly that: a
    camera plugged back in was detected, the open never returned, and systemd
    had to SIGKILL the recorder.

    So discovery here is pure sysfs, and a newly attached camera is surfaced to
    the operator to pick up with a restart, which is quick and always works.
    """

    def __init__(self, cfg, on_detect, is_recording, interval: float = 5.0):
        super().__init__(name="camera-supervisor", daemon=True)
        self.cfg = cfg
        self.on_detect = on_detect
        self.is_recording = is_recording
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from .usbmap import discover_sysfs

        while not self._stop.wait(self.interval):
            try:
                attached = discover_sysfs()
            except Exception:
                logger.exception("camera rescan failed")
                continue
            try:
                self.on_detect(attached)
            except Exception:
                logger.exception("could not report attached cameras")
