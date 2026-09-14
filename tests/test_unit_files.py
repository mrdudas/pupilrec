"""The systemd settings the recorder depends on, as it will be installed.

Three of these were wrong in a way nothing else could catch: a key systemd
ignored, a kill mode that killed the recorder's own children, and a watchdog
with no restart to back it up.  A unit file is code that runs once per boot and
whose mistakes are silent -- systemd logs "Unknown key" and carries on -- so the
settings that were paid for in debugging are pinned here.
"""

import configparser
import os
import unittest

SETUP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "setup")


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str            # systemd keys are case sensitive
    with open(os.path.join(SETUP, name)) as fh:
        parser.read_file(fh)
    return parser


class RateLimitTest(unittest.TestCase):
    """StartLimit* belong to [Unit]; under [Service] systemd ignores them."""

    UNITS = ("pupilrec.service", "pupilrec-gps.service", "pupilrec-sensors.service")

    def test_the_rate_limit_is_where_systemd_reads_it(self):
        for name in self.UNITS:
            with self.subTest(name):
                conf = unit(name)
                self.assertIn("StartLimitIntervalSec", conf["Unit"])
                self.assertIn("StartLimitBurst", conf["Unit"])
                self.assertNotIn("StartLimitIntervalSec", conf["Service"])
                self.assertNotIn("StartLimitBurst", conf["Service"])

    def test_every_unit_restarts_by_itself(self):
        for name in self.UNITS:
            with self.subTest(name):
                self.assertIn(unit(name)["Service"]["Restart"], ("always", "on-failure"))


class RecorderUnitTest(unittest.TestCase):
    def setUp(self):
        self.conf = unit("pupilrec.service")["Service"]

    def test_ffmpeg_is_not_killed_along_with_the_recorder(self):
        """control-group, the default, truncates every video on a restart."""
        self.assertEqual(self.conf.get("KillMode"), "mixed")

    def test_the_watchdog_is_armed_and_answerable(self):
        """WatchdogSec without NotifyAccess is a restart loop, not a watchdog."""
        self.assertEqual(self.conf.get("NotifyAccess"), "main")
        self.assertGreater(float(self.conf["WatchdogSec"].rstrip("s")), 0)

    def test_a_watchdog_kill_is_restarted_from(self):
        """A watchdog timeout counts as a failure; on-failure has to cover it."""
        self.assertEqual(self.conf.get("Restart"), "on-failure")


if __name__ == "__main__":
    unittest.main()
