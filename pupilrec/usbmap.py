"""Identify Pupil Core cameras by the physical USB port they are plugged into.

libuvc addresses cameras as "<bus>:<device address>", but the device address is
handed out at enumeration time and changes on every replug.  The port a headset
hangs off does not, so that is what names a headset here: each Pupil Core has an
internal hub, so its cameras all share one root port ("3-1", "3-7", ...) while
sitting on different sub-ports ("3-1.1", "3-1.4").

Two sources of that information, picked by platform: sysfs on Linux, ioreg on
macOS.  Both read from the operating system rather than from libusb, and that is
the point -- see discover_sysfs for why going through the USB library instead
would freeze the recorder.
"""

from __future__ import annotations

import glob
import os
import plistlib
import subprocess
import sys
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


def _ports_from_location(location_id: int) -> list[int]:
    """macOS locationID -> the chain of hub ports below the controller.

    The top byte is the controller (what libusb calls the bus), and each of the
    remaining nibbles is one hop down the hub tree, zero-padded at the end:
    0x14110000 is bus 0x14, port 1, then port 1 again.
    """
    ports = []
    for shift in (20, 16, 12, 8, 4, 0):
        nibble = (location_id >> shift) & 0xF
        if nibble == 0:
            break
        ports.append(nibble)
    return ports


def _ioreg_usb_nodes() -> list[dict]:
    """Every USB device macOS knows about, flattened out of the registry tree."""
    out = subprocess.run(
        ["ioreg", "-p", "IOUSB", "-l", "-a", "-w", "0"],
        capture_output=True, timeout=10,
    ).stdout
    if not out:
        return []
    tree = plistlib.loads(out)
    nodes: list[dict] = []

    def walk(entries):
        for entry in entries:
            nodes.append(entry)
            walk(entry.get("IORegistryEntryChildren", []))

    walk(tree if isinstance(tree, list) else [tree])
    return nodes


def discover_ioreg() -> list[UsbCam]:
    """The attached Pupil cameras on macOS, read out of the IO registry.

    ioreg runs as a separate process, so unlike anything that opens libusb it
    cannot contend with the cameras that are streaming -- which is the same
    property that makes sysfs the right source on Linux.

    "USB Address" is the device address libuvc puts in its uid, and the top byte
    of locationID is the bus number it pairs with, so the uid can be rebuilt
    here without asking libuvc for anything.
    """
    cams = []
    for node in _ioreg_usb_nodes():
        product = str(node.get("USB Product Name") or "").strip()
        role = role_of(product)
        if role is None:
            continue
        location = node.get("locationID")
        address = node.get("USB Address")
        if location is None or address is None:
            continue
        bus = location >> 24
        ports = _ports_from_location(location)
        if not ports:
            continue
        usb_path = f"{bus}-{'.'.join(str(p) for p in ports)}"
        cams.append(UsbCam(
            uid=f"{bus}:{address}",
            product=product,
            usb_path=usb_path,
            root_port=f"{bus}-{ports[0]}",
            role=role,
        ))
    return sorted(cams, key=lambda c: (c.root_port, c.usb_path))


def discover_sysfs() -> list[UsbCam]:
    """The attached Pupil cameras, read straight from the operating system.

    Everything needed is there, including libuvc's uid, which is just
    "busnum:devnum".  That matters: enumerating through libuvc while cameras are
    streaming can block inside the library without releasing the GIL, freezing
    the whole process.  Nothing here touches libuvc, so it is safe to call at
    any time, including from a background thread while recording.

    On macOS there is no sysfs, and the same information comes from ioreg; the
    name is kept so that callers do not have to care which platform they are on.
    """
    if sys.platform == "darwin":
        return discover_ioreg()

    cams = []
    for path in glob.glob("/sys/bus/usb/devices/*"):
        name = os.path.basename(path)
        if ":" in name or "-" not in name:      # interfaces and root hubs
            continue
        try:
            with open(os.path.join(path, "product")) as fh:
                product = fh.read().strip()
            with open(os.path.join(path, "busnum")) as fh:
                bus = int(fh.read())
            with open(os.path.join(path, "devnum")) as fh:
                dev = int(fh.read())
        except (OSError, ValueError):
            continue
        role = role_of(product)
        if role is None:
            continue
        cams.append(UsbCam(uid=f"{bus}:{dev}", product=product, usb_path=name,
                           root_port=name.split(".")[0], role=role))
    return sorted(cams, key=lambda c: (c.root_port, c.usb_path))


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
    # discover_sysfs rather than discover(uvc.device_list()): this way the
    # listing works on either platform, and without opening libusb at all.
    for cam in discover_sysfs():
        print(f"{cam.uid:8s} {cam.product:16s} usb={cam.usb_path:10s} "
              f"port={cam.root_port:6s} role={cam.role}")
