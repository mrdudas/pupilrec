"""Shrinking preview frames: the size that goes out, and the ways it must not.

The interesting parts are not the arithmetic but the promises around it -- that
a frame is scaled once no matter how many viewers want it, that a frame which
cannot be decoded still reaches the browser, and that a host without Pillow
shows a full-size preview rather than no preview.
"""

import io
import unittest
from unittest import mock

from pupilrec.config import Config
from pupilrec.preview import PreviewScaler

try:
    from PIL import Image
except ImportError:                                 # pragma: no cover
    Image = None


def a_jpeg(width: int, height: int) -> bytes:
    """A JPEG with enough detail that it does not compress away to nothing."""
    img = Image.new("RGB", (width, height))
    for x in range(0, width, 2):
        for y in range(0, height, 2):
            img.putpixel((x, y), ((x * 7) % 256, (y * 5) % 256, (x + y) % 256))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def size_of(jpeg: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(jpeg)).size


@unittest.skipIf(Image is None, "Pillow is not installed")
class ScalingTest(unittest.TestCase):
    def test_world_frame_is_halved(self):
        scaler = PreviewScaler(0.5)
        self.assertEqual(size_of(scaler.scaled(1, a_jpeg(1280, 720))), (640, 360))

    def test_halving_saves_most_of_the_bytes(self):
        """The point of the exercise: a quarter of the pixels, far fewer bytes."""
        original = a_jpeg(1280, 720)
        smaller = PreviewScaler(0.5).scaled(1, original)
        self.assertLess(len(smaller), len(original) * 0.6)

    def test_factor_one_returns_the_very_same_object(self):
        """Not merely equal bytes: an untouched role must not be decoded at all."""
        original = a_jpeg(400, 400)
        scaler = PreviewScaler(1.0)
        self.assertFalse(scaler.active)
        self.assertIs(scaler.scaled(1, original), original)

    def test_a_frame_is_scaled_once_however_many_viewers_ask(self):
        original = a_jpeg(1280, 720)
        scaler = PreviewScaler(0.5)
        with mock.patch.object(PreviewScaler, "_shrink",
                               side_effect=lambda jpeg: b"shrunk") as shrink:
            first = scaler.scaled(7, original)
            second = scaler.scaled(7, original)
            self.assertEqual(shrink.call_count, 1)     # second viewer, same frame
            scaler.scaled(8, original)
            self.assertEqual(shrink.call_count, 2)     # next frame, scaled again
        self.assertEqual(first, second)

    def test_an_odd_size_still_shrinks(self):
        """Nothing may assume the factor divides the frame evenly."""
        self.assertEqual(size_of(PreviewScaler(0.4).scaled(1, a_jpeg(401, 399))),
                         (160, 160))


class FallbackTest(unittest.TestCase):
    """A preview that cannot be shrunk is worth more than no preview."""

    def test_a_corrupt_frame_goes_out_whole(self):
        broken = b"\xff\xd8\xff\xe0 not really a jpeg"
        scaler = PreviewScaler(0.5)
        with self.assertLogs("pupilrec.preview", level="WARNING"):
            self.assertIs(scaler.scaled(1, broken), broken)

    def test_the_complaint_is_logged_once_not_once_per_frame(self):
        broken = b"\xff\xd8 still not a jpeg"
        scaler = PreviewScaler(0.5)
        with self.assertLogs("pupilrec.preview", level="WARNING") as logs:
            for seq in range(20):
                scaler.scaled(seq, broken)
        self.assertEqual(len(logs.output), 1)

    def test_without_pillow_frames_go_out_full_size(self):
        original = b"\xff\xd8\xff\xe0 pretend frame"
        scaler = PreviewScaler(0.5)
        real_import = __import__

        def no_pillow(name, *args, **kwargs):
            if name.startswith("PIL"):
                raise ImportError("no module named PIL")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=no_pillow):
            with self.assertLogs("pupilrec.preview", level="WARNING") as logs:
                self.assertIs(scaler.scaled(1, original), original)
        self.assertIn("Pillow", logs.output[0])


class ConfiguredScaleTest(unittest.TestCase):
    def test_defaults_halve_the_world_and_leave_the_eyes_alone(self):
        cfg = Config()
        self.assertEqual(cfg.scale_for("world"), 0.5)
        self.assertEqual(cfg.scale_for("eye"), 1.0)

    def test_an_unknown_role_is_left_alone(self):
        self.assertEqual(Config().scale_for("thermal"), 1.0)

    def test_nonsense_in_the_file_does_not_reach_a_camera(self):
        cfg = Config()
        for value, expected in [(0.0, 0.1), (-3, 0.1), (0.0001, 0.1),
                                (4.0, 1.0), ("half", 1.0), (None, 1.0)]:
            cfg.preview_scale = {"world": value}
            self.assertEqual(cfg.scale_for("world"), expected, f"for {value!r}")


if __name__ == "__main__":
    unittest.main()
