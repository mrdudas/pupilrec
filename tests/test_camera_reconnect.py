"""A camera that is unplugged mid-session rejoins it when it is plugged back in.

The capture thread is run for real against a fake bus: a dict from USB port to
the uid of whatever is plugged in there, which both the fake sysfs and the fake
libuvc read.  Unplugging is taking a port out of the dict; plugging back in is
putting it back under a new uid, which is what the kernel does on a replug.
"""

import threading
import time
import unittest
from unittest import mock

from pupilrec import capture, usbmap
from pupilrec.capture import CameraWorker, build_workers
from pupilrec.config import Config

PORT = "3-1.1"
MODE = (1280, 720, 60)


class FakeBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.ports = {PORT: "3:10"}
        self.captures = []          # every FakeCapture ever opened
        self.next_address = 11

    def discover(self):
        with self.lock:
            return [usbmap.UsbCam(uid=uid, product="Pupil Cam1 ID2", usb_path=port,
                                  root_port=port.split(".")[0], role="world")
                    for port, uid in self.ports.items()]

    def present(self, uid):
        with self.lock:
            return uid in self.ports.values()

    def unplug(self):
        with self.lock:
            del self.ports[PORT]

    def plug(self):
        with self.lock:
            uid = f"3:{self.next_address}"
            self.next_address += 1
            self.ports[PORT] = uid
            self.plugged_at = time.monotonic()
            return uid


class FakeFrame:
    def __init__(self, index):
        self.jpeg_buffer = b"\xff\xd8jpeg\xff\xd9"
        self.timestamp = index / 60
        self.index = index


class FakeCapture:
    bus: FakeBus = None
    # Set to make a camera that is on the bus deliver nothing (a stalled stream).
    silent = False

    def __init__(self, uid):
        if not self.bus.present(uid):
            raise RuntimeError(f"Device with uid: `{uid}` not found")
        self.uid = uid
        self.opened_at = time.monotonic()
        self.closed = False
        self.controls = []
        self.available_modes = [(*MODE, None, "MJPG")]
        self.frame_mode = None
        self.bandwidth_factor = None
        self.index = 0
        self.bus.captures.append(self)

    def get_frame(self, timeout=0):
        if self.closed:
            raise AssertionError("frame asked of a closed handle")
        time.sleep(0.002)
        if not self.bus.present(self.uid) or FakeCapture.silent:
            time.sleep(timeout)
            raise TimeoutError
        self.index += 1
        return FakeFrame(self.index)

    def close(self):
        if not self.bus.present(self.uid):
            # What the real one does is block with the GIL held.
            raise AssertionError("close() on a handle whose device is gone")
        self.closed = True


class Sink:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ReconnectTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        FakeCapture.bus = self.bus
        FakeCapture.silent = False
        patches = [
            mock.patch.object(usbmap, "discover_sysfs", self.bus.discover),
            mock.patch.object(capture.uvc, "Capture", FakeCapture),
            mock.patch.object(capture, "GRAB_TIMEOUT", 0.02),
            mock.patch.object(capture, "STALL_TIMEOUT", 0.15),
            mock.patch.object(capture, "PRESENCE_POLL", 0.02),
            mock.patch.object(capture, "SETTLE_TIME", 0.2),
            mock.patch.object(capture, "REOPEN_DELAY", 0.05),
            mock.patch.object(capture, "MAX_REOPEN_DELAY", 0.2),
            mock.patch.object(capture.systemd, "ping"),
            mock.patch.object(capture.logger, "disabled", True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.worker = None

    def tearDown(self):
        if self.worker is not None:
            self.worker.stop(abandon=True)
            self.worker.join(timeout=5)
        capture._abandoned_handles.clear()

    def start(self, uid="3:10"):
        self.worker = CameraWorker(
            cam_id="left_world", uid=uid, role="world", side="left",
            usb_path=PORT, product="Pupil Cam1 ID2",
            width=MODE[0], height=MODE[1], fps=MODE[2], bandwidth_factor=1.0)
        self.sink = Sink()
        self.worker.attach_sink(self.sink)
        self.worker.start()
        self.assertTrue(wait_for(lambda: len(self.sink.frames) > 5),
                        "the camera streams at all")

    def test_a_replugged_camera_streams_into_the_same_recording(self):
        self.start()
        self.bus.unplug()
        self.assertTrue(wait_for(lambda: not self.worker.stats.connected))
        self.assertTrue(wait_for(lambda: "unplugged" in self.worker.stats.error))
        before = len(self.sink.frames)

        new_uid = self.bus.plug()
        self.assertTrue(wait_for(lambda: len(self.sink.frames) > before + 5),
                        "frames resume once the camera is back")
        self.assertTrue(self.worker.stats.connected)
        self.assertEqual(self.worker.uid, new_uid)
        self.assertEqual(len(self.bus.captures), 2)
        self.assertEqual(self.bus.captures[-1].uid, new_uid)

    def test_a_vanished_handle_is_kept_not_closed(self):
        """pyuvc's __dealloc__ closes, so dropping the handle would block."""
        self.start()
        first = self.bus.captures[0]
        self.bus.unplug()
        self.assertTrue(wait_for(lambda: first in capture._abandoned_handles))
        self.assertFalse(first.closed)

    def test_a_returning_camera_is_left_to_settle_first(self):
        self.start()
        self.bus.unplug()
        self.assertTrue(wait_for(lambda: "unplugged" in self.worker.stats.error))
        self.bus.plug()
        self.assertTrue(wait_for(lambda: len(self.bus.captures) == 2))
        waited = self.bus.captures[-1].opened_at - self.bus.plugged_at
        self.assertGreaterEqual(waited, capture.SETTLE_TIME * 0.9)

    def test_it_survives_being_unplugged_again_and_again(self):
        self.start()
        for _ in range(3):
            self.bus.unplug()
            self.assertTrue(wait_for(lambda: "unplugged" in self.worker.stats.error))
            before = len(self.sink.frames)
            self.bus.plug()
            self.assertTrue(wait_for(lambda: len(self.sink.frames) > before + 5))
        self.assertEqual(len(self.bus.captures), 4)

    def test_a_stall_on_the_bus_is_closed_and_reopened(self):
        self.start()
        first = self.bus.captures[0]
        FakeCapture.silent = True
        self.assertTrue(wait_for(lambda: first.closed),
                        "a camera still on the bus is closed the normal way")
        FakeCapture.silent = False
        before = len(self.sink.frames)
        self.assertTrue(wait_for(lambda: len(self.sink.frames) > before + 5))
        self.assertEqual(self.worker.uid, "3:10", "same device, same uid")

    def test_a_camera_absent_at_start_joins_when_plugged_in(self):
        """A resumed recording's camera that was still unplugged at start-up."""
        self.bus.unplug()
        self.worker = CameraWorker(
            cam_id="left_world", uid="", role="world", side="left",
            usb_path=PORT, product="Pupil Cam1 ID2",
            width=MODE[0], height=MODE[1], fps=MODE[2], bandwidth_factor=1.0)
        self.sink = Sink()
        self.worker.attach_sink(self.sink)
        self.worker.start()
        self.assertTrue(self.worker.first_open_done.wait(2),
                        "an absent camera must not hold up the others")
        self.assertTrue(wait_for(lambda: "unplugged" in self.worker.stats.error))
        self.bus.plug()
        self.assertTrue(wait_for(lambda: len(self.sink.frames) > 5))

    def test_control_requests_are_answered_while_it_is_away(self):
        self.start()
        self.bus.unplug()
        self.assertTrue(wait_for(lambda: "unplugged" in self.worker.stats.error))
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "not open"):
            self.worker.list_controls()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_reopen_is_marked_as_one_for_the_guard(self):
        phases = []

        class Guard:
            def is_quarantined(self, cam_id):
                return False

            def attempting(self, cam_id, usb_path, phase="start"):
                phases.append(phase)
                import contextlib
                return contextlib.nullcontext()

        self.worker = CameraWorker(
            cam_id="left_world", uid="3:10", role="world", side="left",
            usb_path=PORT, product="Pupil Cam1 ID2",
            width=MODE[0], height=MODE[1], fps=MODE[2], bandwidth_factor=1.0,
            guard=Guard())
        self.sink = Sink()
        self.worker.attach_sink(self.sink)
        self.worker.start()
        self.assertTrue(wait_for(lambda: len(self.sink.frames) > 5))
        self.bus.unplug()
        self.assertTrue(wait_for(lambda: "unplugged" in self.worker.stats.error))
        self.bus.plug()
        self.assertTrue(wait_for(lambda: len(phases) == 2))
        self.assertEqual(phases, ["start", "reopen"])


class ExpectedCamerasTest(unittest.TestCase):
    """Resuming a recording keeps a worker for a camera that is unplugged."""

    def test_an_absent_camera_of_the_recording_still_gets_a_worker(self):
        attached = [usbmap.UsbCam(uid="3:10", product="Pupil Cam1 ID2",
                                  usb_path="3-1.1", root_port="3-1", role="world")]
        expected = [
            {"camera": "left_world", "usb_path": "3-1.1", "role": "world",
             "side": "left", "product": "Pupil Cam1 ID2"},
            {"camera": "left_eye", "usb_path": "3-1.3", "role": "eye",
             "side": "left", "product": "Pupil Cam2 ID0"},
        ]
        cfg = Config()
        cfg.headsets = {"3-1": "left"}
        with mock.patch.object(usbmap, "discover_sysfs", return_value=attached):
            workers = {w.cam_id: w for w in build_workers(cfg, expected=expected)}
        self.assertEqual(sorted(workers), ["left_eye", "left_world"])
        self.assertEqual(workers["left_eye"].uid, "")
        self.assertEqual(workers["left_eye"].usb_path, "3-1.3")
        self.assertEqual(workers["left_world"].uid, "3:10")

    def test_nothing_attached_is_not_an_error_when_resuming(self):
        expected = [{"camera": "left_world", "usb_path": "3-1.1", "role": "world",
                     "side": "left", "product": "Pupil Cam1 ID2"}]
        with mock.patch.object(usbmap, "discover_sysfs", return_value=[]):
            workers = build_workers(Config(), expected=expected)
        self.assertEqual([w.cam_id for w in workers], ["left_world"])

    def test_nothing_attached_and_nothing_expected_still_is(self):
        with mock.patch.object(usbmap, "discover_sysfs", return_value=[]):
            with self.assertRaises(RuntimeError):
                build_workers(Config())


if __name__ == "__main__":
    unittest.main()
