"""How much of the GNSS stream reaches the always-on log.

At 10 Hz around the clock the daily log reached 535,000 rows and 68 MB in a
day, nearly all of it "no fix" from a receiver sitting indoors.  Full rate is
worth it inside a recording and almost nowhere else, so the log is thinned when
nothing is being recorded -- without ever thinning what a recording gets.
"""

import unittest
from unittest import mock

from gpslog.daemon import IDLE_PERIOD_S, GpsDaemon


def daemon(idle_period_s=IDLE_PERIOD_S):
    with mock.patch("gpslog.daemon.ServerWatcher"), \
         mock.patch("gpslog.daemon.DailyLog"), \
         mock.patch("gpslog.daemon.Heartbeat"):
        return GpsDaemon("http://x", "/tmp/x", idle_period_s=idle_period_s)


def stream(dog, seconds, hz=10.0, recording=False, fix="3D"):
    """Feed `seconds` of receiver output and count what the log keeps."""
    kept = 0
    for step in range(int(seconds * hz)):
        if dog.keep_row(step / hz, {"fix": fix}, recording):
            kept += 1
    return kept


class IdleRateTest(unittest.TestCase):
    def test_a_recording_keeps_every_single_row(self):
        self.assertEqual(stream(daemon(), 60, recording=True), 600)

    def test_idle_logging_settles_at_a_tenth_of_a_hertz(self):
        """One row per ten seconds, whatever the receiver is doing."""
        self.assertEqual(stream(daemon(), 600), 60)      # 600 s / 10 s

    def test_a_faster_receiver_does_not_mean_a_bigger_log(self):
        """--gps-only runs the receiver at 18 Hz; the idle log does not care."""
        self.assertEqual(stream(daemon(), 600, hz=18.0), 60)

    def test_the_idle_period_is_adjustable(self):
        self.assertEqual(stream(daemon(idle_period_s=60.0), 600), 10)

    def test_a_zero_period_turns_the_thinning_off(self):
        """The old behaviour has to remain reachable."""
        self.assertEqual(stream(daemon(idle_period_s=0.0), 60), 600)

    def test_the_saving_is_the_point(self):
        """An idle hour: 36,000 rows off the receiver, 360 into the log."""
        delivered = 3600 * 10
        self.assertEqual(stream(daemon(), 3600), delivered // 100)


class FixChangeTest(unittest.TestCase):
    """A thinned log still has to say when the fix appeared, not ten seconds
    later -- that is the one event in an idle log worth having to the second."""

    def test_gaining_a_fix_is_logged_at_once(self):
        dog = daemon()
        dog.keep_row(0.0, {"fix": "none"}, False)
        self.assertFalse(dog.keep_row(1.0, {"fix": "none"}, False))
        self.assertTrue(dog.keep_row(1.1, {"fix": "3D"}, False))

    def test_losing_a_fix_is_logged_at_once(self):
        dog = daemon()
        dog.keep_row(0.0, {"fix": "3D"}, False)
        self.assertTrue(dog.keep_row(0.5, {"fix": "none"}, False))

    def test_an_unchanged_fix_does_not_reset_the_clock_early(self):
        dog = daemon()
        dog.keep_row(0.0, {"fix": "3D"}, False)
        self.assertFalse(dog.keep_row(9.9, {"fix": "3D"}, False))
        self.assertTrue(dog.keep_row(10.0, {"fix": "3D"}, False))

    def test_a_change_restarts_the_idle_interval_from_there(self):
        dog = daemon()
        dog.keep_row(0.0, {"fix": "none"}, False)
        dog.keep_row(3.0, {"fix": "3D"}, False)     # kept: the fix changed
        self.assertFalse(dog.keep_row(12.0, {"fix": "3D"}, False))
        self.assertTrue(dog.keep_row(13.0, {"fix": "3D"}, False))


class TransitionTest(unittest.TestCase):
    def test_starting_a_recording_takes_effect_on_the_next_row(self):
        """No warm-up: the receiver was never slowed down, so full rate is
        available the instant a recording begins."""
        dog = daemon()
        dog.keep_row(0.0, {"fix": "3D"}, False)
        self.assertTrue(dog.keep_row(0.1, {"fix": "3D"}, True))
        self.assertTrue(dog.keep_row(0.2, {"fix": "3D"}, True))

    def test_stopping_a_recording_goes_quiet_again(self):
        dog = daemon()
        for step in range(10):
            dog.keep_row(step / 10, {"fix": "3D"}, True)
        self.assertFalse(dog.keep_row(1.0, {"fix": "3D"}, False))
        self.assertTrue(dog.keep_row(11.0, {"fix": "3D"}, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
