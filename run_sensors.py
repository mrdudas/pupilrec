#!/usr/bin/env python3
"""Record the STWIN board's sensors whenever the camera recorder is recording.

    ./run_sensors.py                  # follow http://127.0.0.1:8080
    ./run_sensors.py --probe          # list what the board offers and exit

Runs as a separate process from the camera server on purpose: the cameras are
timing sensitive, and nothing here shares a thread, a lock or a USB bus with them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from sensorlog.daemon import SensorDaemon, sensor_components
from sensorlog.stwin import Stwin, StwinError
from sensorlog.writer import WAV_COMPONENTS


def parse_args():
    p = argparse.ArgumentParser(description="STWIN sensor recorder")
    p.add_argument("--server", default="http://127.0.0.1:8080",
                   help="camera recorder to follow (default: %(default)s)")
    p.add_argument("--library", default="", help="path to libhs_datalog_v2.so")
    p.add_argument("--fallback-dir", default="",
                   help="where to write if the server reports no directory")
    p.add_argument("--mics", choices=("wav", "csv"), default="wav",
                   help="microphone output format. CSV cannot keep up with the "
                        "192 kHz analog mic and loses samples (default: %(default)s)")
    p.add_argument("--probe", action="store_true",
                   help="print the board's sensors and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def probe(args) -> int:
    board = Stwin(args.library)
    board.open()
    try:
        # Clear a log left running by a process that died, otherwise the board
        # will not answer on its control channel.
        try:
            board.stop_log()
        except StwinError:
            pass
        status = board.device_status()
        components = sensor_components(status)
        print(f"library: {board.version()}")
        for component in status["devices"][0].get("components", []):
            if "firmware_info" in component:
                fw = component["firmware_info"]
                print(f"firmware: {fw.get('fw_name')} {fw.get('fw_version')} "
                      f"({fw.get('part_number')}), alias {fw.get('alias')}")
        print(f"\n{len(components)} enabled sensor components:")
        for name, cfg in components.items():
            print(f"  {name:18s} dim={cfg['dim']} type={cfg['data_type']:7s} "
                  f"samples_per_ts={cfg['samples_per_ts']:5d} "
                  f"sensitivity={cfg['sensitivity']}")
    finally:
        board.close()
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(line_buffering=True)
    try:
        if args.probe:
            return probe(args)
        wav = WAV_COMPONENTS if args.mics == "wav" else ()
        return SensorDaemon(args.server, args.library, args.fallback_dir, wav).run()
    except StwinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
