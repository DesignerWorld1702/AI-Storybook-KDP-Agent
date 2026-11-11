import json
import tempfile
import unittest
from pathlib import Path

import storybook_engine as engine


class PermanentVisualSlotTests(unittest.TestCase):
    def test_repository_models_store_only_physical_identity(self):
        models_root = engine.CHARACTER_LIBRARY_ROOT / "models"
        for number in range(1, 13):
            identity_path = models_root / f"visual_{number:03d}" / "identity.json"
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            self.assertEqual(identity["visual_id"], f"visual_{number:03d}")
            self.assertIsInstance(identity["immutable_identity"], dict)
            self.assertNotIn("library_name", identity)
            self.assertNotIn("character_name", identity)
            self.assertNotIn("changeable_story_identity", identity)

    def test_catalog_exposes_neutral_slots_without_legacy_names_or_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(1, 13):
                model = root / "models" / f"visual_{number:03d}"
                model.mkdir(parents=True)
                identity = {
                    "visual_id": f"visual_{number:03d}",
                    "library_name": f"Legacy Name {number}",
                    "character_name": f"Legacy Name {number}",
                    "immutable_identity": {"hair": f"identity trait {number}"},
                    "changeable_story_identity": {"occupation": "old job"},
                }
                (model / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
            original_root = engine.CHARACTER_LIBRARY_ROOT
            try:
                engine.CHARACTER_LIBRARY_ROOT = root
                catalog = engine.permanent_visual_slot_catalog()
            finally:
                engine.CHARACTER_LIBRARY_ROOT = original_root

        self.assertEqual(len(catalog), 12)
        self.assertEqual(catalog[0]["slot"], "Character 1")
        self.assertEqual(catalog[-1]["slot"], "Character 12")
        self.assertEqual(catalog[0]["visual_id"], "visual_001")
        self.assertEqual(catalog[0]["immutable_identity"], {"hair": "identity trait 1"})
        self.assertNotIn("library_name", catalog[0])
        self.assertNotIn("character_name", catalog[0])
        self.assertNotIn("changeable_story_identity", catalog[0])


if __name__ == "__main__":
    unittest.main()
