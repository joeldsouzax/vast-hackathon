import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

from PIL import Image

from media import Program


def program(style="dissolve", frames=4):
    p = Program.__new__(Program)
    p.transition_style, p.transition_frames = style, frames
    p.transition, p.last_live, p.frames_written = None, None, 0
    p.source_getter = lambda slot: None
    return p


def cam(slot):
    return SimpleNamespace(path=f"/cam{slot}", slot=slot)


class TransitionTest(unittest.TestCase):
    def test_dissolve_blends_then_ends_on_new_camera(self):
        p = program()
        red, blue = Image.new("RGB", (8, 4), (255, 0, 0)), Image.new("RGB", (8, 4), (0, 0, 255))
        self.assertEqual(p._transition_image(red, cam(1), 0).getpixel((0, 0)), (255, 0, 0))
        reds = []
        for _ in range(6):
            p.frames_written += 1
            reds.append(p._transition_image(blue, cam(2), 0).getpixel((0, 0))[0])
        self.assertTrue(0 < reds[0] < 255)
        self.assertEqual(reds, sorted(reds, reverse=True))
        self.assertEqual(reds[-1], 0)
        self.assertIsNone(p.transition)

    def test_cut_style_is_immediate(self):
        p = program("cut")
        red, blue = Image.new("RGB", (8, 4), (255, 0, 0)), Image.new("RGB", (8, 4), (0, 0, 255))
        p._transition_image(red, cam(1), 0)
        p.frames_written = 1
        self.assertEqual(p._transition_image(blue, cam(2), 0).getpixel((0, 0)), (0, 0, 255))


if __name__ == "__main__":
    unittest.main()
