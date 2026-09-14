"""One capture thread per camera, fanning JPEG frames out to preview and recording.

Frames are never decoded.  The cameras emit MJPEG, the browser eats MJPEG and
the recorder muxes MJPEG, so a frame is copied out of the driver buffer once and
then only ever passed around as bytes.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field

import uvc

from . import systemd
from .quarantine import NoGuard

logger = logging.getLogger(__name__)

# libuvc hands back a view into a buffer it reuses on the next call, so a frame
# has to be copied before asking for the following one.  This is the only copy.
GRAB_TIMEOUT = 1.0        # seconds; bounds how long stop() waits on a dead camera
REOPEN_DELAY = 2.0        # seconds before the first reconnect attempt
# A camera that cannot be opened at all -- no bandwidth left on the bus, a
# board in a bad state -- would otherwise retry twice a second forever, and
# each attempt takes the device lock and the GIL for up to FIRST_FRAME_TIMEOUT.
# That is a stall the healthy cameras pay for, so failures back off.
MAX_REOPEN_DELAY = 60.0
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

# A control request is served by the capture thread between two frames, but it
# also queues behind _device_lock, which another camera can hold for a whole
# open.  The budget covers both; past it the camera really is wedged.
CONTROL_TIMEOUT = 12.0

# UVC GET_INFO bits: what the camera says it will let us do with a control.
# (Bit 0, "supports GET", is true of everything these cameras report.)
INFO_SET = 1 << 1
INFO_AUTO_DISABLED = 1 << 2      # set right now because an "auto" mode owns it


def describe_control(ctrl) -> dict:
    """One pyuvc Control as JSON the UI can build an input out of.

    d_type carries the shape: a dict is a menu of named choices, bool is a
    switch, anything else is a number with a range.
    """
    d_type = ctrl.d_type
    if isinstance(d_type, dict):
        kind = "menu"
        # For Auto Exposure Mode the camera answers GET_RES with a bitmap of the
        # modes it actually implements rather than a step -- 9 means manual and
        # aperture priority and nothing else.  Offering the other two would only
        # produce a write the camera rejects.
        supported = (ctrl.step
                     if ctrl.display_name.strip() == "Auto Exposure Mode" else None)
        options = [{"label": label, "value": value}
                   for label, value in d_type.items()
                   if not supported or value & supported]
    elif d_type is bool:
        kind, options = "bool", []
    else:
        kind, options = "int", []
    mask = ctrl.info_bit_mask or 0
    return {
        # Two of the names pyuvc reports carry a trailing space ("Absolute
        # Iris ").  Trim it here so it never reaches config.json or the UI;
        # _control matches trimmed names, so nothing downstream needs to know.
        "name": ctrl.display_name.strip(),
        "kind": kind,
        "options": options,
        "value": ctrl.value,
        "min": ctrl.min_val,
        "max": ctrl.max_val,
        "step": ctrl.step,
        "default": ctrl.def_val,
        "unit": ctrl.unit,
        "doc": ctrl.doc or "",
        "writable": bool(mask & INFO_SET),
        # Reported as read-only for now: an automatic mode is driving it.
        "auto_disabled": bool(mask & INFO_AUTO_DISABLED),
    }


# What Pupil's own software writes into these cameras before it touches
# anything else (pupil_src/shared_modules/video_capture/uvc_backend.py,
# _configure_capture).  These are Pupil Core cameras being used for what Pupil
# Core cameras are for, and leaving them on their factory values is not a
# neutral choice: measured here, the eye cameras' dark range is six times
# darker at the factory Gamma of 144 than at the 200 Pupil sets, and the
# chroma channels of a monochrome infrared sensor cost JPEG size to carry
# nothing.  Auto Exposure Priority is the one that matters even where the
# picture looks the same: at 1 the camera is allowed to drop the frame rate to
# reach an exposure, which is precisely what a recorder running at a fixed 120
# fps must not permit.
#
# Whatever the operator has stored is written after this and wins.
_EYE_BASELINE_CAM2 = {
    "Auto Exposure Mode": 1,        # manual
    "Auto Exposure Priority": 0,
    "Saturation": 0,
    "Gamma": 200,
    "Auto Focus": 0,
}
_EYE_BASELINE_CAM1 = {
    "Auto Exposure Mode": 1,
    "Auto Exposure Priority": 0,
    "Saturation": 0,
    "Absolute Exposure Time": 63,
    "Backlight Compensation": 2,
    "Gamma": 100,
    "Auto Focus": 0,
}
# Pupil sets Auto Exposure Priority to 1 on the world camera, trading frame rate
# for exposure in changing light.  Not taken: this is a recorder, the frame rate
# is the thing being promised, and a world video that quietly drops to 30 fps in
# a dark room would be discovered in the timestamps afterwards.
_WORLD_BASELINE = {
    "Auto Focus": 0,
}
CONTROL_BASELINE = {
    "Pupil Cam2 ID0": _EYE_BASELINE_CAM2, "Pupil Cam2 ID1": _EYE_BASELINE_CAM2,
    "Pupil Cam3 ID0": _EYE_BASELINE_CAM2, "Pupil Cam3 ID1": _EYE_BASELINE_CAM2,
    "Pupil Cam1 ID0": _EYE_BASELINE_CAM1, "Pupil Cam1 ID1": _EYE_BASELINE_CAM1,
    "Pupil Cam1 ID2": _WORLD_BASELINE, "Pupil Cam3 ID2": _WORLD_BASELINE,
}


def baseline_for(product: str) -> dict[str, int]:
    """The working point Pupil sets for this camera, or nothing for a stranger."""
    return dict(CONTROL_BASELINE.get(product, {}))


def _apply_order(items):
    """Automatic-mode switches first, then the values they would override.

    Writing "Absolute Exposure Time" while auto exposure is on is rejected by
    the camera, so the switch has to be settled before the value is restored.
    """
    return sorted(items, key=lambda kv: 0 if "Auto" in kv[0] else 1)


@dataclass
class _ControlRequest:
    """A control read or write, queued for the capture thread to carry out."""
    action: str                     # "list" | "set" | "reset"
    name: str = ""
    value: int = 0
    refresh_all: bool = False       # re-read every control from the camera
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: str = ""


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
    # Mean brightness of the middle of the picture, 0-255, or -1 before the
    # first sample.  Pupil's own auto exposure aims for 90 to 150 on this
    # scale, which is what makes it worth showing an operator.
    brightness: float = -1.0


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
                 width: int, height: int, fps: int, bandwidth_factor: float,
                 controls: dict[str, int] | None = None, guard=None):
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
        # Names this camera on disk while it is being opened, so that a freeze
        # inside libuvc is attributable after the kill.  See quarantine.py.
        self.guard = guard if guard is not None else NoGuard()
        self._open_failures = 0
        # Cameras open one at a time, in an order set by build_workers: this
        # worker waits for `open_after` and then lets the next one go.
        self.open_after: threading.Event | None = None
        self.first_open_done = threading.Event()
        self._abandon = False       # set by stop() when the process is exiting

        # Latest frame for preview.  Readers wait on the condition for a new
        # sequence number rather than polling, so an idle preview costs nothing.
        self._latest: Frame | None = None
        self._seq = 0
        self._new_frame = threading.Condition()

        # Set while a recording is running; the sink is written from this thread.
        self._sink = None
        self._sink_lock = threading.Lock()

        # Image controls the operator chose, written into the camera on every
        # open because the camera forgets them whenever it loses power.
        self.wanted_controls: dict[str, int] = dict(controls or {})
        # Control transfers are done by this thread and no other.  Touching the
        # capture handle from an HTTP thread would race the reopen path, which
        # closes and replaces it, and libuvc gives no way to detect that.
        self._requests: queue.Queue = queue.Queue()

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

    # -- image controls --------------------------------------------------

    def list_controls(self, refresh_all: bool = False) -> list[dict]:
        """Every control this camera exposes, with its current value.

        `refresh_all` re-reads each one from the camera instead of reporting
        what was cached.  It is the expensive path -- see _read_controls -- so
        it happens only when someone explicitly asks for it.
        """
        return self._request(_ControlRequest("list", refresh_all=refresh_all))

    def set_control(self, name: str, value: int) -> list[dict]:
        """Write one control, remember it, and report every control again.

        The whole list comes back because one control moves others: turning
        auto exposure on makes the exposure time read-only, and the camera
        clamps a value it does not like rather than refusing it.
        """
        return self._request(_ControlRequest("set", name=name, value=int(value)))

    def reset_controls(self) -> list[dict]:
        """Put every control back to the default the camera reports."""
        return self._request(_ControlRequest("reset"))

    def _request(self, req: _ControlRequest):
        if not self.is_alive():
            raise RuntimeError(f"{self.cam_id}: capture thread is not running")
        self._requests.put(req)
        if not req.done.wait(CONTROL_TIMEOUT):
            raise TimeoutError(
                f"{self.cam_id}: no answer within {CONTROL_TIMEOUT:.0f}s")
        if req.error:
            raise RuntimeError(req.error)
        return req.result

    def _serve_requests(self) -> None:
        """Run queued control transfers.  Called from this thread only."""
        while True:
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                if self._cap is None:
                    raise RuntimeError(
                        f"{self.cam_id}: camera is not open "
                        f"({self.stats.error or 'no signal'})")
                # Under the same lock as opening and closing a camera: a
                # control transfer must not overlap one, for the reason spelt
                # out in _open.  Listing needs no transfer at all, so it costs
                # nothing to hold the lock for it.
                with _device_lock:
                    if req.action == "set":
                        self._write_control(req.name, req.value)
                    elif req.action == "reset":
                        self._reset_controls()
                        self.wanted_controls.clear()
                    # Only the control just written is read back, unless a
                    # full re-read was asked for.
                    req.result = self._read_controls(
                        req.name if req.action == "set" else None,
                        refresh_all=req.refresh_all)
                if req.action == "set":
                    # Remember what the camera settled on rather than what was
                    # asked for: it clamps a value out of range, and the next
                    # open has to write back the one that will actually stick.
                    applied = next((c["value"] for c in req.result
                                    if c["name"] == req.name), req.value)
                    self.wanted_controls[req.name] = applied
            except Exception as exc:
                req.error = str(exc)
            finally:
                req.done.set()

    def _fail_pending(self, reason: str) -> None:
        """Release anyone waiting on a control once this thread is going away."""
        while True:
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                return
            req.error = f"{self.cam_id}: {reason}"
            req.done.set()

    def _read_controls(self, refresh: str | None = None,
                       refresh_all: bool = False) -> list[dict]:
        """Snapshot every control, re-reading at most the one just written.

        Re-reading all of them is not affordable by default.  Each one is a USB
        control transfer that pyuvc performs with the GIL held, and an eye
        camera's twenty-odd controls measured 1.2 s end to end -- during which
        every capture thread in the process stops and each camera loses a
        second of frames.  So the values pyuvc read when the camera was opened
        are kept and updated by our own writes, and only the control a write
        just touched is read back, to learn what the camera clamped it to.

        `refresh_all` pays that cost deliberately.  It is the only way to see a
        value an automatic mode has moved on its own, or to correct one the
        camera reported wrongly at open, so the operator can ask for it.
        """
        if refresh_all:
            for ctrl in self._cap.controls:
                self._refresh_one(ctrl)
        elif refresh:
            self._refresh_one(self._control(refresh))
        return [describe_control(c) for c in self._cap.controls]

    def _refresh_one(self, ctrl) -> None:
        try:
            ctrl.refresh()
        except Exception as exc:
            logger.debug("%s: %s could not be refreshed: %s",
                         self.cam_id, ctrl.display_name.strip(), exc)

    def _control(self, name: str):
        wanted = name.strip()
        for ctrl in self._cap.controls:
            if ctrl.display_name.strip() == wanted:
                return ctrl
        raise RuntimeError(f"{self.cam_id}: no control named {name!r}")

    def _write_control(self, name: str, value: int) -> None:
        ctrl = self._control(name)
        if not (ctrl.info_bit_mask or 0) & INFO_SET:
            raise RuntimeError(f"{name} cannot be set on this camera")
        ctrl.value = value

    def _reset_controls(self) -> None:
        defaults = [(c.display_name.strip(), c.def_val) for c in self._cap.controls
                    if c.def_val is not None and (c.info_bit_mask or 0) & INFO_SET]
        for name, value in _apply_order(defaults):
            try:
                self._control(name).value = value
            except Exception as exc:
                logger.warning("%s: could not reset %s: %s", self.cam_id, name, exc)

    def _restore_controls(self, cap) -> None:
        """Write the working point into a freshly opened camera.

        Two layers.  First what Pupil sets for this model of camera, because a
        camera that has just been powered on is at its factory values and those
        are not what these sensors are meant to run at.  Then whatever the
        operator stored here, which overrides it -- a value someone chose while
        looking at the picture beats a default, always.

        A control the camera does not have, or refuses, is logged and skipped:
        losing one setting must not cost the whole stream.
        """
        wanted = baseline_for(self.product) | self.wanted_controls
        if not wanted:
            return
        by_name = {c.display_name.strip(): c for c in cap.controls}
        for name, value in _apply_order(wanted.items()):
            ctrl = by_name.get(name)
            if ctrl is None:
                logger.warning("%s: no control named %r on this camera",
                               self.cam_id, name)
                continue
            try:
                ctrl.value = int(value)
            except Exception as exc:
                logger.warning("%s: could not restore %s=%s: %s",
                               self.cam_id, name, value, exc)

    # -- lifecycle -------------------------------------------------------

    def stop(self, abandon: bool = False) -> None:
        """Ask this camera to stop.

        `abandon` leaves the capture handle open on the way out, for a process
        that is about to exit: closing it is the one call on the shutdown path
        with no bound on how long it takes.  See run.py.
        """
        self._abandon = abandon
        self._stop.set()
        # A camera stopped before it ever opened must not strand the ones
        # queued behind it.
        self.first_open_done.set()

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
        with _device_lock, self.guard.attempting(self.cam_id, self.usb_path):
            # Everything from here to the first frame can freeze the process
            # rather than fail, which is why the note is written first.
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
                # The camera came up with its factory values; put the
                # operator's back before releasing the lock.  A control
                # transfer takes ~100 ms on an eye camera and pyuvc holds the
                # GIL for all of it, which is long enough to wedge another
                # camera that is inside uvc.Capture() at the time: measured,
                # one stored setting left a camera stuck mid-open and the whole
                # recorder unresponsive until it was killed.
                self._restore_controls(cap)
            except Exception:
                cap.close()
                raise
        self._cap = cap
        self._last_frame_mono = time.monotonic()
        self.stats.connected = True
        self.stats.error = ""
        self._open_failures = 0
        logger.info("%s: streaming %dx%d@%d (uid %s, usb %s)",
                    self.cam_id, self.width, self.height, self.fps,
                    self.uid, self.usb_path)
        # Opening cameras is serialised and each one holds the GIL throughout,
        # so with enough of them the watchdog thread never gets to run before
        # systemd's patience runs out.  One camera opened is progress worth
        # reporting in its own right.
        systemd.ping()

    def _device_vanished(self) -> bool:
        """True when the device this handle refers to is no longer on the bus.

        A camera that was unplugged, or whose hub re-enumerated it, keeps its
        USB port but is given a new address, so a uid that no longer matches
        means the handle points at something that does not exist any more.
        Reading this from sysfs touches no libuvc state and cannot block.
        """
        try:
            return self._resolve_uid() != self.uid
        except RuntimeError:
            return True                 # nothing at that port at all
        except Exception:
            logger.exception("%s: could not tell whether the device is still there",
                             self.cam_id)
            return False                # unsure: close it the normal way

    def _close(self) -> None:
        cap, self._cap = self._cap, None
        self.stats.connected = False
        if cap is None:
            return
        if self._device_vanished():
            # libuvc blocks inside close() on a handle whose device is gone,
            # and it does so with the GIL held, which stops every thread in the
            # process, HTTP included.  Measured: a headset whose hub
            # re-enumerated left the recorder frozen and deaf to SIGTERM until
            # systemd's 90 s stop timeout fired -- a six second USB dropout
            # turned into a ninety second outage.  So the handle is abandoned
            # instead.  It costs a file descriptor until the process exits, and
            # the port is opened from scratch next time regardless.
            logger.warning("%s: device at %s is gone (was uid %s); abandoning "
                           "the handle rather than blocking on close",
                           self.cam_id, self.usb_path, self.uid)
            return
        try:
            with _device_lock:
                cap.close()
        except Exception:
            logger.exception("%s: error closing capture", self.cam_id)

    def run(self) -> None:
        window_start = time.monotonic()
        window_frames = 0

        while not self._stop.is_set():
            # Between frames is the only safe moment to talk to the camera's
            # control endpoint, so queued reads and writes are served here.
            self._serve_requests()

            if self._cap is None:
                if self.open_after is not None and not self.first_open_done.is_set():
                    self.open_after.wait()
                try:
                    self._open()
                except Exception as exc:
                    self.stats.error = str(exc)
                    self.stats.connected = False
                    self._open_failures += 1
                    delay = min(REOPEN_DELAY * 2 ** (self._open_failures - 1),
                                MAX_REOPEN_DELAY)
                    logger.error("%s: open failed: %s (retry in %.0fs)",
                                 self.cam_id, exc, delay)
                    self.first_open_done.set()    # never hold up the next camera
                    if self._stop.wait(delay):
                        break
                    self.stats.reopens += 1
                    continue
                self.first_open_done.set()

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

        self._finish()

    def _finish(self) -> None:
        """The last thing the capture thread does, once it is not running."""
        if self._abandon:
            # The process is on its way out and the kernel releases every USB
            # interface it held.  Doing it here instead means uvc_close, which
            # has been measured taking the whole of systemd's 90s stop timeout
            # with the GIL in its hand -- and a shutdown that slow is one that
            # ends in SIGKILL, which is worse than not closing at all.
            logger.info("%s: stopped after %d frames, leaving the handle to "
                        "the kernel", self.cam_id, self.stats.frames)
        else:
            self._close()
            logger.info("%s: stopped after %d frames", self.cam_id, self.stats.frames)
        self._fail_pending("camera stopped")


def _port_order(cam):
    """Sort key for a USB path, by number rather than by digit.

    "3-1.10" sorts before "3-1.3" as text, and the order is what decides which
    eye camera is `eye` and which is `eye2` -- so on a hub with ten or more
    ports, plain string sorting would quietly swap the two eyes of one headset
    between runs, with nothing in the recording to show it had happened.  No hub
    here has that many ports; the cost of not relying on that is three lines.
    """
    return [int(part) if part.isdigit() else part
            for part in re.split(r"[-.]", cam.usb_path)]


def build_workers(cfg, guard=None) -> list[CameraWorker]:
    """Discover attached Pupil cameras and create a worker for each.

    A camera the guard has condemned gets no worker at all: it froze the
    recorder the last time it was opened, and opening it again would do the
    same.  Everything else comes up as usual, which is the point -- one camera
    the bus cannot carry must cost that camera, not the recording.
    """
    from .usbmap import discover_sysfs, group_by_port

    # sysfs, not libuvc: see discover_sysfs for why enumerating through the
    # library is not safe once cameras are streaming.
    cams = discover_sysfs()
    if not cams:
        raise RuntimeError(
            "No Pupil Core cameras found. Are they plugged in, and has "
            "uvcvideo been detached (see setup/install.sh)?"
        )
    guard = guard if guard is not None else NoGuard()
    for gone in cfg.forget_absent_headsets({cam.root_port for cam in cams}):
        logger.info("forgetting the label for %s: nothing is plugged into it", gone)

    workers = []
    for index, (root_port, port_cams) in enumerate(sorted(group_by_port(cams).items())):
        side = cfg.side_for(root_port, index)
        # A headset may carry more than one camera of a role -- a Pupil Core can
        # have two eye cameras.  The first of a role keeps the plain name so
        # existing recordings stay comparable; further ones are numbered in USB
        # port order, which is stable across replugs.
        seen: dict[str, int] = {}
        for cam in sorted(port_cams, key=_port_order):
            seen[cam.role] = seen.get(cam.role, 0) + 1
            suffix = "" if seen[cam.role] == 1 else str(seen[cam.role])
            cam_id = f"{side}_{cam.role}{suffix}"
            if guard.is_quarantined(cam_id):
                logger.warning("%s at %s is left out: it froze the recorder "
                               "when it was last opened", cam_id, cam.usb_path)
                continue
            width, height, fps = cfg.mode_for(cam.role)
            workers.append(CameraWorker(
                cam_id=cam_id,
                uid=cam.uid, role=cam.role, side=side,
                usb_path=cam.usb_path, product=cam.product,
                width=width, height=height, fps=fps,
                bandwidth_factor=cfg.bandwidth_factor,
                controls=cfg.controls_for(cam_id),
                guard=guard,
            ))
    # Open the world cameras first, one camera at a time.  When the bus runs out
    # of bandwidth it is whichever camera is being opened that wedges, so this
    # order decides what is lost: an eye camera rather than a headset's main
    # view.  Seven cameras do not fit on one USB 2.0 bus and six do, and which
    # six should not be decided by whichever thread won the race.
    previous = None
    for worker in sorted(workers,
                         key=lambda w: (w.role != "world", w.side, w.usb_path)):
        worker.open_after = previous
        previous = worker.first_open_done

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
