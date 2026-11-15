import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from storybook_engine import validate_scene_image_set


class SceneImageQualityTests(unittest.TestCase):
    def make_image(self, path, variant=0):
        image = Image.new("RGB", (1024, 1024), (20, 40, 80))
        draw = ImageDraw.Draw(image)
        for offset in range(0, 1024, 64):
            colour = ((offset + variant * 13) % 255, (offset * 2 + 40) % 255, (offset * 3 + 70) % 255)
            draw.rectangle((0, offset, 1023, min(1023, offset + 31)), fill=colour)
        image.save(path)

    def test_rejects_near_duplicate_scene_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_image(root / "scene_001.png")
            (root / "scene_001.png").replace(root / "scene_000.png")
            self.make_image(root / "scene_001.png")
            (root / "scene_001.png").write_bytes((root / "scene_000.png").read_bytes())
            with self.assertRaisesRegex(ValueError, "nearly identical"):
                validate_scene_image_set(root, expected_count=2, expected_size=(1024, 1024))

    def test_rejects_blank_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (1024, 1024), "white").save(root / "scene_001.png")
            with self.assertRaisesRegex(ValueError, "blank or nearly uniform"):
                validate_scene_image_set(root, expected_count=1, expected_size=(1024, 1024))

    def test_rejects_wrong_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_image(root / "scene_001.png")
            with self.assertRaisesRegex(ValueError, "expected"):
                validate_scene_image_set(root, expected_count=1, expected_size=(1536, 1536))


if __name__ == "__main__":
    unittest.main()
