"""The liveness watchdog, checked against a stand-in for systemd.

The point of the watchdog is that it fails when the interpreter stops running,
which is exactly what cannot be arranged in a test: freezing the GIL would
freeze the test too.  What is checked here is everything around that -- that
the pings are well formed and land on the socket, that the interval is read the
way systemd sets it, and above all that a machine without systemd, or with a
socket that has gone away, keeps running rather than raising into a capture
thread.
"""

import os
import socket
import tempfile
import unittest
from unittest import mock

from pupilrec import systemd


class FakeSystemd:
    """A datagram socket standing in for systemd's notify socket."""

    def __init__(self, directory):
        self.path = os.path.join(directory, "notify")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(2.0)

    def receive(self) -> str:
        return self.sock.recv(4096).decode()

    def close(self):
        self.sock.close()


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.systemd = FakeSystemd(self.dir.name)
        self.addCleanup(self.systemd.close)

    def notifier(self, **env):
        """A module-level notifier built against the fake socket."""
        env.setdefault("NOTIFY_SOCKET", self.systemd.path)
        with mock.patch.dict(os.environ, env, clear=False):
            notifier = systemd._Notifier()
        self.addCleanup(notifier.close)
        return notifier

    def test_a_message_reaches_the_socket(self):
        notifier = self.notifier()
        notifier.send("WATCHDOG=1")
        self.assertEqual(self.systemd.receive(), "WATCHDOG=1")

    def test_without_systemd_nothing_is_sent_and_nothing_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            notifier = systemd._Notifier()
        self.addCleanup(notifier.close)
        self.assertFalse(notifier.available)
        notifier.send("WATCHDOG=1")          # must not raise

    def test_a_socket_that_went_away_is_survivable(self):
        """A failed notification must never propagate into the caller."""
        notifier = self.notifier()
        self.systemd.close()
        os.unlink(self.systemd.path)
        with self.assertLogs("pupilrec.systemd", "WARNING"):
            notifier.send("WATCHDOG=1")      # must not raise
        notifier.send("WATCHDOG=1")          # and must not warn twice

    def test_an_abstract_socket_address_becomes_a_leading_nul(self):
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "@systemd/notify"}):
            self.assertEqual(systemd._socket_address(), "\0systemd/notify")

    def test_a_filesystem_socket_address_is_used_as_is(self):
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "/run/systemd/notify"}):
            self.assertEqual(systemd._socket_address(), "/run/systemd/notify")


class IntervalTest(unittest.TestCase):
    def interval(self, available=True, **env):
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(type(systemd._notifier), "available",
                               property(lambda _self: available)):
            return systemd.watchdog_interval()

    def test_pings_go_out_well_inside_the_deadline(self):
        """A ping at a third of the interval survives a scheduling hiccup."""
        self.assertAlmostEqual(self.interval(WATCHDOG_USEC="20000000"), 20 / 3)

    def test_no_watchdog_configured_means_no_pinging(self):
        self.assertIsNone(self.interval())

    def test_no_systemd_means_no_pinging(self):
        self.assertIsNone(self.interval(available=False, WATCHDOG_USEC="20000000"))

    def test_a_child_does_not_answer_for_its_parent(self):
        """WATCHDOG_PID names the process systemd is actually watching."""
        self.assertIsNone(self.interval(WATCHDOG_USEC="20000000",
                                        WATCHDOG_PID=str(os.getpid() + 1)))

    def test_our_own_pid_is_accepted(self):
        self.assertIsNotNone(self.interval(WATCHDOG_USEC="20000000",
                                           WATCHDOG_PID=str(os.getpid())))

    def test_nonsense_from_the_environment_disables_it(self):
        with self.assertLogs("pupilrec.systemd", "WARNING"):
            self.assertIsNone(self.interval(WATCHDOG_USEC="soon"))


class WatchdogThreadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.systemd = FakeSystemd(self.dir.name)
        self.addCleanup(self.systemd.close)
        notifier = self._notifier_for(self.systemd.path)
        self.addCleanup(notifier.close)
        patch = mock.patch.object(systemd, "_notifier", notifier)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _notifier_for(path):
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
            return systemd._Notifier()

    def watchdog(self, status=None, usec="300000"):
        with mock.patch.dict(os.environ, {"WATCHDOG_USEC": usec}):
            return systemd.Watchdog(status=status)

    def test_it_pings_immediately_rather_than_making_systemd_wait(self):
        dog = self.watchdog()
        self.assertTrue(dog.enabled)
        dog.start()
        self.addCleanup(dog.stop)
        self.assertEqual(self.systemd.receive(), "WATCHDOG=1")

    def test_the_status_line_rides_along(self):
        dog = self.watchdog(status=lambda: "4/4 cameras streaming")
        dog.start()
        self.addCleanup(dog.stop)
        self.assertEqual(self.systemd.receive(),
                         "WATCHDOG=1\nSTATUS=4/4 cameras streaming")

    def test_a_broken_status_line_does_not_stop_the_pings(self):
        """A cosmetic bug must not become a restart loop."""
        def explode():
            raise RuntimeError("no status for you")

        dog = self.watchdog(status=explode)
        with self.assertLogs("pupilrec.systemd", "ERROR"):
            dog.start()
            self.addCleanup(dog.stop)
            self.assertEqual(self.systemd.receive(), "WATCHDOG=1")

    def test_it_keeps_pinging(self):
        dog = self.watchdog()
        dog.start()
        self.addCleanup(dog.stop)
        for _ in range(3):
            self.assertEqual(self.systemd.receive(), "WATCHDOG=1")

    def test_without_a_watchdog_the_thread_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            dog = systemd.Watchdog()
        self.assertFalse(dog.enabled)
        dog.start()
        dog.join(timeout=2)
        self.assertFalse(dog.is_alive())

    def test_children_cannot_notify_on_this_service_s_behalf(self):
        """`systemctl is-active`, run for the health panel, would otherwise
        report its own EXIT_STATUS down our socket."""
        env = {"NOTIFY_SOCKET": "/run/x", "WATCHDOG_USEC": "20000000",
               "WATCHDOG_PID": "1", "PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            systemd.clear_environment()
            self.assertNotIn("NOTIFY_SOCKET", os.environ)
            self.assertNotIn("WATCHDOG_USEC", os.environ)
            self.assertNotIn("WATCHDOG_PID", os.environ)
            self.assertEqual(os.environ["PATH"], "/usr/bin")

    def test_clearing_the_environment_leaves_the_open_socket_working(self):
        dog = self.watchdog()
        with mock.patch.dict(os.environ, {}, clear=False):
            systemd.clear_environment()
        dog.start()
        self.addCleanup(dog.stop)
        self.assertEqual(self.systemd.receive(), "WATCHDOG=1")

    def test_a_deliberate_shutdown_is_announced(self):
        """Otherwise a slow but healthy stop looks like the freeze we watch for."""
        systemd.stopping("shutting down")
        self.assertEqual(self.systemd.receive(),
                         "STOPPING=1\nSTATUS=shutting down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
