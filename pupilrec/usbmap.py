"""Identify Pupil Core cameras by the physical USB port they are plugged into.

libuvc addresses cameras as "<bus>:<device address>", but the device address is
handed out at enumeration time and changes on every replug.  The port a headset
hangs off does not, so that is what names a headset here: each Pupil Core has an
internal hub, so its cameras all share one root port ("3-1", "3-7", ...) while
sitting on different sub-ports ("3-1.1", "3-1.4").
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass

WORLD_PRODUCTS = ("Pupil Cam1 ID2", "Pupil Cam3 ID2")
EYE_PRODUCTS = ("Pupil Cam2 ID0", "Pupil Cam2 ID1", "Pupil Cam1 ID0", "Pupil Cam1 ID1")


@dataclass(frozen=True)
class UsbCam:
    uid: str          # libuvc uid, "<bus>:<address>"
    product: str      # "Pupil Cam1 ID2"
    usb_path: str     # "3-1.4"
    root_port: str    # "3-1" -- the front panel port, stable across replugs
    role: str         # "world" | "eye"


def _sysfs_index() -> dict[tuple[int, int], str]:
    """(busnum, devnum) -> sysfs name, e.g. (3, 6) -> "3-1.1"."""
    index = {}
    for path in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(path, "busnum")) as fh:
                bus = int(fh.read())
            with open(os.path.join(path, "devnum")) as fh:
                dev = int(fh.read())
        except (OSError, ValueError):
            continue
        index[(bus, dev)] = os.path.basename(path)
    return index


def role_of(product: str) -> str | None:
    if product in WORLD_PRODUCTS:
        return "world"
    if product in EYE_PRODUCTS:
        return "eye"
    return None


def discover(device_list) -> list[UsbCam]:
    """Annotate a libuvc device list with physical port information.

    `device_list` is what uvc.device_list() returns; it is passed in rather than
    fetched here so callers can enumerate once and reuse the result.
    """
    sysfs = _sysfs_index()
    cams = []
    for dev in device_list:
        role = role_of(dev["name"])
        if role is None:
            continue
        uid = dev["uid"]
        bus, _, addr = uid.partition(":")
        usb_path = sysfs.get((int(bus), int(addr)), "")
        root_port = usb_path.split(".")[0] if usb_path else ""
        cams.append(UsbCam(uid, dev["name"], usb_path, root_port, role))
    return sorted(cams, key=lambda c: (c.root_port, c.usb_path))


def group_by_port(cams: list[UsbCam]) -> dict[str, list[UsbCam]]:
    """Cameras grouped per front panel port -- one group per headset."""
    groups: dict[str, list[UsbCam]] = {}
    for cam in cams:
        groups.setdefault(cam.root_port, []).append(cam)
    return groups


if __name__ == "__main__":
    import uvc

    for cam in discover(uvc.device_list()):
        print(f"{cam.uid:8s} {cam.product:16s} usb={cam.usb_path:10s} "
              f"port={cam.root_port:6s} role={cam.role}")
