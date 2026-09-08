"""Turning the board's raw USB stream into samples with timestamps.

The stream has two layers, and both had to be established by inspection --
neither is described in ST's documentation for this firmware.

1. Transport blocks. `hs_datalog_get_data` returns a run of fixed-size blocks,
   each `[uint32 counter][payload]`, where the counter is the running total of
   payload bytes delivered so far. The payload size is constant per component
   (40 bytes for the pressure sensor, 7000 for the vibration one) and is learned
   from the first block. Because the counter is cumulative it also detects data
   the host failed to collect in time.

2. Sensor frames, once the block headers are stripped: `samples_per_ts` samples
   of `dim` channels, followed by one double holding the device time at the *end*
   of that frame. Times inside a frame are linearly interpolated, which is what
   ST's own tooling does. Frames are not aligned to blocks, so the two layers
   are buffered separately.
"""

from __future__ import annotations

import struct

import numpy as np

DTYPES = {
    "int8": np.int8, "uint8": np.uint8,
    "int16": np.int16, "uint16": np.uint16,
    "int32": np.int32, "uint32": np.uint32,
    "float": np.float32, "double": np.float64,
}
TIMESTAMP_BYTES = 8      # one double per frame
BLOCK_HEADER_BYTES = 4   # uint32 cumulative payload counter
MAX_PLAUSIBLE_GAP = 1 << 26   # 64 MB; larger means a counter reset, not loss


class ComponentStream:
    """Incremental parser for one sensor component."""

    def __init__(self, name: str, status: dict):
        self.name = name
        self.dim = int(status.get("dim", 1))
        self.data_type = status.get("data_type", "int16")
        self.dtype = DTYPES.get(self.data_type, np.int16)
        self.itemsize = np.dtype(self.dtype).itemsize
        self.samples_per_ts = int(status.get("samples_per_ts", 0) or 0)
        self.sensitivity = float(status.get("sensitivity", 1) or 1)
        self.odr = float(status.get("measodr") or status.get("odr") or 0)

        self.has_timestamps = self.samples_per_ts > 0
        if self.has_timestamps:
            self.data_bytes = self.samples_per_ts * self.dim * self.itemsize
            self.frame_bytes = self.data_bytes + TIMESTAMP_BYTES
        else:
            self.data_bytes = self.dim * self.itemsize
            self.frame_bytes = self.data_bytes

        self.block_payload = 0        # learned from the first block
        self.lost_bytes = 0           # gaps seen in the block counter
        self.bad_timestamps = 0       # frames whose stamp had to be synthesised
        self.resyncs = 0              # block counter jumps that were not data loss
        self.samples_seen = 0
        self._last_delta = 0.0        # last believable gap between frame stamps

        self._raw = b""               # bytes of an incomplete transport block
        self._payload = b""           # deframed bytes of an incomplete frame
        self._counter = None
        self._prev_ts = float(status.get("ioffset", 0) or 0)

    @property
    def frame_period(self) -> float:
        if self.has_timestamps and self.odr > 0:
            return self.samples_per_ts / self.odr
        return 1.0 / self.odr if self.odr > 0 else 0.0

    # -- layer 1: transport blocks ---------------------------------------

    def _deframe(self, raw: bytes) -> bytes:
        """Strip block headers, tracking the counter for lost data."""
        self._raw += raw
        out = bytearray()

        while True:
            if self.block_payload == 0:
                if len(self._raw) < BLOCK_HEADER_BYTES:
                    break
                # The first counter equals one payload's worth of bytes.
                self.block_payload = struct.unpack_from("<I", self._raw, 0)[0]
                if not (0 < self.block_payload <= 1 << 20):
                    self.block_payload = 0
                    self._raw = b""        # unusable; wait for a clean start
                    break

            block = BLOCK_HEADER_BYTES + self.block_payload
            if len(self._raw) < block:
                break

            counter = struct.unpack_from("<I", self._raw, 0)[0]
            if self._counter is not None:
                expected = self._counter + self.block_payload
                gap = counter - expected
                if 0 < gap <= MAX_PLAUSIBLE_GAP:
                    # The board kept producing while we were not reading.
                    self.lost_bytes += gap
                elif gap != 0:
                    # Either the counter restarted (a new log session) or we are
                    # reading a block boundary that is not really one.  Neither
                    # is lost data, so resynchronise instead of inventing a
                    # number: a stale byte in the buffer once produced a
                    # "660 GB lost" report this way.
                    self.resyncs += 1
            self._counter = counter

            out += self._raw[BLOCK_HEADER_BYTES:block]
            self._raw = self._raw[block:]

        return bytes(out)

    def _frame_time(self, end_ts: float) -> float:
        """Validate one frame's timestamp, synthesising it when the board's is not usable.

        The 192 kHz microphone emits zeroed and NaN stamps on the frames it
        flushes while the log is being stopped.  Rather than let those corrupt
        the table, they are replaced by continuing at the last believable rate --
        the same recovery ST's own tooling performs.
        """
        delta = end_ts - self._prev_ts
        plausible = (
            np.isfinite(end_ts)
            and delta > 0
            and (self._last_delta == 0.0 or 0.25 * self._last_delta < delta < 4 * self._last_delta)
        )
        if plausible:
            self._last_delta = delta
            return end_ts

        self.bad_timestamps += 1
        step = self._last_delta or self.frame_period
        return self._prev_ts + step

    # -- layer 2: sensor frames ------------------------------------------

    def feed(self, raw: bytes):
        """Consume bytes. -> (values [n, dim] float64, device_times [n] float64)."""
        self._payload += self._deframe(raw)

        n_frames = len(self._payload) // self.frame_bytes
        if n_frames == 0:
            return np.empty((0, self.dim)), np.empty(0)

        used = n_frames * self.frame_bytes
        block, self._payload = self._payload[:used], self._payload[used:]

        per_frame = self.samples_per_ts if self.has_timestamps else 1
        values = np.empty((n_frames * per_frame, self.dim), dtype=np.float64)
        times = np.empty(n_frames * per_frame, dtype=np.float64)

        for i in range(n_frames):
            start = i * self.frame_bytes
            samples = np.frombuffer(
                block[start:start + self.data_bytes], dtype=self.dtype
            ).reshape(-1, self.dim)
            values[i * per_frame:(i + 1) * per_frame] = samples

            if self.has_timestamps:
                end_ts = self._frame_time(
                    struct.unpack_from("<d", block, start + self.data_bytes)[0])
                # The first frame starts at the board's initial offset; every
                # later one starts where the previous ended.  No sample rate is
                # needed anywhere -- the stream carries its own time.
                times[i * per_frame:(i + 1) * per_frame] = np.linspace(
                    self._prev_ts, end_ts, per_frame, endpoint=False)
                self._prev_ts = end_ts
            else:
                self._prev_ts += self.frame_period
                times[i] = self._prev_ts

        values *= self.sensitivity
        self.samples_seen += values.shape[0]
        return values, times
