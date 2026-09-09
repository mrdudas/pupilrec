#!/usr/bin/env python3
"""Start the capture threads and the web front-end.

    ./run.py                 # serve on 0.0.0.0:8080
    ./run.py --list          # just show which camera sits on which port
    ./run.py --swap-sides    # exchange the left/right labels and save
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from pupilrec import systemd
from pupilrec.capture import CameraSupervisor, build_workers
from pupilrec.config import Config
from pupilrec.server import local_addresses, serve


def parse_args():
    p = argparse.ArgumentParser(description="Pupil Core multi-headset recorder")
    p.add_argument("--host", help="bind address (default from config)")
    p.add_argument("--port", type=int, help="bind port (default from config)")
    p.add_argument("--recordings", help="directory to write recordings into")
    p.add_argument("--bandwidth-factor", type=float,
                   help="libuvc isochronous bandwidth factor (default 2.0)")
    p.add_argument("--list", action="store_true",
                   help="list the attached cameras and exit")
    p.add_argument("--swap-sides", action="store_true",
                   help="swap which front port is labelled left and right, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # Keep progress visible when stdout is a log file rather than a terminal.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # pyuvc logs every control transfer at debug level; far too chatty for -v.
    logging.getLogger("uvc").setLevel(logging.WARNING)

    # Started before the cameras are touched, because opening one is itself a
    # place the process can freeze: pyuvc holds the GIL inside libuvc, so a
    # camera that never answers takes every thread down with it, this one
    # included, and systemd restarting the service is the only way out.
    watchdog = systemd.Watchdog()
    watchdog.start()
    # The interval has been read; take it out of the environment so that ffmpeg
    # and systemctl, which this process spawns, cannot answer in its place.
    systemd.clear_environment()

    cfg = Config.load()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.recordings:
        cfg.recordings_dir = args.recordings
    if args.bandwidth_factor:
        cfg.bandwidth_factor = args.bandwidth_factor

    if args.swap_sides:
        watchdog.stop()
        flip = {"left": "right", "right": "left"}
        cfg.headsets = {port: flip.get(side, side) for port, side in cfg.headsets.items()}
        cfg.save()
        print("Sides swapped:")
        for port, side in sorted(cfg.headsets.items()):
            print(f"  USB port {port} -> {side}")
        return 0

    try:
        workers = build_workers(cfg)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cfg.save()          # persist the port -> side assignment picked on first run

    if args.list:
        watchdog.stop()
        for w in workers:
            print(f"{w.cam_id:14s} {w.product:16s} usb={w.usb_path:8s} "
                  f"uid={w.uid:8s} {w.width}x{w.height}@{w.fps}")
        return 0

    for worker in workers:
        worker.start()

    httpd, state = serve(cfg, workers)

    # Headsets are optional and may be plugged in later.  The supervisor cannot
    # start such a camera itself, but it can say one is waiting.
    supervisor = CameraSupervisor(
        cfg, state.note_attached, lambda: state.session is not None)
    supervisor.start()

    def health_line() -> str:
        """One line for `systemctl status`, built without taking a lock.

        Whatever this reads must never block: a status line is decoration, and
        this thread stalling is what tells systemd to restart the recorder.
        """
        running = list(state.workers.values())
        up = sum(1 for w in running if w.stats.connected)
        line = f"{up}/{len(running)} cameras streaming"
        session = state.session
        return line if session is None else f"{line}, recording {session.name}"

    watchdog.set_status(health_line)
    print(f"\n  {len(workers)} cameras streaming. Open on the iPad:")
    for url in local_addresses(cfg.port):
        print(f"    {url}")
    print("\n  Ctrl-C to stop.\n")

    stopping = threading.Event()

    def shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        print("\nshutting down…")
        # Tell systemd the silence from here on is deliberate, so a slow but
        # healthy shutdown is not mistaken for the freeze this guards against.
        systemd.stopping("shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever()
    finally:
        supervisor.stop()
        if state.session is not None:
            logging.info("finalising the running recording")
            state.stop_recording()
        # state.workers, not the startup list: it may have grown since.
        running = list(state.workers.values())
        for worker in running:
            worker.stop()
        for worker in running:
            worker.join(timeout=5)
        watchdog.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
