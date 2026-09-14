"""Shrinking preview frames so the tablet's Wi-Fi need not carry full capture.

The cameras produce MJPEG and the server used to ship those bytes untouched,
which is the cheapest thing it could do -- but a 720p world frame is about
100 KB, so one viewer on one world camera already costs 1 MB/s at 10 fps, and
all four cameras together ask about 2.4 MB/s of a link the tablet shares with
everything else in the room.

Halving each side costs a decode and a re-encode, and measured against these
cameras it takes a 115 KB frame down to 12 KB -- a ninth, not the quarter that
pixel count alone would suggest.  Two things add up: a quarter of the pixels,
and the camera's own encoder being far more generous with bytes than a preview
needs.  That is also why the number is worth measuring rather than deriving.

The decode is cheaper than it looks (3.5 ms for a 720p frame here): libjpeg can
scale while decoding, which Pillow calls draft mode, so at 1/2 it skips most of
the inverse DCT rather than handing us pixels we would throw away.

This happens on the HTTP side and never in a capture thread.  A capture thread
holds the GIL inside libuvc, so work added there is work the freeze watchdog
counts against the whole process.  Recording is untouched either way -- the
sink is fed from the capture thread with the camera's own bytes.
"""

from __future__ import annotations

import io
import logging
import threading

logger = logging.getLogger(__name__)

# Judged on the halved world frames: at 85 they are indistinguishable from the
# camera's own JPEG at tablet viewing distance while costing a ninth of it.
# Higher gives back the savings, lower shows on the eye cameras, whose pupil
# edge is the one thing an operator actually looks at.
JPEG_QUALITY = 85


def brightness_of(jpeg: bytes, window: float = 0.5) -> float:
    """Mean brightness of the middle of a frame, 0-255.  -1 if it cannot be read.

    The middle, not the whole frame, because that is where the subject is: an
    eye camera's corners see eyelid and skin at a different exposure, and a
    number that averages them says less about the picture than one that does
    not.  Pupil's own auto exposure works the same way, on a weighted centre
    window, and aims for 90 to 150 -- which is what makes this worth showing to
    an operator who otherwise has only their own eyes on a tablet in daylight.

    Cheap on purpose: libjpeg is asked for a small greyscale decode (draft does
    both in one pass), so this costs a fraction of a millisecond and can be
    sampled once a second per camera without competing with capture.
    """
    if not jpeg:
        return -1.0
    try:
        from PIL import Image
    except ImportError:
        return -1.0
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.draft("L", (64, 64))
        img = img.convert("L")
        width, height = img.size
        box_w = max(1, int(width * window))
        box_h = max(1, int(height * window))
        left = (width - box_w) // 2
        top = (height - box_h) // 2
        middle = img.crop((left, top, left + box_w, top + box_h))
        counts = middle.histogram()
        pixels = sum(counts)
        if not pixels:
            return -1.0
        return sum(level * n for level, n in enumerate(counts)) / pixels
    except Exception:
        # A frame too corrupt to measure is not worth a log line every second.
        return -1.0


class PreviewScaler:
    """Shrinks one camera's frames, once per frame however many viewers watch.

    Viewers of the same camera ask for the same frame within milliseconds of
    each other, so the result is kept until a newer frame replaces it.  The
    lock is deliberately held across the scaling: the second viewer waits the
    few milliseconds it takes and then gets the cached bytes, which is less
    work than both of them scaling the same frame.
    """

    def __init__(self, factor: float, quality: int = JPEG_QUALITY):
        self.factor = float(factor)
        self.quality = int(quality)
        self._lock = threading.Lock()
        self._seq = -1
        self._jpeg = b""
        self._warned = False

    @property
    def active(self) -> bool:
        """False when frames go out as they arrived, so nothing is decoded."""
        return 0.0 < self.factor < 1.0

    def scaled(self, seq: int, jpeg: bytes) -> bytes:
        """The frame numbered `seq`, shrunk -- or as it came, if it cannot be."""
        if not self.active or not jpeg:
            return jpeg
        with self._lock:
            if seq != self._seq or not self._jpeg:
                self._jpeg = self._shrink(jpeg)
                self._seq = seq
            return self._jpeg

    def _shrink(self, jpeg: bytes) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            self._warn("Pillow is missing; preview frames go out at full size")
            return jpeg
        try:
            img = Image.open(io.BytesIO(jpeg))
            width = max(1, round(img.width * self.factor))
            height = max(1, round(img.height * self.factor))
            # Hands the scaling to libjpeg where it can be done during the
            # decode.  It only halves, so at factor 0.5 this is the whole job
            # and the resize below never runs.
            img.draft(img.mode, (width, height))
            if img.size != (width, height):
                img = img.resize((width, height), Image.BILINEAR)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=self.quality)
            return out.getvalue()
        except Exception as exc:
            # A corrupt frame reaches us now and then -- the capture log has the
            # JPEG header warnings to prove it -- and must cost one frame's
            # quality, not the preview.
            self._warn(f"cannot shrink a preview frame ({exc}); sending it whole")
            return jpeg

    def _warn(self, message: str) -> None:
        """Complain once: this runs per frame, and the cause does not change."""
        if not self._warned:
            self._warned = True
            logger.warning("%s", message)
