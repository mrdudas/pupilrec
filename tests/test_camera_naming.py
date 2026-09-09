"""Camera identification with more headsets and cameras than are on the desk.

The rig this was built against has two headsets with one eye camera each. A
third headset, or a second eye camera on a headset, cannot be plugged in here to
try, so the naming and grouping are exercised against synthetic device lists.
What that does verify is the part that would break: name collisions and label
reuse. It cannot verify USB bandwidth, which only real hardware can answer.
"""

import unittest
from unittest import mock

from pupilrec import usbmap
from pupilrec.capture import build_workers
from pupilrec.config import Config


def cam(uid, product, usb_path, role):
    return usbmap.UsbCam(uid=uid, product=product, usb_path=usb_path,
                         root_port=usb_path.split(".")[0], role=role)


def headset(port, eyes=1, worlds=1):
    """One headset's cameras, laid out the way a Pupil Core presents them."""
    out = []
    for i in range(worlds):
        out.append(cam(f"3:{port}{i}0", "Pupil Cam1 ID2", f"{port}.{i + 1}", "world"))
    for i in range(eyes):
        out.append(cam(f"3:{port}{i}1", f"Pupil Cam2 ID{i}", f"{port}.{i + 4}", "eye"))
    return out


class NamingTest(unittest.TestCase):
    def workers(self, cams, cfg=None):
        cfg = cfg or Config()
        with mock.patch.object(usbmap, "discover_sysfs", return_value=cams):
            return [w.cam_id for w in build_workers(cfg)], cfg

    def test_two_headsets_unchanged(self):
        """The names recordings already use must not move."""
        names, _ = self.workers(headset("3-1") + headset("3-7"))
        self.assertEqual(sorted(names),
                         ["left_eye", "left_world", "right_eye", "right_world"])

    def test_third_headset_gets_its_own_label(self):
        names, cfg = self.workers(headset("3-1") + headset("3-7") + headset("3-9"))
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6, "camera ids must be unique")
        self.assertIn("unit3_world", names)
        self.assertIn("unit3_eye", names)
        self.assertEqual(len(set(cfg.headsets.values())), 3, "labels must be distinct")

    def test_second_eye_camera_on_one_headset(self):
        names, _ = self.workers(headset("3-1", eyes=2) + headset("3-7"))
        self.assertEqual(sorted(names),
                         ["left_eye", "left_eye2", "left_world",
                          "right_eye", "right_world"])

    def test_six_cameras_three_headsets_two_eyes(self):
        cams = headset("3-1", eyes=2) + headset("3-7", eyes=2) + headset("3-9", eyes=2)
        names, _ = self.workers(cams)
        self.assertEqual(len(names), 9)
        self.assertEqual(len(set(names)), 9)

    def test_single_headset(self):
        """The second headset is optional too."""
        names, _ = self.workers(headset("3-1"))
        self.assertEqual(sorted(names), ["left_eye", "left_world"])

    def test_labels_are_stable_across_runs(self):
        cfg = Config()
        first, _ = self.workers(headset("3-1") + headset("3-7"), cfg)
        # A later run sees the ports in a different order; labels must not move.
        second, _ = self.workers(headset("3-7") + headset("3-1"), cfg)
        self.assertEqual(sorted(first), sorted(second))

    def test_existing_labels_are_never_reused(self):
        cfg = Config()
        cfg.headsets = {"3-7": "left"}          # a hand-edited config
        names, cfg = self.workers(headset("3-1") + headset("3-7"), cfg)
        self.assertEqual(len(set(cfg.headsets.values())), 2)
        self.assertEqual(len(set(names)), 4)

    def test_modes_follow_the_role_not_the_name(self):
        cfg = Config()
        with mock.patch.object(usbmap, "discover_sysfs",
                               return_value=headset("3-1", eyes=2)):
            workers = {w.cam_id: w for w in build_workers(cfg)}
        self.assertEqual((workers["left_eye2"].width, workers["left_eye2"].fps),
                         (400, 120))
        self.assertEqual((workers["left_world"].width, workers["left_world"].fps),
                         (1280, 60))


if __name__ == "__main__":
    unittest.main(verbosity=2)
