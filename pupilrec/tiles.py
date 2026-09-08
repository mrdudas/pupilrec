"""A caching map tile proxy, so the iPad needs no internet of its own.

Tiles are fetched from OpenStreetMap the first time they are looked at, stored
on disk, and served from there forever after.  The recording box therefore needs
a connection only while exploring new area; the tablet never does.

Deliberately on demand only.  The OSM tile usage policy forbids bulk
downloading, so nothing here pre-fetches an area -- a tile is retrieved because
somebody actually looked at it, and repeated viewing costs nothing further.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib

logger = logging.getLogger(__name__)

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# The policy requires an identifying agent; a generic one gets blocked.
USER_AGENT = "pupilrec-map/1.0 (local recording rig; contact: operator)"
FETCH_TIMEOUT_S = 10.0
MAX_ZOOM = 19


def _placeholder() -> bytes:
    """A flat grey 256x256 PNG, shown where a tile is not cached and offline."""
    width = height = 256
    raw = b"".join(b"\x00" + bytes([0x2b, 0x2f, 0x38]) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


PLACEHOLDER_PNG = _placeholder()


class TileCache:
    def __init__(self, directory: str, url_template: str = TILE_URL):
        self.directory = directory
        self.url_template = url_template
        self.hits = 0
        self.fetches = 0
        self.failures = 0
        self.last_error = ""
        # One in-flight fetch per tile: a map pan asks for the same tile from
        # several connections at once, and one request upstream is enough.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _path(self, z: int, x: int, y: int) -> str:
        return os.path.join(self.directory, str(z), str(x), f"{y}.png")

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    @staticmethod
    def valid(z: int, x: int, y: int) -> bool:
        if not (0 <= z <= MAX_ZOOM):
            return False
        span = 1 << z
        return 0 <= x < span and 0 <= y < span

    def get(self, z: int, x: int, y: int):
        """-> (png bytes, source) where source is "cache", "network" or "missing"."""
        if not self.valid(z, x, y):
            return None, "invalid"
        path = self._path(z, x, y)
        try:
            with open(path, "rb") as fh:
                self.hits += 1
                return fh.read(), "cache"
        except OSError:
            pass

        with self._lock_for(f"{z}/{x}/{y}"):
            try:                                # another thread may have won
                with open(path, "rb") as fh:
                    self.hits += 1
                    return fh.read(), "cache"
            except OSError:
                pass
            url = self.url_template.format(z=z, x=x, y=y)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
                    body = response.read()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                return PLACEHOLDER_PNG, "missing"

            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(body)
                os.replace(tmp, path)
            except OSError:
                logger.exception("could not cache tile %s", path)
            self.fetches += 1
            return body, "network"

    def stats(self) -> dict:
        tiles = 0
        size = 0
        for root, _dirs, files in os.walk(self.directory):
            for name in files:
                if name.endswith(".png"):
                    tiles += 1
                    try:
                        size += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        return {
            "cached_tiles": tiles,
            "cache_bytes": size,
            "hits": self.hits,
            "fetched": self.fetches,
            "failures": self.failures,
            "last_error": self.last_error,
        }
