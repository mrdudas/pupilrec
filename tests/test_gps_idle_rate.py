"""How much of the GNSS stream reaches the always-on log.

At 10 Hz around the clock the daily log reached 535,000 rows and 68 MB in a
day, nearly all of it "no fix" from a receiver sitting indoors.  Full rate is
worth it inside a recording and almost nowhere else, so the log is thinned when
nothing is being recorded -- without ever thinning what a recording gets.
"""

import unittest
from unittest import mock

from gpslog.daemon import IDLE_PERIOD_S, GpsDaemon, host_times


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


class HostTimeTest(unittest.TestCase):
    """One read can carry several messages, and they did not all arrive at
    once.  Measured before this: 99 host-time gaps of 0.000 s alternating with
    99 of 0.200 s, while the receiver said every message was 0.100 s apart."""

    @staticmethod
    def times(itows, arrival=1000.0):
        return [round(t, 4) for _, t in
                host_times([{"itow_s": w} for w in itows], arrival)]

    def test_one_message_keeps_the_arrival_time(self):
        self.assertEqual(self.times([500.0]), [1000.0])

    def test_two_messages_in_one_read_are_pulled_apart(self):
        """The later one arrived now; the earlier one, 100 ms ago."""
        self.assertEqual(self.times([500.0, 500.1]), [999.9, 1000.0])

    def test_a_whole_batch_keeps_the_receiver_s_spacing(self):
        self.assertEqual(self.times([500.0, 500.1, 500.2, 500.3]),
                         [999.7, 999.8, 999.9, 1000.0])

    def test_no_two_rows_share_a_timestamp(self):
        stamps = self.times([500.0 + n / 10 for n in range(5)])
        self.assertEqual(len(set(stamps)), 5)

    def test_a_week_rollover_falls_back_to_the_read_time(self):
        """iTOW restarts at the end of a GPS week; better a coarse time than
        a row claiming to be from six days ago."""
        self.assertEqual(self.times([604799.9, 0.0]), [1000.0, 1000.0])

    def test_a_wildly_wrong_itow_falls_back(self):
        self.assertEqual(self.times([100.0, 500.0]), [1000.0, 1000.0])

    def test_a_missing_itow_falls_back(self):
        fixes = [{"itow_s": None}, {"itow_s": 500.1}]
        self.assertEqual([t for _, t in host_times(fixes, 1000.0)],
                         [1000.0, 1000.0])

    def test_an_empty_read_produces_nothing(self):
        self.assertEqual(host_times([], 1000.0), [])

    def test_the_fixes_come_back_untouched_and_in_order(self):
        fixes = [{"itow_s": 500.0, "fix": "3D"}, {"itow_s": 500.1, "fix": "2D"}]
        out = host_times(fixes, 1000.0)
        self.assertEqual([f["fix"] for f, _ in out], ["3D", "2D"])
        self.assertIs(out[0][0], fixes[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
