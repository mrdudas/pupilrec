"""CSV output, one file per sensor component.

Column layout is deliberately flat and cheap to produce: at 192 kHz (the analog
microphone) anything per-row that costs real work -- formatting a date, say --
would not keep up.  The absolute start time and every sensor parameter live in
the sidecar JSON instead, written once.
"""

from __future__ import annotations

import io
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# Per-axis names where the board's channel order is known, so a column means
# something without consulting a datasheet.
# Components written as audio instead of CSV.  See WavWriter for why.
WAV_COMPONENTS = ("imp23absu_mic", "imp34dt05_mic")

AXIS_NAMES = {
    3: ("x", "y", "z"),
    1: ("value",),
}


class ComponentWriter:
    """Appends samples of one component to its CSV."""

    def __init__(self, directory: str, stream, start_unix: float):
        self.stream = stream
        self.start_unix = start_unix
        self.path = os.path.join(directory, f"stwin_{stream.name}.csv")
        self.rows = 0

        axes = AXIS_NAMES.get(stream.dim, tuple(f"ch{i}" for i in range(stream.dim)))
        if len(axes) != stream.dim:
            axes = tuple(f"ch{i}" for i in range(stream.dim))
        self.columns = ["sample_index", "unix_time", "device_time", *axes]

        self._fh = open(self.path, "w", buffering=1 << 20)
        self._fh.write(",".join(self.columns) + "\n")

        # int-ish sensors keep 6 decimals; floats (pressure, temperature) too.
        self._fmt = ",".join(["%d", "%.6f", "%.6f"] + ["%.6f"] * stream.dim)

    def write(self, values: np.ndarray, times: np.ndarray) -> None:
        if values.shape[0] == 0:
            return
        n = values.shape[0]
        index = np.arange(self.rows, self.rows + n, dtype=np.float64)
        block = np.empty((n, 3 + values.shape[1]), dtype=np.float64)
        block[:, 0] = index
        # Device time is seconds since the board started logging; the host clock
        # at that moment anchors it to wall clock time.
        block[:, 1] = self.start_unix + times
        block[:, 2] = times
        block[:, 3:] = values

        buf = io.StringIO()
        np.savetxt(buf, block, fmt=self._fmt, delimiter=",")
        self._fh.write(buf.getvalue())
        self.rows += n

    def close(self) -> dict:
        self._fh.close()
        return {
            "component": self.stream.name,
            "file": os.path.basename(self.path),
            "rows": self.rows,
            "channels": self.stream.dim,
            "data_type": self.stream.data_type,
            "sensitivity": self.stream.sensitivity,
            "samples_per_timestamp": self.stream.samples_per_ts,
            # Non-zero means the host did not collect from the board fast
            # enough and the board's buffer wrapped; samples are missing.
            "lost_bytes": self.stream.lost_bytes,
            "bad_timestamps": self.stream.bad_timestamps,
            "transport_block_payload": self.stream.block_payload,
            "size_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }


class WavWriter:
    """Audio output for the microphones, with a companion timestamp table.

    The analog microphone runs at 192 kHz.  Writing that as CSV costs ~39 MB/s
    of text formatting, which measurably cannot keep up: the board's buffer
    wraps and samples are lost.  WAV is 100x smaller, is what any analysis tool
    wants anyway, and keeps up comfortably.

    Alignment is not lost: one row per sensor frame goes into
    `<name>_timestamps.csv`, which is enough to place any audio sample in time.
    """

    def __init__(self, directory: str, stream, start_unix: float):
        import wave

        self.stream = stream
        self.start_unix = start_unix
        self.path = os.path.join(directory, f"stwin_{stream.name}.wav")
        self.table_path = os.path.join(directory, f"stwin_{stream.name}_timestamps.csv")
        self.rows = 0

        rate = int(round(stream.odr)) or 16000
        self._wav = wave.open(self.path, "wb")
        self._wav.setnchannels(stream.dim)
        self._wav.setsampwidth(2)                  # the mics are int16
        self._wav.setframerate(rate)
        self.sample_rate = rate

        self._table = open(self.table_path, "w", buffering=1 << 16)
        self._table.write("sample_index,unix_time,device_time\n")
        self._every = max(1, stream.samples_per_ts)

    def write(self, values: np.ndarray, times: np.ndarray) -> None:
        if values.shape[0] == 0:
            return
        # values were scaled to +/-1.0 by sensitivity; go back to int16.
        raw = np.clip(values / self.stream.sensitivity, -32768, 32767).astype(np.int16)
        self._wav.writeframes(raw.tobytes())

        # One timestamp row per frame rather than per sample.
        for offset in range(0, values.shape[0], self._every):
            index = self.rows + offset
            self._table.write(
                f"{index},{self.start_unix + times[offset]:.6f},{times[offset]:.6f}\n")
        self.rows += values.shape[0]

    def close(self) -> dict:
        self._wav.close()
        self._table.close()
        return {
            "component": self.stream.name,
            "file": os.path.basename(self.path),
            "timestamps": os.path.basename(self.table_path),
            "format": "wav",
            "sample_rate": self.sample_rate,
            "rows": self.rows,
            "channels": self.stream.dim,
            "data_type": self.stream.data_type,
            "lost_bytes": self.stream.lost_bytes,
            "bad_timestamps": self.stream.bad_timestamps,
            "transport_block_payload": self.stream.block_payload,
            "size_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }


def make_writer(directory: str, stream, start_unix: float, wav_components=WAV_COMPONENTS):
    """CSV for sensors, WAV for microphones."""
    if stream.name in wav_components:
        return WavWriter(directory, stream, start_unix)
    return ComponentWriter(directory, stream, start_unix)
