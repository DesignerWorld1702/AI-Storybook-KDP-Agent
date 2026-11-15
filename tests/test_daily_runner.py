import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily_runner


class DailyRunnerResumeTests(unittest.TestCase):
    def test_cached_story_must_pass_quality_review_before_outputs_are_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outroot = root / "output"
            book = outroot / "20261116_daily_story"
            book.mkdir(parents=True)
            story = {
                "story_schema_version": 5,
                "character_registry": [{"name": "Hero"}],
                "principal_villain": "Villain",
                "trim_size": "8.5 × 8.5 in — square picture book",
                "pdf_text_size": 11,
                "scenes": [{"number": n, "text": "cached"} for n in range(1, 51)],
            }
            (book / "story.json").write_text(json.dumps(story), encoding="utf-8")
            config = {
                "trim_size": "8.5 × 8.5 in — square picture book",
                "scene_words": 45,
                "pdf_text_size": 11,
            }

            with patch.object(daily_runner, "OUTROOT", outroot), \
                 patch.object(daily_runner.engine, "ensure_character_library_references"), \
                 patch.object(daily_runner.engine, "review_story_quality", return_value={"score": 90}) as review, \
                 patch.object(daily_runner.engine, "create_reference_images", return_value=[root / "style.png"]), \
                 patch.object(daily_runner.engine, "scene_image_dimensions", return_value=(1536, 1536)), \
                 patch.object(daily_runner, "valid_image", return_value=True), \
                 patch.object(daily_runner.engine, "validate_scene_image_set", return_value={"checked_images": 50}), \
                 patch.object(daily_runner.engine, "build_page_images"), \
                 patch.object(daily_runner.engine, "build_pdf"), \
                 patch.object(daily_runner.engine, "make_kindle_html"), \
                 patch.object(daily_runner, "build_fixed_layout_epub"), \
                 patch.object(daily_runner.engine, "make_listing"), \
                 patch.object(daily_runner.engine, "write_checklist"), \
                 patch.object(daily_runner.engine, "create_contact_sheet"):
                result = daily_runner.build_one_book(
                    __import__("datetime").date(2026, 11, 16), config
                )

            self.assertTrue(result)
            review.assert_called_once()
            saved = json.loads((book / "story.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["quality_review"]["score"], 90)
            self.assertEqual(json.loads((book / "image_quality_report.json").read_text())["checked_images"], 50)

    def test_cached_story_is_not_built_when_quality_review_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outroot = Path(tmp) / "output"
            book = outroot / "20261116_daily_story"
            book.mkdir(parents=True)
            story = {
                "story_schema_version": 5,
                "character_registry": [{"name": "Hero"}],
                "principal_villain": "Villain",
                "scenes": [{"number": n, "text": "cached"} for n in range(1, 51)],
            }
            (book / "story.json").write_text(json.dumps(story), encoding="utf-8")
            config = {"trim_size": "8.5 × 8.5 in — square picture book", "scene_words": 45, "pdf_text_size": 11}

            with patch.object(daily_runner, "OUTROOT", outroot), \
                 patch.object(daily_runner.engine, "ensure_character_library_references"), \
                 patch.object(daily_runner.engine, "review_story_quality", side_effect=ValueError("quality gate rejected")) as review, \
                 patch.object(daily_runner.engine, "create_reference_images") as create_refs:
                result = daily_runner.build_one_book(
                    __import__("datetime").date(2026, 11, 16), config
                )

            self.assertFalse(result)
            review.assert_called_once()
            create_refs.assert_not_called()
            status = json.loads((book / "job_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")


if __name__ == "__main__":
    unittest.main()
