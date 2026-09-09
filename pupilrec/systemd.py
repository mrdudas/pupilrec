"""Proving to systemd that this process is still alive.

The recorder can freeze in a way that no timeout inside it can ever catch.
pyuvc does its USB work with the GIL held, so a camera wedged inside libuvc
stops *every* thread in the process: the HTTP server, the other capture
threads, and signal delivery with them.  A frozen recorder cannot notice it is
frozen, and it cannot be asked to stop either -- SIGTERM is queued and never
delivered, because delivering it needs the interpreter to run.  It has happened
twice in a day: once on a camera close after a headset's USB hub re-enumerated,
once on a camera open at start-up, and only SIGKILL from outside ended either.

systemd can watch from outside.  While this process can still run Python it
sends a ping; when the pings stop, systemd kills and restarts the service.
That the ping comes from a plain Python thread is the whole point -- it is a
direct test of the thing that fails, because the freeze takes this thread down
with everything else.

Nothing here needs systemd to be present.  With no NOTIFY_SOCKET in the
environment every call is a no-op, so running run.py from a terminal behaves
exactly as it did before.
"""

from __future__ import annotations

import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)

# systemd wants a ping at least twice per interval.  A third of it leaves room
# for a scheduling hiccup, so a late ping always means a real stall.
PING_DIVISOR = 3


def _socket_address() -> str | None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return None
    # A leading "@" means the abstract namespace, which is a NUL on the wire.
    return "\0" + address[1:] if address.startswith("@") else address


class _Notifier:
    """One datagram socket to systemd, or nothing at all when run by hand."""

    def __init__(self):
        self._address = _socket_address()
        self._sock = None
        self._warned = False
        if self._address is None:
            return
        try:
            self._sock = socket.socket(socket.AF_UNIX,
                                       socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        except OSError:
            logger.exception("could not open the systemd notify socket")

    @property
    def available(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def send(self, message: str) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(message.encode(), self._address)
        except OSError as exc:
            # A notification that cannot be sent must never take the recorder
            # with it.  The worst case is a restart of a process that was fine,
            # which is a great deal better than an exception in a capture path.
            if not self._warned:
                logger.warning("systemd notification failed: %s", exc)
                self._warned = True


_notifier = _Notifier()


def notify(message: str) -> None:
    _notifier.send(message)


def clear_environment() -> None:
    """Stop child processes from notifying systemd on this service's behalf.

    Anything spawned from here inherits NOTIFY_SOCKET, and a child that knows
    the protocol will use it: `systemctl is-active`, which the health panel
    runs, reports its own EXIT_STATUS down our socket.  NotifyAccess=main makes
    systemd ignore those, but the tidier fix is the one sd_notify offers with
    its unset_environment flag -- take the variables out of the environment
    once they have been read, so no child ever sees them.  The socket this
    process already opened keeps working.
    """
    for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        os.environ.pop(name, None)


def stopping(status: str = "") -> None:
    """Tell systemd the shutdown is deliberate, so it stops watching for pings."""
    notify("STOPPING=1" + (f"\nSTATUS={status}" if status else ""))


def watchdog_interval() -> float | None:
    """Seconds between pings, or None when systemd is not watching.

    WatchdogSec in the unit is what puts WATCHDOG_USEC here; without it the
    watchdog stays off and this process is on its own, exactly as before.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec or not _notifier.available:
        return None
    # Set when systemd means the watchdog for this process specifically; a
    # child that inherited the environment must not answer on its behalf.
    owner = os.environ.get("WATCHDOG_PID")
    if owner and owner != str(os.getpid()):
        return None
    try:
        return max(1.0, int(usec) / 1e6 / PING_DIVISOR)
    except ValueError:
        logger.warning("ignoring unreadable WATCHDOG_USEC=%r", usec)
        return None


class Watchdog(threading.Thread):
    """Pings systemd for as long as this process can still run Python.

    `status` is an optional callable returning one short line for
    `systemctl status`.  It must not take a lock the rest of the program holds
    for any length of time: a status line is not worth a restart, and this
    thread going quiet is what triggers one.
    """

    def __init__(self, status=None):
        super().__init__(name="systemd-watchdog", daemon=True)
        self.interval = watchdog_interval()
        self._status = status
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return self.interval is not None

    def set_status(self, status) -> None:
        """Attach the status line once there is something worth reporting."""
        self._status = status

    def stop(self) -> None:
        self._stop.set()

    def _message(self) -> str:
        if self._status is None:
            return "WATCHDOG=1"
        try:
            return f"WATCHDOG=1\nSTATUS={self._status()}"
        except Exception:
            # Never let a broken status line stop the pings; that would turn a
            # cosmetic bug into a restart loop.
            logger.exception("watchdog status callback failed")
            return "WATCHDOG=1"

    def run(self) -> None:
        if self.interval is None:
            return
        logger.info("systemd watchdog: pinging every %.1fs", self.interval)
        notify(self._message())         # do not make systemd wait for the first
        while not self._stop.wait(self.interval):
            notify(self._message())
