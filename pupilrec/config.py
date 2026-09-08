"""Runtime configuration, persisted next to the project so it survives restarts."""

from __future__ import annotations

import json
import os
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

    host: str = "0.0.0.0"
    port: int = 8080

    def mode_for(self, role: str) -> tuple[int, int, int]:
        w, h, fps = self.modes.get(role, DEFAULT_MODES[role])
        return int(w), int(h), int(fps)

    def side_for(self, root_port: str, fallback_index: int) -> str:
        """Label for a headset, assigning a stable default the first time."""
        if root_port in self.headsets:
            return self.headsets[root_port]
        side = "left" if fallback_index == 0 else "right"
        self.headsets[root_port] = side
        return side

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
