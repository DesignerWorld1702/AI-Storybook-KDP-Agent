import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storybook_engine as engine


class PublicationQualityGateTests(unittest.TestCase):
    def valid_story(self):
        return {
            "title": "Test",
            "principal_villain": "The Shade",
            "character_registry": [
                {"name": "Hero", "importance": "protagonist"},
                {"name": "The Shade", "importance": "villain"},
            ],
            "scenes": [
                {
                    "number": number,
                    "heading": f"Scene {number}",
                    "text": "The hero takes a clear step forward and discovers one useful clue. The new clue changes the plan and gives the team a reason to keep moving toward the truth. Before sunset, they compare the marks, check the locked gate, and agree to follow the safest path together.",
                    "visual_prompt": f"A distinct cinematic illustration for scene {number}.",
                    "characters_present": ["Hero"] if number < 50 else ["Hero", "The Shade"],
                }
                for number in range(1, 51)
            ],
        }

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


    def test_real_world_registry_corrects_mislabelled_human_creature_role(self):
        plan = {
            "character_registry": [{
                "name": "Emily Patel",
                "species": "human",
                "role": "creature",
                "importance": "creature",
                "visual_id": "visual_001",
            }]
        }
        engine.normalize_real_world_registry(plan)
        entry = plan["character_registry"][0]
        self.assertEqual(entry["species"], "human")
        self.assertEqual(entry["importance"], "supporting")
        self.assertEqual(entry["role"], "human supporting character")


    def test_real_world_registry_keeps_mislabelled_principal_villain_registered(self):
        plan = {
            "principal_villain": "Olivia Lee",
            "character_registry": [{
                "name": "Olivia Lee",
                "species": "human",
                "role": "creature",
                "importance": "creature",
                "visual_id": "visual_003",
            }]
        }
        engine.normalize_real_world_registry(plan)
        entry = plan["character_registry"][0]
        self.assertEqual(entry["importance"], "villain")
        self.assertEqual(entry["role"], "human antagonist")

    def test_real_world_registry_rejects_nonhuman_species(self):
        plan = {"character_registry": [{
            "name": "Drako",
            "species": "dragon",
            "role": "antagonist",
            "importance": "villain",
            "visual_id": None,
        }]}
        with self.assertRaisesRegex(ValueError, "non-human character"):
            engine.normalize_real_world_registry(plan)

    def test_real_world_registry_remaps_fantasy_visual_slot_to_unused_human_model(self):
        plan = {"character_registry": [{
            "name": "Bamboo",
            "species": "human",
            "role": "supporting",
            "importance": "supporting",
            "visual_id": "visual_012",
            "immutable_identity": {"species": "panda"},
            "story_costume": "A plain dark jacket",
        }]}
        engine.normalize_real_world_registry(plan)
        item = plan["character_registry"][0]
        self.assertEqual(item["species"], "human")
        self.assertNotIn(item["visual_id"], {"visual_009", "visual_010", "visual_011", "visual_012"})
        self.assertNotEqual(item["immutable_identity"], {"species": "panda"})

    def test_seven_out_of_ten_is_minimum_and_report_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(engine, "cf_text", return_value=json.dumps(self.report(70))):
                result = engine.review_story_quality(self.valid_story(), [], tmp)
            self.assertEqual(result["rating_out_of_10"], 7.0)
            self.assertEqual(result["score"], 70.0)
            self.assertEqual(result["minimum_publish_rating"], 7.0)
            self.assertTrue((Path(tmp) / "quality_review.json").is_file())


    def test_core_dimension_below_seven_fails_even_when_average_is_high(self):
        report = self.report(80)
        report["dimension_scores"]["easy_english"] = 6.0
        with patch.object(engine, "cf_text", return_value=json.dumps(report)):
            with self.assertRaisesRegex(ValueError, "easy_english"):
                engine.review_story_quality(self.valid_story(), [])

    def test_below_seven_is_rejected_even_if_model_says_passed(self):
        with patch.object(engine, "cf_text", return_value=json.dumps(self.report(69))):
            with self.assertRaisesRegex(ValueError, "quality gate"):
                engine.review_story_quality(self.valid_story(), [])







    def test_canonical_cast_alias_replacement_updates_page_text(self):
        names, aliases = engine.normalize_scene_cast_names(
            ["Chief Jameson"], [{"name": "James Winter"}], return_aliases=True
        )
        scene = {"text": "Chief Jameson enters. Jameson checks the door.", "heading": "Chief Jameson arrives."}
        engine.apply_scene_cast_aliases(scene, aliases)
        self.assertEqual(names, ["James Winter"])
        self.assertEqual(scene["text"], "James Winter enters. James Winter checks the door.")
        self.assertEqual(scene["heading"], "James Winter arrives.")

    def test_title_and_clear_name_variants_map_to_canonical_registry_name(self):
        names = engine.normalize_scene_cast_names(
            ["Chief Jameson", "victim", "Winter"],
            [{"name": "James Winter"}],
        )
        self.assertEqual(names, ["James Winter"])

    def test_rejects_non_string_scene_cast_entries(self):
        with self.assertRaisesRegex(ValueError, "must be a string"):
            engine.normalize_scene_cast_names(["Hero", {"name": "Unknown"}])




    def test_concrete_costume_lock_replaces_abstract_costume(self):
        plan = {
            "genre": "Real-world crime suspense thriller",
            "title": "Test Book",
            "character_registry": [{
                "name": "Ethan Thompson",
                "role": "human antagonist",
                "importance": "villain",
                "immutable_identity": {"hair": "short dark hair", "eyes": "grey"},
                "story_costume": "Disguise and deception",
            }],
        }
        response = {
            "costumes": [{
                "name": "Ethan Thompson",
                "story_costume": "A dark charcoal overcoat over a black shirt, black trousers, polished leather boots, and a silver ring",
            }]
        }
        with patch.object(engine, "cf_text", return_value=json.dumps(response)):
            engine.ensure_concrete_story_costumes(plan)
        item = plan["character_registry"][0]
        self.assertIn("overcoat", item["story_costume"])
        self.assertIn("fixed story costume", item["visual_bible"])
        self.assertIn("Ethan Thompson", plan["character_bible"])

    def test_outline_review_computes_score_and_enforces_core_dimensions(self):
        report = {
            "passed": True,
            "critical_issues": [],
            "minor_issues": [],
            "dimension_scores": {
                "plot": 8, "continuity": 8, "pacing": 8,
                "character_consistency": 8, "ending": 8,
            },
        }
        with patch.object(engine, "cf_text", return_value=json.dumps(report)):
            result = engine.review_story_outline(
                {"title": "Test", "character_registry": [], "principal_villain": "Villain"},
                [{"number": 1, "beat": "A short outline beat"}],
            )
        self.assertEqual(result["score"], 80.0)
        self.assertTrue(result["passed"])

    def test_outline_review_rejects_core_dimension_below_seven(self):
        report = {
            "passed": True,
            "critical_issues": [],
            "minor_issues": [],
            "dimension_scores": {
                "plot": 8, "continuity": 8, "pacing": 8,
                "character_consistency": 8, "ending": 6,
            },
        }
        with patch.object(engine, "cf_text", return_value=json.dumps(report)):
            result = engine.review_story_outline(
                {"title": "Test", "character_registry": [], "principal_villain": "Villain"},
                [{"number": 1, "beat": "A short outline beat"}],
            )
        self.assertFalse(result["passed"])
        self.assertTrue(result["critical_issues"])

    def test_page_word_count_gate_is_exactly_40_to_55_words(self):
        self.assertFalse(engine.page_text_word_count_valid("word " * 39))
        self.assertTrue(engine.page_text_word_count_valid("word " * 40))
        self.assertTrue(engine.page_text_word_count_valid("word " * 55))
        self.assertFalse(engine.page_text_word_count_valid("word " * 56))



    def test_scene_sanitizer_anonymizes_unregistered_people_and_keeps_registered_cast(self):
        scene = {
            "heading": "Captain Rachel Jenkins arrives",
            "text": "Captain Rachel Jenkins arrives and questions James Winter.",
            "visual_prompt": "Captain Rachel Jenkins stands beside James Winter.",
            "characters_present": ["Captain Rachel Jenkins", "James Winter"],
        }
        engine.sanitize_scene_unregistered_cast(
            scene, [{"name": "James Winter"}]
        )
        self.assertEqual(scene["characters_present"], ["James Winter"])
        self.assertEqual(scene["text"], "the captain arrives and questions James Winter.")
        self.assertEqual(scene["heading"], "the captain arrives")
        self.assertEqual(
            engine.unregistered_named_entities_in_outline(
                [{"beat": scene["text"]}], [{"name": "James Winter"}]
            ),
            [],
        )

    def test_outline_sanitizer_removes_extra_people_and_keeps_registered_cast(self):
        registry = [{"name": "James Winter"}, {"name": "Emily Taylor"}]
        outline = [
            {"number": 1, "beat": "The victim, Sarah Lee, reports the strange break-in."},
            {"number": 2, "beat": "A reclusive hacker known only as Zero Cool shares a clue."},
            {"number": 3, "beat": "Police Chief Michael Reed briefs James Winter."},
            {"number": 4, "beat": "The old informant, named Sal, reveals the truth to Emily Taylor."},
        ]
        cleaned = engine.sanitize_outline_unregistered_names(outline, registry)
        self.assertEqual(
            [item["beat"] for item in cleaned],
            [
                "The victim reports the strange break-in.",
                "A reclusive hacker shares a clue.",
                "the police chief briefs James Winter.",
                "The old informant reveals the truth to Emily Taylor.",
            ],
        )
        self.assertEqual(engine.unregistered_named_entities_in_outline(cleaned, registry), [])

    def test_outline_rejects_unregistered_named_people(self):
        outline = [
            {"number": 1, "beat": "The victim, Sarah Lee, reports the strange break-in."},
            {"number": 2, "beat": "Police Chief Michael Reed briefs James Winter."},
            {"number": 3, "beat": "A hacker known only as Zero Cool shares a clue."},
            {"number": 4, "beat": "The old informant, named Sal, reveals the truth."},
            {"number": 5, "beat": "Dr. Lee gives Emily Taylor a warning."},
        ]
        registry = [
            {"name": "James Winter"},
            {"name": "Emily Taylor"},
            {"name": "Alexei Petrov"},
        ]
        self.assertEqual(
            engine.unregistered_named_entities_in_outline(outline, registry),
            ["Sarah Lee", "Michael Reed", "Zero Cool", "Sal", "Lee"],
        )

    def test_outline_requires_exact_registered_names(self):
        outline = [
            {"number": 1, "beat": "Jameson arrives at the mansion."},
            {"number": 2, "beat": "Rodriguez finds the hidden evidence."},
        ]
        registry = [
            {"name": "James Winter"},
            {"name": "Sarah Taylor"},
            {"name": "Victor Vex"},
        ]
        self.assertEqual(
            engine.missing_registered_outline_names(outline, registry),
            ["James Winter", "Sarah Taylor", "Victor Vex"],
        )

    def test_unnamed_role_labels_are_not_treated_as_named_characters(self):
        names = engine.normalize_scene_cast_names(["Hero", "victim", "Guard", "The Shade", "Hero"])
        self.assertEqual(names, ["Hero", "The Shade"])

    def test_rejects_incomplete_book_before_model_review(self):
        story = self.valid_story()
        story["scenes"].pop()
        with self.assertRaisesRegex(ValueError, "exactly 50 scenes"):
            engine.review_story_quality(story, [])

    def test_rejects_page_outside_word_count_before_model_review(self):
        story = self.valid_story()
        story["scenes"][7]["text"] = "Too short."
        with self.assertRaisesRegex(ValueError, "scene 8 has 2 words"):
            engine.review_story_quality(story, [])

    def test_rejects_unregistered_character_before_model_review(self):
        story = self.valid_story()
        story["scenes"][2]["characters_present"] = ["Hero", "New Person"]
        with self.assertRaisesRegex(ValueError, "unregistered character"):
            engine.review_story_quality(story, [])

    def test_critical_issue_is_rejected_even_with_high_score(self):
        with patch.object(engine, "cf_text", return_value=json.dumps(
            self.report(95, critical=["Scene 18 changes the lead character's name"])
        )):
            with self.assertRaisesRegex(ValueError, "quality gate"):
                engine.review_story_quality(self.valid_story(), [])


if __name__ == "__main__":
    unittest.main()
