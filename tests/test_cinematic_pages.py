import tempfile
import unittest
from pathlib import Path

from PIL import Image

from storybook_engine import build_page_images, build_scene_prompt, director_camera_brief


class CinematicPageTests(unittest.TestCase):
    def test_caption_is_inside_image_on_translucent_black_panel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scenes = root / "scenes"
            scenes.mkdir()
            Image.new("RGB", (1536, 1536), (40, 120, 210)).save(scenes / "scene_001.png")
            story = {
                "scenes": [{
                    "number": 1,
                    "heading": "The Hidden Door",
                    "text": "Mira heard a soft knock behind the wall. She moved the old shelf and found a narrow door. A cold light slipped through its cracks. Aira reached for the handle, but Mira stopped her and pointed to a small wire near the lock."
                }]
            }
            pages = build_page_images(story, scenes, root / "pages", "8.5 × 8.5 in — square picture book")
            page_path = pages / "page_001.png"
            self.assertTrue(page_path.is_file())
            with Image.open(page_path) as page:
                self.assertEqual(page.size, (1536, 1536))
                top = page.getpixel((1450, 300))
                under_panel = page.getpixel((1450, 1450))
                self.assertGreater(top[2], under_panel[2])
                self.assertGreater(under_panel[2], 0)  # artwork remains visible through the panel

    def test_camera_brief_rotates_viewpoints_and_avoids_default_front_portraits(self):
        briefs = [director_camera_brief({"number": n, "characters_present": ["A", "B"]}) for n in range(1, 13)]
        self.assertEqual(len(set(briefs)), 12)
        self.assertIn("behind the listener", briefs[1])
        self.assertIn("profile", briefs[3])
        self.assertIn("360-degree", briefs[11])

    def test_scene_prompt_keeps_dialogue_eyelines_off_camera(self):
        story = {"character_registry": [], "character_bible": "", "art_direction": ""}
        prompt = build_scene_prompt(story, {
            "number": 2,
            "characters_present": ["A", "B"],
            "visual_prompt": "Two people speak in a station."
        }, "cinematic")
        self.assertIn("speaker looks at listener", prompt.lower())
        self.assertIn("Do not make characters face the viewer by default", prompt)


if __name__ == "__main__":
    unittest.main()
