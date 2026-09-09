#!/usr/bin/env python3
"""Log GNSS position continuously, and into each recording as it happens.

    ./run_gps.py                 # 10 Hz into a recording, 0.1 Hz when idle
    ./run_gps.py --gps-only      # 18 Hz, GPS only, at the cost of fix quality
    ./run_gps.py --probe         # report what the receiver is doing and exit

The receiver is optional: with none attached this waits quietly for one and
nothing else in the system is affected.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    # Imported here, as probe() does with its own, so the module stays cheap.
    from gpslog.daemon import IDLE_PERIOD_S

    p = argparse.ArgumentParser(description="Continuous GNSS logger")
    p.add_argument("--server", default="http://127.0.0.1:8080",
                   help="recorder to follow for the per-recording copy")
    p.add_argument("--dir", default=os.path.join(PROJECT_ROOT, "gps"),
                   help="where the daily logs go (default: %(default)s)")
    p.add_argument("--device", default="", help="serial device (default: autodetect)")
    p.add_argument("--period-ms", type=int, default=0,
                   help="navigation period; below 55 ms the receiver clamps to 100")
    p.add_argument("--gps-only", action="store_true",
                   help="disable GLONASS/Galileo/BeiDou to reach 18 Hz")
    p.add_argument("--idle-period", type=float, default=IDLE_PERIOD_S,
                   help="seconds between logged rows while nothing is being "
                        "recorded (default: %(default)s); 0 logs every row")
    p.add_argument("--probe", action="store_true", help="report status and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def probe(args) -> int:
    import serial

    from gpslog import ublox as U
    from gpslog.logger import find_device

    path = args.device or find_device()
    if not path:
        print("No GNSS receiver found.")
        return 1
    print(f"receiver: {path}")
    port = serial.Serial(path, 115200, timeout=0.2)
    port.write(U.cfg_gnss(U.GPS_ONLY if args.gps_only else U.ALL_GNSS))
    time.sleep(1.5)
    port.write(U.cfg_rate(args.period_ms or (55 if args.gps_only else 100)))
    port.write(U.cfg_message(U.CLS_NAV, U.MSG_PVT, 1))
    time.sleep(0.5)
    port.reset_input_buffer()

    reader, seen, last, start = U.Reader(), 0, None, time.time()
    while time.time() - start < 5:
        for cls, msg, payload in reader.feed(port.read(4096) or b""):
            if (cls, msg) == (U.CLS_NAV, U.MSG_PVT):
                seen += 1
                last = U.parse_pvt(payload)
    port.close()

    print(f"rate: {seen / (time.time() - start):.1f} Hz")
    if last is None:
        print("no navigation messages -- is this a u-blox receiver?")
        return 1
    print(f"fix: {last['fix']} (valid={last['fix_ok']}), satellites: {last['num_sv']}, "
          f"PDOP: {last['pdop']}")
    if last["fix_ok"]:
        print(f"position: {last['lat_deg']:.7f}, {last['lon_deg']:.7f} "
              f"+/- {last['h_acc_m']:.1f} m, altitude {last['hmsl_m']:.1f} m")
    else:
        print("no position yet -- indoors this is expected; rows are still logged "
              "with the fix status so the gap is visible in the data")
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(line_buffering=True)

    if args.probe:
        return probe(args)

    from gpslog.daemon import GpsDaemon

    period = args.period_ms or (55 if args.gps_only else 100)
    return GpsDaemon(args.server, args.dir, period, args.gps_only, args.device,
                     idle_period_s=max(0.0, args.idle_period)).run()


if __name__ == "__main__":
    sys.exit(main())
