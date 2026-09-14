"""Surviving a camera that freezes the recorder instead of failing.

The freeze itself cannot be reproduced in a test -- it is a C library holding
the GIL, which would take the test down with it -- so what is checked here is
the bookkeeping that makes the next start survive it: the note is on disk
before the open and gone after, a note left behind condemns exactly one camera,
and the condemnation expires when the cameras themselves change.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from pupilrec import usbmap
from pupilrec.capture import build_workers
from pupilrec.config import Config
from pupilrec.quarantine import NoGuard, OpenGuard, signature_of


def cam(uid, product, usb_path, role):
    return usbmap.UsbCam(uid=uid, product=product, usb_path=usb_path,
                         root_port=usb_path.split(".")[0], role=role)


def headset(port, eyes=1):
    out = [cam(f"3:{port}0", "Pupil Cam1 ID2", f"{port}.1", "world")]
    for i in range(eyes):
        out.append(cam(f"3:{port}{i + 1}", f"Pupil Cam2 ID{i}",
                       f"{port}.{i + 3}", "eye"))
    return out


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "cameras.json")
        self.killed = []          # see killed_while_opening

    def stored(self) -> dict:
        with open(self.path) as fh:
            return json.load(fh)

    def killed_while_opening(self, cam_id: str, usb_path: str,
                             signature: str = "sig") -> None:
        """A start that got as far as the open and was then killed.

        The context manager is entered and never exited, which is what SIGKILL
        does to the real thing and the only state that leaves the note behind.
        It has to be held onto, though: drop the last reference and CPython
        finalises the abandoned generator, which runs the very `finally` that a
        killed process never gets to run, and the test would prove nothing.
        """
        cm = OpenGuard(self.path, signature).attempting(cam_id, usb_path)
        cm.__enter__()
        self.killed.append(cm)

    def test_the_note_is_written_before_the_open_and_removed_after(self):
        guard = OpenGuard(self.path, "sig")
        with guard.attempting("left_world", "3-1.1"):
            self.assertEqual(self.stored()["in_flight"]["cam_id"], "left_world")
        self.assertIsNone(self.stored()["in_flight"])

    def test_a_note_left_behind_condemns_that_camera(self):
        """The process died mid-open: the next start must leave it out."""
        self.killed_while_opening("unit3_world", "3-2.1")
        started = OpenGuard(self.path, "sig")
        self.assertTrue(started.is_quarantined("unit3_world"))
        self.assertFalse(started.is_quarantined("left_world"))

    def test_an_open_that_finished_condemns_nobody(self):
        """Only a start that died mid-open counts; a slow one is still fine."""
        guard = OpenGuard(self.path, "sig")
        with guard.attempting("left_world", "3-1.1"):
            pass
        self.assertEqual(OpenGuard(self.path, "sig").quarantined, {})

    def test_only_the_camera_that_wedged_is_condemned(self):
        guard = OpenGuard(self.path, "sig")
        with guard.attempting("left_world", "3-1.1"):
            pass                                    # this one opened fine
        self.killed_while_opening("unit3_eye2", "3-2.4")        # this one did not
        after = OpenGuard(self.path, "sig")
        self.assertEqual([c["id"] for c in after.report()], ["unit3_eye2"])

    def test_each_start_can_condemn_one_more(self):
        """Two cameras too many for the bus: two starts, then it comes up."""
        self.killed_while_opening("a_world", "3-2.1")
        self.killed_while_opening("b_eye", "3-2.3")
        self.assertEqual(sorted(OpenGuard(self.path, "sig").quarantined),
                         ["a_world", "b_eye"])

    def test_changing_the_cameras_retries_everything(self):
        self.killed_while_opening("unit3_world", "3-2.1", signature="sig-old")
        moved = OpenGuard(self.path, "sig-new")     # headset went to another port
        self.assertEqual(moved.quarantined, {})
        self.assertFalse(moved.is_quarantined("unit3_world"))

    def test_clearing_releases_every_camera(self):
        self.killed_while_opening("unit3_world", "3-2.1")
        guard = OpenGuard(self.path, "sig")
        self.assertEqual(guard.clear(), ["unit3_world"])
        self.assertEqual(OpenGuard(self.path, "sig").quarantined, {})

    def test_an_unwritable_file_never_raises_into_an_open(self):
        """Losing the note costs the next start a freeze, not this one a camera."""
        guard = OpenGuard(os.path.join(self.dir.name, "nope", "x.json"), "sig")
        with mock.patch("os.makedirs", side_effect=OSError("read-only")):
            with self.assertLogs("pupilrec.quarantine", level="WARNING"):
                with guard.attempting("left_world", "3-1.1"):
                    pass

    def test_a_signature_follows_the_ports_not_the_order(self):
        cams = headset("3-1") + headset("3-2", eyes=2)
        self.assertEqual(signature_of(cams), signature_of(list(reversed(cams))))
        self.assertNotEqual(signature_of(cams), signature_of(headset("3-1")))


class BuildWorkersTest(unittest.TestCase):
    def workers(self, cams, guard=None, cfg=None):
        cfg = cfg or Config()
        with mock.patch.object(usbmap, "discover_sysfs", return_value=cams):
            return [w.cam_id for w in build_workers(cfg, guard)], cfg

    def test_a_quarantined_camera_gets_no_worker(self):
        guard = NoGuard()
        with mock.patch.object(NoGuard, "is_quarantined",
                               lambda self, cam_id: cam_id == "left_eye"):
            with self.assertLogs("pupilrec.capture", level="WARNING"):
                names, _ = self.workers(headset("3-1") + headset("3-7"), guard)
        self.assertNotIn("left_eye", names)
        self.assertEqual(sorted(names), ["left_world", "right_eye", "right_world"])

    def test_the_rest_still_come_up_when_every_eye_is_quarantined(self):
        """Down to the world cameras alone is still a working recorder."""
        with mock.patch.object(NoGuard, "is_quarantined",
                               lambda self, cam_id: cam_id.split("_")[1].startswith("eye")):
            with self.assertLogs("pupilrec.capture", level="WARNING"):
                names, _ = self.workers(headset("3-1") + headset("3-7"), NoGuard())
        self.assertEqual(sorted(names), ["left_world", "right_world"])

    def test_workers_carry_the_guard_into_their_opens(self):
        guard = NoGuard()
        cfg = Config()
        with mock.patch.object(usbmap, "discover_sysfs", return_value=headset("3-1")):
            workers = build_workers(cfg, guard)
        self.assertTrue(all(w.guard is guard for w in workers))


class HeadsetLabelTest(unittest.TestCase):
    def test_a_third_headset_is_unit3_not_unit2(self):
        cfg = Config()
        cfg.headsets = {"3-1": "left", "3-7": "right"}
        self.assertEqual(cfg.side_for("3-2", 1), "unit3")

    def test_a_moved_headset_does_not_leave_its_label_behind(self):
        cfg = Config()
        cfg.headsets = {"3-1": "left", "3-7": "right", "3-2": "unit3"}
        self.assertEqual(cfg.forget_absent_headsets({"3-1", "3-7", "3-6"}),
                         ["3-2 (unit3)"])
        self.assertEqual(cfg.side_for("3-6", 1), "unit3")   # the number is free again

    def test_a_chosen_name_survives_being_unplugged(self):
        cfg = Config()
        cfg.headsets = {"3-1": "left", "3-7": "right", "3-2": "kalibralo"}
        self.assertEqual(cfg.forget_absent_headsets({"3-1"}), [])
        self.assertEqual(cfg.headsets["3-2"], "kalibralo")

    def test_remembered_camera_settings_keep_a_label_alive(self):
        """Dropping the label would orphan the settings stored under it."""
        cfg = Config()
        cfg.headsets = {"3-1": "left", "3-2": "unit3"}
        cfg.camera_controls = {"unit3_eye": {"Brightness": 40}}
        self.assertEqual(cfg.forget_absent_headsets({"3-1"}), [])
        self.assertEqual(cfg.headsets["3-2"], "unit3")


if __name__ == "__main__":
    unittest.main()


class OpenOrderTest(unittest.TestCase):
    """Which camera is lost when the bus fills up is a decision, not a race."""

    def chain(self, cams):
        with mock.patch.object(usbmap, "discover_sysfs", return_value=cams):
            workers = build_workers(Config())
        by_event = {w.first_open_done: w for w in workers}
        first = [w for w in workers if w.open_after is None]
        self.assertEqual(len(first), 1, "exactly one camera opens without waiting")
        order, current = [], first[0]
        while current is not None:
            order.append(current.cam_id)
            nxt = [w for w in workers if w.open_after is current.first_open_done]
            current = nxt[0] if nxt else None
        self.assertEqual(len(order), len(workers), "every camera is in the chain")
        return order

    def test_world_cameras_open_before_any_eye(self):
        order = self.chain(headset("3-1", eyes=2) + headset("3-7"))
        roles = ["world" if "world" in name else "eye" for name in order]
        self.assertEqual(roles, ["world", "world", "eye", "eye", "eye"])

    def test_a_headset_without_a_world_camera_still_chains(self):
        order = self.chain(headset("3-1", eyes=2)[1:])       # eye cameras only
        self.assertEqual(len(order), 2)

    def test_one_camera_needs_no_chain(self):
        order = self.chain(headset("3-1", eyes=0))
        self.assertEqual(len(order), 1)

    def test_stopping_an_unopened_camera_releases_the_next(self):
        with mock.patch.object(usbmap, "discover_sysfs",
                               return_value=headset("3-1", eyes=2)):
            workers = build_workers(Config())
        waiting = [w for w in workers if w.open_after is not None][0]
        blocker = [w for w in workers if w.first_open_done is waiting.open_after][0]
        blocker.stop()
        self.assertTrue(waiting.open_after.wait(timeout=1),
                        "a camera stopped before opening must not strand the queue")
