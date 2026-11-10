import tempfile
import unittest
from pathlib import Path

from storybook_engine import (
    build_scene_prompt,
    character_reference_path,
    scene_reference_images,
)


class CharacterContinuityTests(unittest.TestCase):
    def setUp(self):
        self.story = {
            "art_direction": "misty mountain city, cool blue palette",
            "character_bible": "Aira: fixed auburn hair and green eyes",
            "character_registry": [
                {
                    "name": "Aira",
                    "role": "lead investigator",
                    "importance": "protagonist",
                    "visual_bible": "25, long espresso hair, hazel-green eyes, burgundy jacket",
                },
                {
                    "name": "Mira",
                    "role": "local guide",
                    "importance": "supporting",
                    "visual_bible": "31, short copper hair, grey eyes, mustard coat",
                },
                {
                    "name": "The Warden",
                    "role": "principal villain",
                    "importance": "villain",
                    "visual_bible": "tall, pale eyes, story-specific dark ceremonial coat and silver mask",
                },
                {
                    "name": "Guard",
                    "role": "one-scene guard",
                    "importance": "supporting",
                    "visual_bible": "broad build, shaved head, rust uniform",
                },
            ],
            "scenes": [],
        }

    def test_prompt_includes_exact_scene_character_bibles(self):
        scene = {
            "number": 7,
            "characters_present": ["Aira", "Mira", "The Warden"],
            "visual_prompt": "They confront each other in the archive.",
        }
        prompt = build_scene_prompt(self.story, scene, "cinematic anime realism")
        self.assertIn("hazel-green eyes, burgundy jacket", prompt)
        self.assertIn("short copper hair, grey eyes, mustard coat", prompt)
        self.assertIn("silver mask", prompt)
        self.assertIn("Do not merge identities", prompt)

    def test_scene_references_include_recurring_supporting_cast_and_villain(self):
        with tempfile.TemporaryDirectory() as tmp:
            style_ref = Path(tmp) / "style.png"
            scene = {"characters_present": ["Aira", "Mira", "The Warden", "Guard"]}
            for name in ("Aira", "Mira", "The Warden"):
                path = character_reference_path(tmp, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"portrait")
            refs = scene_reference_images(self.story, scene, Path(tmp), style_ref)
            self.assertEqual(refs[0], style_ref)
            self.assertIn(character_reference_path(tmp, "Mira"), refs)
            self.assertIn(character_reference_path(tmp, "The Warden"), refs)
            self.assertNotIn(character_reference_path(tmp, "Guard"), refs)


if __name__ == "__main__":
    unittest.main()
