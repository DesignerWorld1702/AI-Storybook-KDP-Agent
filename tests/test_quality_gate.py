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
                    "text": (
                        "Hero takes a clear step forward and discovers one useful clue. The new clue changes the plan and gives the team a reason to keep moving toward the truth. Before sunset, they compare the marks, check the locked gate, and agree to follow the safest path together."
                        + (" The Shade's mark appears on the evidence." if number in {5, 10, 15, 42, 46, 50} else "")
                    ),
                    "visual_prompt": f"A distinct cinematic illustration for scene {number}.",
                    "characters_present": ["Hero", "The Shade"] if number in {5, 10, 15, 42, 46, 50} else ["Hero"],
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


    def test_vague_costume_uses_concrete_role_based_fallback(self):
        plan = {
            "genre": "Real-world crime suspense thriller",
            "title": "Test Book",
            "character_registry": [{
                "name": "Kate Matthews",
                "role": "detective",
                "importance": "protagonist",
                "species": "human",
                "immutable_identity": {"hair": "dark hair", "eyes": "brown"},
                "story_costume": "Police Department uniform",
            }],
        }
        response = {"costumes": [{
            "name": "Kate Matthews",
            "story_costume": "Police Department uniform",
        }]}
        with patch.object(engine, "cf_text", return_value=json.dumps(response)):
            engine.ensure_concrete_story_costumes(plan)
        costume = plan["character_registry"][0]["story_costume"]
        self.assertIn("navy field jacket", costume)
        self.assertIn("black leather boots", costume)



    def test_repaired_outline_still_rejects_repeated_scene_beats(self):
        rows = [
            {"number": i, "beat": f"Scene {i} introduces a distinct action, new clue, clear consequence, and a fresh decision for the team"}
            for i in range(1, 51)
        ]
        for index in range(44, 50):
            rows[index]["beat"] = rows[43]["beat"]
        with self.assertRaisesRegex(ValueError, "repeated or near-identical"):
            engine.validate_outline_rows(rows, [], strict_repetition=False)

    def test_outline_rejects_even_five_exact_duplicate_beats(self):
        rows = [
            {"number": i, "beat": f"Scene {i} reveals a new clue and creates a clear consequence for the investigation"}
            for i in range(1, 51)
        ]
        for index in range(45, 50):
            rows[index]["beat"] = rows[44]["beat"]
        with self.assertRaisesRegex(ValueError, "repeated or near-identical"):
            engine.validate_outline_rows(rows, [])

    def test_outline_rejects_semantically_near_identical_beats(self):
        rows = [
            {"number": i, "beat": f"Scene {i} reveals a new clue and creates a clear consequence for the investigation"}
            for i in range(1, 51)
        ]
        rows[1]["beat"] = "Scene two reveals a new clue and creates a clear consequence for the investigation"
        rows[2]["beat"] = "Scene three reveals a new clue and creates a clear consequence for the investigation"
        with self.assertRaisesRegex(ValueError, "near-identical"):
            engine.validate_outline_rows(rows, [])

    def test_repaired_outline_accepts_fifty_distinct_scene_beats(self):
        rows = [
            {"number": i, "beat": f"Scene {i} introduces a distinct action, new clue, clear consequence, and a fresh decision for the team"}
            for i in range(1, 51)
        ]
        reviewed = engine.validate_outline_rows(rows, [], strict_repetition=False)
        self.assertEqual(len(reviewed), 50)

    def test_outline_detects_unregistered_first_name_in_prose(self):
        outline = [{"number": 1, "beat": "James Winter gathers evidence against Victor."}]
        registry = [{"name": "James Winter"}, {"name": "Marcus Blackwood"}]
        self.assertEqual(
            engine.unregistered_named_entities_in_outline(outline, registry),
            ["Victor"],
        )

    def test_outline_detects_and_anonymizes_whose_name_is_extra(self):
        outline = [{"number": 1, "beat": "The woman, whose name is Sarah, explains the clue to Kate Matthews."}]
        registry = [{"name": "Kate Matthews"}]
        self.assertEqual(
            engine.unregistered_named_entities_in_outline(outline, registry),
            ["Sarah"],
        )
        engine.sanitize_outline_unregistered_names(outline, registry)
        self.assertNotIn("Sarah", outline[0]["beat"])
        self.assertIn("Kate Matthews", outline[0]["beat"])

    def test_registered_cast_must_have_meaningful_repeated_presence(self):
        registry = [
            {"name": "Kate Matthews", "importance": "protagonist"},
            {"name": "Alexander Petrov", "importance": "supporting"},
            {"name": "Victor LaGrange", "importance": "villain"},
        ]
        outline = []
        for number in range(1, 51):
            names = []
            if number <= 12 or number >= 41:
                names.append("Kate Matthews")
            if number in {3, 8, 15, 22}:
                names.append("Alexander Petrov")
            if number in {5, 12, 20, 35, 45, 49}:
                names.append("Victor LaGrange")
            outline.append({
                "number": number,
                "beat": f"Scene {number} advances the case with a distinct action and evidence. " + " ".join(names),
            })
        self.assertEqual(len(engine.validate_outline_cast_distribution(outline, registry)), 50)
        registry[1]["name"] = "Sarah Lee"
        with self.assertRaisesRegex(ValueError, "Supporting character Sarah Lee"):
            engine.validate_outline_cast_distribution(outline, registry)

    def test_registered_villain_must_be_foreshadowed_in_first_fifteen_scenes(self):
        outline = [
            {"number": i, "beat": f"Scene {i} shows a distinct action with new evidence and a clear consequence for the investigation"}
            for i in range(1, 51)
        ]
        with self.assertRaisesRegex(ValueError, "within scenes 1-15"):
            engine.validate_principal_villain_foreshadowing(outline, "Victor LaGrange")
        outline[8]["beat"] += " The notebook names Victor LaGrange as the person paying the witness."
        self.assertEqual(
            len(engine.validate_principal_villain_foreshadowing(outline, "Victor LaGrange")),
            50,
        )

    def test_outline_accepts_minor_beat_length_variation_up_to_22_words(self):
        rows = [
            {"number": i, "beat": f"Scene {i} introduces a distinct event with clear action, cause, consequence, and forward movement for the story"}
            for i in range(1, 51)
        ]
        rows[19]["beat"] = "Scene twenty reveals a new clue and changes the investigation while the main characters prepare their next careful move together"
        result = engine.validate_outline_rows(rows, [], strict_repetition=False)
        self.assertEqual(len(result), 50)

    def test_scene_page_text_is_revalidated_after_name_normalization(self):
        scene = {
            "number": 22,
            "heading": "Tracking Down the Leader",
            "text": " ".join(["Original"] * 56),
        }
        repaired = " ".join(["Clear"] * 45)
        with patch.object(engine, "cf_text", return_value=repaired):
            result = engine.repair_scene_page_text(
                scene,
                {"title": "The Art of Deception", "character_registry": [{"name": "James Winter"}],
                 "principal_villain": "Marcus Blackwood"},
                22,
            )
        self.assertEqual(len(result["text"].split()), 45)
        self.assertEqual(result["text"], repaired)

    def test_book_description_is_grounded_and_names_protagonist_and_villain(self):
        plan = {
            "title": "The Last Clue",
            "principal_villain": "Victor LaGrange",
            "character_registry": [
                {"name": "Kate Matthews", "importance": "protagonist"},
                {"name": "Victor LaGrange", "importance": "villain"},
            ],
        }
        outline = [
            {"number": i, "beat": f"Scene {i} advances the investigation with a distinct action and clear consequence"}
            for i in range(1, 51)
        ]
        description = (
            "Detective Kate Matthews follows a trail of stolen records after a witness is threatened. "
            "The first clue points to Victor LaGrange, a powerful crime boss who hides his role behind "
            "a trusted business. Kate works with her partner to trace the payments and protect the witness. "
            "Each new record brings them closer to the truth, but Victor sends people to stop them. "
            "Kate uses the evidence to expose his crime and bring him before the law. "
            "The case closes, the witness is safe, and Kate can finally return home with peace."
        )
        with patch.object(engine, "cf_text", return_value=description):
            result = engine.synchronize_book_description(plan, outline)
        self.assertEqual(result, description)
        self.assertIn("Kate Matthews", plan["description"])
        self.assertIn("Victor LaGrange", plan["description"])

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


    def test_outline_review_reclassifies_only_evidence_grounded_minor_notes(self):
        outline = [
            {"number": i, "beat": f"Scene {i} contains a clear event with evidence and a consequence for the story."}
            for i in range(1, 51)
        ]
        report = {
            "passed": False,
            "critical_issues": [
                {
                    "number": 7,
                    "reason": "This might be a minor plot hole, but it is not a major contradiction.",
                    "evidence_quote": outline[6]["beat"],
                },
                {
                    "number": 50,
                    "reason": "The final shot is a minor issue that could be improved.",
                    "evidence_quote": outline[49]["beat"],
                },
                {
                    "number": 20,
                    "reason": "A shootout happens here.",
                    "evidence_quote": "A shootout happens here.",
                },
                {
                    "reason": "This is a minor issue about the setup.",
                    "evidence_quote": outline[0]["beat"],
                },
            ],
            "minor_issues": [],
            "dimension_scores": {
                "plot": 8, "continuity": 7, "pacing": 8,
                "character_consistency": 8, "ending": 7,
            },
        }
        with patch.object(engine, "cf_text", return_value=json.dumps(report)):
            result = engine.review_story_outline(
                {"title": "Test", "character_registry": [], "principal_villain": "Villain"},
                outline,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["critical_issues"], [])
        self.assertEqual(len(result["minor_issues"]), 3)
        self.assertEqual(len(result["unverified_review_notes"]), 1)

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




    def test_outline_entity_detection_collapses_repeated_first_name(self):
        outline = [{"number": 1, "beat": "The witness is named Sophia Sophia."}]
        found = engine.unregistered_named_entities_in_outline(outline, [{"name": "Sophia Bennett"}])
        self.assertEqual(found, [])




    def test_registered_titled_full_name_partial_is_not_treated_as_new_person(self):
        outline = [{"number": 1, "beat": "Kate Matthews discovers the hidden records."}]
        registry = [{"name": "Detective Kate Matthews"}]
        self.assertEqual(engine.unregistered_named_entities_in_outline(outline, registry), [])
        engine.sanitize_outline_unregistered_names(outline, registry)
        self.assertIn("Detective Kate Matthews", outline[0]["beat"])


    def test_outline_repair_keeps_registered_principal_villain_as_mastermind(self):
        outline = [
            {"number": 21, "beat": "The mastermind behind the burglaries is revealed to be Victor's business partner."},
            {"number": 22, "beat": "The mastermind reveals a plan to frame Kate for the crimes."},
        ]
        engine.enforce_principal_villain_identity(outline, "Alexei Petrov")
        self.assertIn(
            "Alexei Petrov is revealed as the mastermind behind the burglaries",
            outline[0]["beat"],
        )
        self.assertTrue(outline[1]["beat"].startswith("Alexei Petrov reveals"))
        self.assertNotIn("business partner", outline[0]["beat"])

    def test_outline_sanitizer_expands_unique_registered_surname_alias(self):
        outline = [
            {"number": 1, "beat": "Jameson finds the final clue and confronts the villain."},
            {"number": 2, "beat": "Maya Kanzaki brings the final evidence to the court."},
        ]
        registry = [{"name": "Detective Jameson"}, {"name": "Maya Kanzaki"}]
        engine.sanitize_outline_unregistered_names(outline, registry)
        self.assertIn("Detective Jameson", outline[0]["beat"])
        self.assertEqual(engine.missing_registered_outline_names(outline, registry), [])

    def test_outline_sanitizer_final_sweep_removes_unregistered_full_names(self):
        outline = [
            {"number": 1, "beat": "The detective meets Naomi Tanaka outside the station."},
            {"number": 2, "beat": "A witness named Naomi Tanaka gives the clue to the team."},
        ]
        engine.sanitize_outline_unregistered_names(outline, [{"name": "Maya Kanzaki"}])
        self.assertEqual(
            engine.unregistered_named_entities_in_outline(outline, [{"name": "Maya Kanzaki"}]),
            [],
        )
        self.assertNotIn("Naomi Tanaka", " ".join(row["beat"] for row in outline))

    def test_outline_sanitizer_resolves_repeated_first_name_to_registered_character(self):
        outline = [{"number": 1, "beat": "The witness is named Sophia Sophia."}]
        engine.sanitize_outline_unregistered_names(outline, [{"name": "Sophia Bennett"}])
        self.assertNotIn("Sophia Sophia", outline[0]["beat"])
        self.assertIn("Sophia Bennett", outline[0]["beat"])

    def test_outline_sanitizer_does_not_expand_overlapping_aliases_twice(self):
        outline = [{"number": 1, "beat": "An old informant named Detective James reveals the clue."}]
        engine.sanitize_outline_unregistered_names(outline, [{"name": "James Reed"}])
        self.assertEqual(
            outline[0]["beat"],
            "An old informant named James Reed reveals the clue.",
        )

    def test_outline_sanitizer_handles_title_plus_first_name_only(self):
        outline = [{"number": 1, "beat": "Detective James reviews the hidden clue."}]
        engine.sanitize_outline_unregistered_names(outline, [{"name": "Kate Matthews"}])
        self.assertEqual(outline[0]["beat"], "the detective reviews the hidden clue.")

    def test_outline_sanitizer_expands_partial_name_to_registered_full_name(self):
        outline = [{"number": 1, "beat": "Detective James reviews the hidden clue."}]
        engine.sanitize_outline_unregistered_names(outline, [{"name": "James Reed"}])
        self.assertEqual(outline[0]["beat"], "Detective James Reed reviews the hidden clue.")

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

    def test_discard_cached_story_plan_removes_plan_and_scene_batches(self):
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            plan = workdir / "story_plan.json"
            plan.write_text("{}", encoding="utf-8")
            batches = [
                workdir / "scene_batch_01_05.json",
                workdir / "scene_batch_06_10.json",
            ]
            for batch in batches:
                batch.write_text("{}", encoding="utf-8")
            engine.discard_cached_story_plan(plan, workdir)
            self.assertFalse(plan.exists())
            self.assertTrue(all(not batch.exists() for batch in batches))

    def test_outline_rejects_ending_padded_with_epilogue_only_beats(self):
        rows = [
            {"number": i, "beat": f"the team takes a distinct action in scene {i} and finds a useful new clue"}
            for i in range(1, 51)
        ]
        filler = [
            "the team is praised by the mayor for its careful work and courage",
            "the city celebrates the heroes during a public ceremony in the main square",
            "the case is remembered as a cautionary tale for everyone in the art world",
            "the detective receives a medal and public recognition for solving the case",
            "the artist's exhibition is extended and her work is praised by many visitors",
        ]
        for offset, beat in enumerate(filler):
            rows[35 + offset]["beat"] = beat
        with self.assertRaisesRegex(ValueError, "epilogue-only"):
            engine.validate_outline_rows(rows, [], strict_repetition=False)

    def test_outline_allows_a_small_number_of_meaningful_aftermath_beats(self):
        rows = [
            {"number": i, "beat": f"the team takes a distinct action in scene {i} and finds a useful new clue"}
            for i in range(1, 51)
        ]
        rows[35]["beat"] = "the team is praised by the mayor for its careful work and courage"
        rows[36]["beat"] = "the artist's exhibition is extended and her work is praised by many visitors"
        rows[37]["beat"] = "the detective receives a medal for solving the case"
        reviewed = engine.validate_outline_rows(rows, [], strict_repetition=False)
        self.assertEqual(len(reviewed), 50)

    def test_critical_issue_is_rejected_even_with_high_score(self):
        with patch.object(engine, "cf_text", return_value=json.dumps(
            self.report(95, critical=["Scene 18 changes the lead character's name"])
        )):
            with self.assertRaisesRegex(ValueError, "quality gate"):
                engine.review_story_quality(self.valid_story(), [])


if __name__ == "__main__":
    unittest.main()
