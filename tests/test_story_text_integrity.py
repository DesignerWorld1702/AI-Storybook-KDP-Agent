import unittest
from unittest.mock import patch

import storybook_engine as engine


class StoryTextIntegrityTests(unittest.TestCase):
    def test_scene_text_repair_uses_neutral_bridge_after_model_undershoots(self):
        candidate = (
            "Mira checks the map beside the gate. The guard points toward the tower. "
            "Their team follows the path and listens. A light shines beyond trees, "
            "but no one can see its source."
        )
        self.assertEqual(len(candidate.split()), 32)
        with patch.object(engine, "cf_text", return_value=candidate):
            scene = {"heading": "The tower", "text": "short"}
            result = engine.repair_scene_page_text(scene, {"title": "Test", "character_registry": []}, 2)
        self.assertGreaterEqual(len(result["text"].split()), 40)
        self.assertLessEqual(len(result["text"].split()), 55)
        self.assertIn(candidate, result["text"])

    def test_detects_repeated_full_sentence_across_pages(self):
        scenes = [
            {"number": 1, "heading": "A new clue", "text": "The guard opened the old door slowly. A bright lantern warmed the room. A silver cup sat near the window."},
            {"number": 2, "heading": "A second clue", "text": "The guard opened the old door slowly. Three birds flew above the river. A farmer waved beside the path."},
        ]
        with self.assertRaisesRegex(ValueError, "Repeated page text"):
            engine.validate_story_text_integrity(scenes)

    def test_detects_unwanted_arabic_script(self):
        scenes = [{"number": 8, "heading": "Night", "text": "The door opens. مرحبا appears on the wall."}]
        with self.assertRaisesRegex(ValueError, "Arabic"):
            engine.validate_story_text_integrity(scenes)

    def test_detects_unwanted_hangul_script(self):
        scenes = [{"number": 9, "heading": "Night", "text": "A note says 안녕하세요 beside the door."}]
        with self.assertRaisesRegex(ValueError, "Hangul"):
            engine.validate_story_text_integrity(scenes)

    def test_detects_near_duplicate_page_paragraphs(self):
        first = "Maya walks beside the river and checks her folded map. She notices a red mark near the old bridge. The team follows the path and finds a locked gate."
        second = "Maya walks beside the river and checks the map. A red mark appears near the old bridge. The team follows the path and finds a closed gate."
        with self.assertRaisesRegex(ValueError, "Near-duplicate"):
            engine.validate_story_text_integrity([
                {"number": 1, "heading": "River", "text": first},
                {"number": 2, "heading": "Bridge", "text": second},
            ])

    def test_accepts_distinct_english_pages(self):
        scenes = [
            {"number": 1, "heading": "The station", "text": "Maya studies the train schedule. A blue ticket rests beside the window."},
            {"number": 2, "heading": "The market", "text": "Jon buys a fresh apple. He hears a bell ring across the busy street."},
        ]
        report = engine.validate_story_text_integrity(scenes)
        self.assertTrue(report["passed"])
        self.assertEqual(report["pages_checked"], 2)


if __name__ == "__main__":
    unittest.main()
