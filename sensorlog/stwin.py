"""Talking to a STEVAL-STWINKT1B running FP-SNS-DATALOG2 firmware.

The board is a vendor-class USB device speaking ST's PnPL protocol.  ST ships a
prebuilt shared library for it; this is a thin ctypes layer over that, because
the alternative -- reimplementing the USB framing -- buys nothing here.

Note it must be the *v2* library.  The v1 one hardcodes USB product id 0x5743
and will report zero devices for this board, which reports 0x5744.  See
docs/STWIN-SENSORS.md.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os

logger = logging.getLogger(__name__)

OK = 0
INTERFACE_SD = 0
INTERFACE_USB = 1

DEFAULT_LIB_PATHS = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "vendor", "libhs_datalog_v2.so"),
    "/usr/local/lib/libhs_datalog_v2.so",
)


class StwinError(RuntimeError):
    pass


class Stwin:
    """One connected board.  Not thread safe; drive it from a single thread."""

    def __init__(self, lib_path: str = ""):
        self._lib = ctypes.CDLL(lib_path or self._find_library())
        self._bind()
        self._open = False
        self.device_id = 0

    @staticmethod
    def _find_library() -> str:
        for path in DEFAULT_LIB_PATHS:
            if os.path.exists(path):
                return path
        raise StwinError(
            "libhs_datalog_v2.so not found. Run setup/install.sh, which fetches "
            "it from STMicroelectronics/fp-sns-datalog1."
        )

    def _bind(self) -> None:
        lib, c_int, c_char_p = self._lib, ctypes.c_int, ctypes.c_char_p
        P = ctypes.POINTER
        sigs = {
            "hs_datalog_open": ([], c_int),
            "hs_datalog_close": ([], c_int),
            "hs_datalog_free": ([c_char_p], c_int),
            "hs_datalog_get_version": ([P(c_char_p)], c_int),
            "hs_datalog_get_device_number": ([P(c_int)], c_int),
            "hs_datalog_get_device_status": ([c_int, P(c_char_p)], c_int),
            "hs_datalog_send_message": ([c_int, c_char_p, c_int, P(c_int), P(c_char_p)], c_int),
            "hs_datalog_start_log": ([c_int, c_int, P(c_char_p)], c_int),
            "hs_datalog_stop_log": ([c_int, P(c_char_p)], c_int),
            "hs_datalog_get_available_data_size": ([c_int, c_char_p, P(c_int)], c_int),
            "hs_datalog_get_data": ([c_int, c_char_p, P(ctypes.c_uint8), c_int, P(c_int)], c_int),
        }
        for name, (argtypes, restype) in sigs.items():
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = argtypes, restype

    # -- string helpers ---------------------------------------------------

    def _call_str(self, fn, *args) -> str:
        """Call a function whose last argument is a char** the library owns.

        The library is inconsistent about success: some of these return 0 and
        others return the length of the string they produced.  Only a negative
        value means failure (ST_HS_DATALOG_ERROR is -1).
        """
        out = ctypes.c_char_p()
        if fn(*args, ctypes.byref(out)) < 0:
            raise StwinError(f"{fn.__name__} failed")
        text = out.value.decode() if out.value else ""
        if out.value:
            self._lib.hs_datalog_free(out)
        return text

    # -- lifecycle --------------------------------------------------------

    def open(self) -> None:
        if self._lib.hs_datalog_open() != OK:
            raise StwinError("hs_datalog_open failed")
        self._open = True
        count = ctypes.c_int(0)
        if self._lib.hs_datalog_get_device_number(ctypes.byref(count)) != OK:
            raise StwinError("hs_datalog_get_device_number failed")
        if count.value < 1:
            self.close()
            raise StwinError(
                "No STWIN board found. Check the USB cable and that udev rule "
                "71-stwin-datalog.rules is installed."
            )
        logger.info("STWIN connected (%s, %d device(s))", self.version(), count.value)

    def close(self) -> None:
        if self._open:
            self._lib.hs_datalog_close()
            self._open = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- state ------------------------------------------------------------

    def version(self) -> str:
        return self._call_str(self._lib.hs_datalog_get_version)

    def device_status(self) -> dict:
        return json.loads(self._call_str(self._lib.hs_datalog_get_device_status, self.device_id))

    def send(self, message: dict) -> str:
        """Send a raw PnPL message and return the board's reply."""
        payload = json.dumps(message).encode()
        size = ctypes.c_int(0)
        out = ctypes.c_char_p()
        rc = self._lib.hs_datalog_send_message(
            self.device_id, payload, len(payload), ctypes.byref(size), ctypes.byref(out))
        if rc < 0:
            raise StwinError(f"send_message failed for {message}")
        text = out.value.decode() if out.value else ""
        if out.value:
            self._lib.hs_datalog_free(out)
        return text

    def set_property(self, component: str, prop: str, value) -> str:
        return self.send({component: {prop: value}})

    # -- logging ----------------------------------------------------------

    def start_log(self, interface: int = INTERFACE_USB) -> str:
        return self._call_str(self._lib.hs_datalog_start_log, self.device_id, interface)

    def stop_log(self) -> str:
        return self._call_str(self._lib.hs_datalog_stop_log, self.device_id)

    def available(self, component: str) -> int:
        size = ctypes.c_int(0)
        if self._lib.hs_datalog_get_available_data_size(
                self.device_id, component.encode(), ctypes.byref(size)) != OK:
            raise StwinError(f"available_data_size failed for {component}")
        return size.value

    def read(self, component: str, size: int) -> bytes:
        """Read up to `size` bytes of streamed data for one component."""
        if size <= 0:
            return b""
        buf = (ctypes.c_uint8 * size)()
        actual = ctypes.c_int(0)
        if self._lib.hs_datalog_get_data(
                self.device_id, component.encode(), buf, size, ctypes.byref(actual)) != OK:
            raise StwinError(f"get_data failed for {component}")
        return bytes(bytearray(buf)[:actual.value])
