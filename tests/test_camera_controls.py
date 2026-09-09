"""Image controls: how they are shaped for the UI and how they survive a replug.

The interesting parts are the ones a camera on the desk cannot demonstrate on
demand: what happens when a headset is unplugged and put back (the camera comes
up with its factory values, and the stored ones have to be written back), and
what a camera that reports an odd control looks like by the time it reaches the
browser.  Those are exercised here against stand-in controls.

What this cannot check is whether a given camera accepts a given value; only the
hardware answers that, and it answers by clamping or refusing at write time.
"""

import unittest
from unittest import mock

from pupilrec import usbmap
from pupilrec.capture import (
    CameraWorker, _apply_order, build_workers, describe_control)
from pupilrec.config import Config


class FakeControl:
    """Stands in for a pyuvc Control, which only exists next to real hardware."""

    def __init__(self, name, value, d_type=int, min_val=0, max_val=100,
                 step=1, def_val=0, unit="processing_unit", info=0b11):
        self.display_name = name
        self.value = value
        self.d_type = d_type
        self.min_val, self.max_val, self.step = min_val, max_val, step
        self.def_val = def_val
        self.unit = unit
        self.doc = ""
        self.info_bit_mask = info
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


class DescribeTest(unittest.TestCase):
    def test_number_control_carries_its_range(self):
        d = describe_control(FakeControl("Brightness", 12, min_val=-64, max_val=64))
        self.assertEqual(d["kind"], "int")
        self.assertEqual((d["min"], d["max"], d["value"]), (-64, 64, 12))
        self.assertTrue(d["writable"])

    def test_bool_control(self):
        d = describe_control(FakeControl("Auto Focus", 1, d_type=bool))
        self.assertEqual(d["kind"], "bool")

    def test_menu_lists_its_choices(self):
        d = describe_control(FakeControl(
            "Power Line frequency", 1, d_type={"Disabled": 0, "50Hz": 1, "60Hz": 2}))
        self.assertEqual(d["kind"], "menu")
        self.assertEqual([o["value"] for o in d["options"]], [0, 1, 2])

    def test_exposure_menu_offers_only_the_supported_modes(self):
        """The camera answers GET_RES with a bitmap here, not a step size.

        A Pupil Core reports 9 -- manual and aperture priority -- and rejects a
        write of either other mode, so neither may reach the UI.
        """
        d = describe_control(FakeControl(
            "Auto Exposure Mode", 8, step=9,
            d_type={"manual mode": 1, "auto mode": 2,
                    "shutter priority mode": 4, "aperture priority mode": 8}))
        self.assertEqual([o["label"] for o in d["options"]],
                         ["manual mode", "aperture priority mode"])

    def test_a_camera_that_reports_no_bitmap_keeps_every_mode(self):
        d = describe_control(FakeControl(
            "Auto Exposure Mode", 1, step=None,
            d_type={"manual mode": 1, "auto mode": 2}))
        self.assertEqual(len(d["options"]), 2)

    def test_stray_whitespace_is_trimmed_off_the_name(self):
        """Two names pyuvc reports end in a space; that must not reach config."""
        self.assertEqual(describe_control(FakeControl("Absolute Iris ", 100))["name"],
                         "Absolute Iris")

    def test_read_only_control_is_marked(self):
        d = describe_control(FakeControl("Tilt control", 0, info=0b01))
        self.assertFalse(d["writable"])

    def test_control_held_by_an_automatic_mode_is_marked(self):
        d = describe_control(FakeControl("White Balance temperature", 4600, info=0b111))
        self.assertTrue(d["auto_disabled"])


class ApplyOrderTest(unittest.TestCase):
    def test_automatic_switches_are_written_first(self):
        """Exposure time is refused while auto exposure still owns it."""
        ordered = _apply_order([("Absolute Exposure Time", 80),
                                ("Brightness", 10),
                                ("Auto Exposure Mode", 1)])
        self.assertEqual(ordered[0][0], "Auto Exposure Mode")


class StoredControlsTest(unittest.TestCase):
    def worker(self, controls=None):
        return CameraWorker(
            cam_id="left_world", uid="3:5", role="world", side="left",
            usb_path="3-1.1", product="Pupil Cam1 ID2",
            width=1280, height=720, fps=60, bandwidth_factor=2.0,
            controls=controls)

    def test_config_round_trips_through_the_file(self):
        cfg = Config()
        cfg.remember_control("left_world", "Brightness", 25)
        with mock.patch("builtins.open", mock.mock_open()) as fh:
            cfg.save("/dev/null")
        self.assertIn("camera_controls", "".join(
            call.args[0] for call in fh().write.call_args_list))
        self.assertEqual(cfg.controls_for("left_world"), {"Brightness": 25})
        cfg.forget_controls("left_world")
        self.assertEqual(cfg.controls_for("left_world"), {})

    def test_a_worker_is_built_with_the_values_stored_for_its_own_camera(self):
        cfg = Config()
        cfg.remember_control("left_world", "Brightness", 25)
        cfg.remember_control("right_world", "Brightness", -10)
        cams = [usbmap.UsbCam("3:1", "Pupil Cam1 ID2", "3-1.1", "3-1", "world"),
                usbmap.UsbCam("3:2", "Pupil Cam1 ID2", "3-7.1", "3-7", "world")]
        with mock.patch.object(usbmap, "discover_sysfs", return_value=cams):
            workers = {w.cam_id: w for w in build_workers(cfg)}
        self.assertEqual(workers["left_world"].wanted_controls, {"Brightness": 25})
        self.assertEqual(workers["right_world"].wanted_controls, {"Brightness": -10})

    def test_a_replugged_camera_gets_its_values_written_back(self):
        """The camera comes up factory-fresh; the stored values go back in."""
        worker = self.worker({"Absolute Exposure Time": 80, "Auto Exposure Mode": 1})
        cap = mock.Mock()
        cap.controls = [FakeControl("Auto Exposure Mode", 8, def_val=8),
                        FakeControl("Absolute Exposure Time", 157, def_val=157)]
        worker._restore_controls(cap)
        self.assertEqual([c.value for c in cap.controls], [1, 80])

    def test_a_value_the_camera_will_not_take_does_not_cost_the_stream(self):
        """One rejected setting must not stop the camera from streaming."""
        class Stubborn:
            """A control the camera refuses to write, the way a busy one does."""

            display_name = "Brightness"

            @property
            def value(self):
                return 0

            @value.setter
            def value(self, _):
                raise RuntimeError("device rejected the write")

        worker = self.worker({"Brightness": 25, "Contrast": 40})
        bad = Stubborn()
        good = FakeControl("Contrast", 32)
        cap = mock.Mock()
        cap.controls = [bad, good]
        with self.assertLogs("pupilrec.capture", "WARNING"):
            worker._restore_controls(cap)      # must not raise
        self.assertEqual(good.value, 40)

    def test_a_control_the_camera_does_not_have_is_skipped(self):
        worker = self.worker({"Absolute Focus": 5})
        cap = mock.Mock()
        cap.controls = [FakeControl("Brightness", 0)]
        with self.assertLogs("pupilrec.capture", "WARNING"):
            worker._restore_controls(cap)      # must not raise

    def test_a_request_to_a_stopped_worker_fails_instead_of_hanging(self):
        """The UI must get an answer even when the capture thread is gone."""
        worker = self.worker()
        with self.assertRaises(RuntimeError):
            worker.list_controls()


class ReadControlsTest(unittest.TestCase):
    """Reading is cached by default because a full re-read stalls every camera."""

    def worker(self):
        w = CameraWorker(
            cam_id="left_eye", uid="3:6", role="eye", side="left",
            usb_path="3-1.4", product="Pupil Cam2 ID1",
            width=400, height=400, fps=120, bandwidth_factor=2.0)
        w._cap = mock.Mock()
        w._cap.controls = [FakeControl("Brightness", 10),
                           FakeControl("Gain", 20),
                           FakeControl("Contrast", 48)]
        return w

    def test_listing_touches_the_camera_not_at_all(self):
        w = self.worker()
        w._read_controls()
        self.assertEqual([c.refreshed for c in w._cap.controls], [0, 0, 0])

    def test_a_write_reads_back_only_what_it_wrote(self):
        w = self.worker()
        w._read_controls("Gain")
        self.assertEqual([c.refreshed for c in w._cap.controls], [0, 1, 0])

    def test_an_explicit_refresh_reads_every_control(self):
        w = self.worker()
        w._read_controls(refresh_all=True)
        self.assertEqual([c.refreshed for c in w._cap.controls], [1, 1, 1])

    def test_one_control_that_will_not_refresh_does_not_stop_the_rest(self):
        w = self.worker()
        w._cap.controls[0].refresh = mock.Mock(side_effect=RuntimeError("nope"))
        out = w._read_controls(refresh_all=True)
        self.assertEqual(len(out), 3)
        self.assertEqual(w._cap.controls[2].refreshed, 1)


class CloseTest(unittest.TestCase):
    """Closing a handle whose device is gone freezes the whole process.

    libuvc blocks inside close() with the GIL held, so the recorder stops
    answering entirely -- SIGTERM included.  It was seen for real: a headset
    whose USB hub re-enumerated turned a six second dropout into a ninety
    second outage, ended only by systemd's stop timeout.
    """

    def worker(self):
        w = CameraWorker(
            cam_id="right_eye", uid="3:14", role="eye", side="right",
            usb_path="3-7.3", product="Pupil Cam2 ID0",
            width=400, height=400, fps=120, bandwidth_factor=2.0)
        w._cap = mock.Mock()
        return w

    def close_with(self, worker, attached):
        with mock.patch.object(usbmap, "discover_sysfs", return_value=attached):
            worker._close()

    def test_a_camera_still_on_the_bus_is_closed_normally(self):
        w = self.worker()
        cap = w._cap
        self.close_with(w, [usbmap.UsbCam("3:14", "Pupil Cam2 ID0",
                                          "3-7.3", "3-7", "eye")])
        cap.close.assert_called_once()
        self.assertIsNone(w._cap)

    def test_a_re_enumerated_device_is_abandoned(self):
        """Same port, new address: the handle points at nothing."""
        w = self.worker()
        cap = w._cap
        with self.assertLogs("pupilrec.capture", "WARNING"):
            self.close_with(w, [usbmap.UsbCam("3:19", "Pupil Cam2 ID0",
                                              "3-7.3", "3-7", "eye")])
        cap.close.assert_not_called()
        self.assertIsNone(w._cap)
        self.assertFalse(w.stats.connected)

    def test_an_unplugged_device_is_abandoned(self):
        w = self.worker()
        cap = w._cap
        with self.assertLogs("pupilrec.capture", "WARNING"):
            self.close_with(w, [])
        cap.close.assert_not_called()

    def test_closing_twice_is_harmless(self):
        w = self.worker()
        here = [usbmap.UsbCam("3:14", "Pupil Cam2 ID0", "3-7.3", "3-7", "eye")]
        self.close_with(w, here)
        self.close_with(w, here)          # must not raise

    def test_a_sysfs_failure_falls_back_to_closing(self):
        """Unsure is not the same as gone; only skip a close we know is futile."""
        w = self.worker()
        cap = w._cap
        with mock.patch.object(usbmap, "discover_sysfs",
                               side_effect=OSError("sysfs unreadable")), \
             self.assertLogs("pupilrec.capture", "ERROR"):
            w._close()
        cap.close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
