"""Runtime configuration, persisted next to the project so it survives restarts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

# (width, height, fps), MJPEG, verified against the hardware.
#
# The eye cameras run at their maximum.  The world cameras trade resolution for
# frame rate: they offer either 1920x1080 at 30 fps or 1280x720 at 60 fps, and
# 60 fps is the more useful of the two here.  It is also cheaper -- 720p frames
# are less than half the size, so twice as many of them still cost slightly less
# bandwidth and disk than 1080p30 (15.7 vs 16.6 MB/s across all four cameras).
DEFAULT_MODES = {
    "world": (1280, 720, 60),
    "eye": (400, 400, 120),
}


@dataclass
class Config:
    # Front panel port -> the side label shown in the UI.  Ports are sysfs root
    # port names ("3-1", "3-7"); whichever headset is plugged into that port
    # takes the label, which is exactly the "left or right socket" rule.
    headsets: dict[str, str] = field(default_factory=dict)

    recordings_dir: str = os.path.join(PROJECT_ROOT, "recordings")
    # Where the GNSS logger writes its daily files; the map page reads them.
    gps_dir: str = os.path.join(PROJECT_ROOT, "gps")
    # Map tiles are cached here so the tablet needs no internet of its own.
    tiles_dir: str = os.path.join(PROJECT_ROOT, "tiles")

    # Passed to libuvc when starting a stream.  It scales the isochronous
    # bandwidth the driver reserves; the kernel's uvcvideo driver always asks
    # for the maximum, which is why two cameras of one headset cannot stream
    # under V4L2 at all.  2.0 is pyuvc's default and measured stable here.
    bandwidth_factor: float = 2.0

    modes: dict[str, list[int]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_MODES.items()}
    )

    # Preview is throttled independently of capture: recording always gets every
    # frame, the browser only gets this many per second.
    preview_fps: dict[str, float] = field(
        default_factory=lambda: {"world": 10.0, "eye": 10.0}
    )

    # And shrunk independently of capture, per role, before frames leave the
    # host.  The world cameras are what makes a four-camera preview too much for
    # the tablet's Wi-Fi: ~1 MB/s each at 720p against ~0.2 MB/s for an eye.
    # Half sides measure at about a tenth of that -- 0.1 MB/s against 1.0 -- and
    # still show the operator what the camera is pointed at.  1.0 sends the
    # camera's own JPEG untouched, which is what the already small eye frames
    # want.  Recording is unaffected either way.
    preview_scale: dict[str, float] = field(
        default_factory=lambda: {"world": 0.5, "eye": 1.0}
    )

    # Image controls (brightness, gain, exposure, ...) per camera id, as
    # {"left_world": {"Brightness": 32, ...}}.  The value really does live in
    # the camera -- but only until it loses power, and a headset loses power on
    # every replug and every reboot.  So what the operator sets is kept here and
    # written back into the camera each time it is opened; this file is what
    # makes a setting outlast the cable being pulled.
    camera_controls: dict[str, dict[str, int]] = field(default_factory=dict)

    host: str = "0.0.0.0"
    port: int = 8080

    def controls_for(self, cam_id: str) -> dict[str, int]:
        """The stored control values for one camera, keyed by control name."""
        return dict(self.camera_controls.get(cam_id, {}))

    def remember_control(self, cam_id: str, name: str, value: int) -> None:
        self.camera_controls.setdefault(cam_id, {})[name] = int(value)

    def forget_controls(self, cam_id: str) -> None:
        """Drop the stored values so the camera keeps its own defaults."""
        self.camera_controls.pop(cam_id, None)

    def scale_for(self, role: str) -> float:
        """How much of its own size a role's preview frames keep.

        Clamped rather than trusted: this comes from a hand-edited file, and a
        misplaced decimal point should cost some bandwidth, not hand the tablet
        a one-pixel image or a preview larger than the camera can produce.
        """
        try:
            factor = float(self.preview_scale.get(role, 1.0))
        except (TypeError, ValueError):
            return 1.0
        return min(1.0, max(0.1, factor))

    def mode_for(self, role: str) -> tuple[int, int, int]:
        w, h, fps = self.modes.get(role, DEFAULT_MODES[role])
        return int(w), int(h), int(fps)

    def side_for(self, root_port: str, fallback_index: int) -> str:
        """Label for a headset, assigning a stable default the first time.

        Two headsets get "left" and "right"; further ones are numbered, because
        a third front port has no side to be on.  Any of them can be renamed in
        config.json -- the label is what names the files.
        """
        if root_port in self.headsets:
            return self.headsets[root_port]
        default = ("left", "right")
        side = (default[fallback_index] if fallback_index < len(default)
                else f"unit{fallback_index + 1}")
        # Never hand out a label already taken by another port.
        taken = set(self.headsets.values())
        if side in taken:
            # Count past every headset already known, not just past this one's
            # position: a third headset discovered after the other two were
            # labelled would otherwise be offered "unit2", which reads as the
            # second of something and belongs to nothing.
            n = max(fallback_index + 1, len(taken) + 1)
            while f"unit{n}" in taken:
                n += 1
            side = f"unit{n}"
        self.headsets[root_port] = side
        return side

    def forget_absent_headsets(self, attached_ports) -> list[str]:
        """Drop generated labels for ports with nothing plugged into them.

        A headset moved to another socket arrives as a new port and takes a new
        label, leaving the old one behind pointing at an empty one.  Do that
        three times while hunting for a USB port with bandwidth to spare -- as
        happened here -- and the third headset is called "unit4", which names
        nothing.

        Only generated "unitN" labels are dropped, and only when no camera
        setting is stored under them.  A name someone chose, and a headset whose
        image controls are remembered, are kept while unplugged: outlasting a
        pulled cable is what this file is for.
        """
        attached = set(attached_ports)
        dropped = []
        for port, side in sorted(self.headsets.items()):
            if port in attached or not re.fullmatch(r"unit\d+", side):
                continue
            if any(key.startswith(f"{side}_") for key in self.camera_controls):
                continue
            del self.headsets[port]
            dropped.append(f"{port} ({side})")
        return dropped

    def save(self, path: str = CONFIG_PATH) -> None:
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=2)
            fh.write("\n")

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "Config":
        cfg = cls()
        try:
            with open(path) as fh:
                stored = json.load(fh)
        except FileNotFoundError:
            return cfg
        for key, value in stored.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg
