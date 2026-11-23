import unittest

import storybook_engine as engine


class DialogueUniquenessTests(unittest.TestCase):
    def test_rejects_exact_repetition_between_scenes(self):
        scenes = [
            {"number": 1, "dialogue": [{"speaker": "A", "line": "We must go now!"}]},
            {"number": 9, "dialogue": [{"speaker": "B", "line": "We must go now!"}]},
        ]
        with self.assertRaisesRegex(ValueError, "Repeated dialogue rejected"):
            engine.validate_unique_story_dialogue(scenes)

    def test_rejects_case_and_punctuation_variants(self):
        scenes = [
            {"number": 2, "dialogue": [{"speaker": "A", "line": "Stay here, please."}]},
            {"number": 3, "dialogue": [{"speaker": "B", "line": "STAY HERE PLEASE"}],
        }
        with self.assertRaisesRegex(ValueError, "Repeated dialogue rejected"):
            engine.validate_unique_story_dialogue(scenes)

    def test_accepts_distinct_lines_and_empty_dialogue(self):
        scenes = [
            {"number": 1, "dialogue": []},
            {"number": 2, "dialogue": [{"speaker": "A", "line": "The key is under the stone."}]},
            {"number": 3, "dialogue": [{"speaker": "B", "line": "I can see the river."}]},
        ]
        report = engine.validate_unique_story_dialogue(scenes)
        self.assertTrue(report["passed"])
        self.assertEqual(report["unique_dialogue_lines"], 2)

    def test_rejects_blank_lines(self):
        with self.assertRaisesRegex(ValueError, "blank dialogue"):
            engine.validate_unique_story_dialogue([
                {"number": 4, "dialogue": [{"speaker": "A", "line": "   "}]}
            ])


if __name__ == "__main__":
    unittest.main()
