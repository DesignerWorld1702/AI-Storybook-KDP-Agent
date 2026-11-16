import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import daily_runner


def valid_outline():
    templates = [
        "At {a}, Hero maps {b}, while Villain follows {c} toward {d}.",
        "Before dawn, Villain hides {a}; Hero compares {b} with {c} and reaches {d}.",
        "Hero questions the guard at {a}, then finds {b} beside {c} and seals {d}.",
        "A signal from {a} sends Hero to {b}; Villain removes {c} before {d} arrives.",
        "Villain tests {a} near {b}, but Hero discovers {c} and protects {d}.",
        "Hero opens {a}, studies {b}, and learns Villain moved {c} toward {d}.",
        "At {a}, Villain blocks {b}; Hero traces {c} and uncovers {d} in time.",
        "Hero follows a trail from {a} to {b}, while Villain destroys {c} near {d}.",
        "The clue at {a} changes the plan: Hero checks {b} before Villain reaches {c} or {d}.",
        "Villain leaves {a} behind, and Hero links {b}, {c}, and {d} to the crime.",
    ]
    rows = []
    for i in range(50):
        tokens = [f"zone{i:02d}", f"archive{i:02d}", f"signal{i:02d}", f"ledger{i:02d}"]
        rows.append({
            "number": i + 1,
            "beat": templates[i % len(templates)].format(
                a=tokens[0], b=tokens[1], c=tokens[2], d=tokens[3]
            ),
        })
    return rows


def valid_story():
    return {
        "story_schema_version": 5,
        "character_registry": [
            {"name": "Hero", "importance": "protagonist"},
            {"name": "Villain", "importance": "villain"},
        ],
        "principal_villain": "Villain",
        "trim_size": "8.5 × 8.5 in — square picture book",
        "pdf_text_size": 11,
        "scenes": [
            {"number": n, "text": "cached", "heading": f"Scene {n}",
             "visual_prompt": f"Distinct illustration for scene {n}",
             "characters_present": ["Hero"]}
            for n in range(1, 51)
        ],
    }


class DailyRunnerResumeTests(unittest.TestCase):
    def test_finds_newest_incomplete_book_even_when_older_than_today(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            completed = root / "20261115_daily_story"
            incomplete = root / "20261116_daily_story"
            completed.mkdir()
            incomplete.mkdir()
            (completed / "job_status.json").write_text(
                json.dumps({"status": "completed"}), encoding="utf-8"
            )
            (completed / "publication_package_verified.json").write_text(
                json.dumps({"verification": "passed"}), encoding="utf-8"
            )
            (incomplete / "job_status.json").write_text(
                json.dumps({"status": "failed"}), encoding="utf-8"
            )
            with patch.object(daily_runner, "OUTROOT", root):
                self.assertEqual(
                    daily_runner.find_latest_incomplete_book_date(),
                    date(2026, 11, 16),
                )

    def test_completed_but_unverified_book_remains_resumable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "20261116_daily_story"
            folder.mkdir()
            (folder / "job_status.json").write_text(
                json.dumps({"status": "completed"}), encoding="utf-8"
            )
            with patch.object(daily_runner, "OUTROOT", root):
                self.assertEqual(
                    daily_runner.find_latest_incomplete_book_date(),
                    date(2026, 11, 16),
                )

    def test_resume_latest_incomplete_book_calls_builder_with_saved_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "20261116_daily_story"
            folder.mkdir()
            (folder / "job_status.json").write_text(
                json.dumps({"status": "failed"}), encoding="utf-8"
            )
            config = {"test": True}
            with (
                patch.object(daily_runner, "OUTROOT", root),
                patch.object(daily_runner, "load_config", return_value=config),
                patch.object(daily_runner, "build_one_book", return_value=True) as build,
            ):
                self.assertEqual(daily_runner.resume_latest_incomplete_book(), 0)
            build.assert_called_once_with(date(2026, 11, 16), config)

    def test_cached_story_must_pass_quality_review_before_outputs_are_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outroot = root / "output"
            book = outroot / "20261116_daily_story"
            book.mkdir(parents=True)
            story = valid_story()
            (book / "story.json").write_text(json.dumps(story), encoding="utf-8")
            (book / "story_plan.json").write_text(
                json.dumps({"outline": valid_outline()}), encoding="utf-8"
            )
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
                result = daily_runner.build_one_book(date(2026, 11, 16), config)

            self.assertTrue(result)
            review.assert_called_once()
            saved = json.loads((book / "story.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["quality_review"]["score"], 90)
            self.assertEqual(
                json.loads((book / "image_quality_report.json").read_text())["checked_images"], 50
            )

    def test_cached_story_is_not_built_when_quality_review_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            outroot = Path(tmp) / "output"
            book = outroot / "20261116_daily_story"
            book.mkdir(parents=True)
            (book / "story.json").write_text(json.dumps(valid_story()), encoding="utf-8")
            (book / "story_plan.json").write_text(
                json.dumps({"outline": valid_outline()}), encoding="utf-8"
            )
            config = {
                "trim_size": "8.5 × 8.5 in — square picture book",
                "scene_words": 45,
                "pdf_text_size": 11,
            }

            with patch.object(daily_runner, "OUTROOT", outroot), \
                 patch.object(daily_runner.engine, "ensure_character_library_references"), \
                 patch.object(daily_runner.engine, "review_story_quality", side_effect=ValueError("quality gate rejected")) as review, \
                 patch.object(daily_runner.engine, "create_reference_images") as create_refs:
                result = daily_runner.build_one_book(date(2026, 11, 16), config)

            self.assertFalse(result)
            review.assert_called_once()
            create_refs.assert_not_called()
            status = json.loads((book / "job_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")


if __name__ == "__main__":
    unittest.main()
