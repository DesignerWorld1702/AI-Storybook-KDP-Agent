import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storybook_engine as engine


class PublicationQualityGateTests(unittest.TestCase):
    def report(self, score, passed=True, critical=None):
        return {
            "passed": passed,
            "critical_issues": critical or [],
            "minor_issues": [],
            "dimension_scores": {
                "plot": score / 10, "continuity": score / 10, "character_consistency": score / 10,
                "easy_english": score / 10, "pacing": score / 10, "emotional_impact": score / 10,
                "scene_variety": score / 10, "art_direction": score / 10, "originality": score / 10, "ending": score / 10
            },
            "score": score
        }

    def test_seven_out_of_ten_is_minimum_and_report_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(engine, "cf_text", return_value=json.dumps(self.report(70))):
                result = engine.review_story_quality(
                    {"title": "Test", "character_registry": [], "scenes": []}, [], tmp
                )
            self.assertEqual(result["rating_out_of_10"], 7.0)
            self.assertEqual(result["score"], 70.0)
            self.assertEqual(result["minimum_publish_rating"], 7.0)
            self.assertTrue((Path(tmp) / "quality_review.json").is_file())

    def test_below_seven_is_rejected_even_if_model_says_passed(self):
        with patch.object(engine, "cf_text", return_value=json.dumps(self.report(69))):
            with self.assertRaisesRegex(ValueError, "quality gate"):
                engine.review_story_quality(
                    {"title": "Test", "character_registry": [], "scenes": []}, []
                )

    def test_critical_issue_is_rejected_even_with_high_score(self):
        with patch.object(engine, "cf_text", return_value=json.dumps(
            self.report(95, critical=["Scene 18 changes the lead character's name"])
        )):
            with self.assertRaisesRegex(ValueError, "quality gate"):
                engine.review_story_quality(
                    {"title": "Test", "character_registry": [], "scenes": []}, []
                )


if __name__ == "__main__":
    unittest.main()
