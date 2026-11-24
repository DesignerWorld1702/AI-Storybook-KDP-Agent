import unittest

import storybook_engine as engine


class OutlineVillainFinalActTests(unittest.TestCase):
    def outline(self, villain_late=True):
        rows = []
        for n in range(1, 51):
            beat = f"Scene {n}: Detective Mira follows a new clue."
            if n in {3, 8, 12, 20, 30} or (villain_late and n in {43, 48}):
                beat += " Alexander Petrov acts to obstruct the investigation."
            rows.append({"number": n, "beat": beat})
        return rows

    def registry(self):
        return [
            {"name": "Detective Mira", "importance": "protagonist"},
            {"name": "Alexander Petrov", "importance": "villain"},
        ]

    def test_rejects_villain_absent_from_final_ten_scenes(self):
        with self.assertRaisesRegex(ValueError, "final ten scenes"):
            engine.validate_outline_cast_distribution(self.outline(False), self.registry())

    def test_accepts_named_villain_in_final_ten_scenes(self):
        engine.validate_outline_cast_distribution(self.outline(True), self.registry())


if __name__ == "__main__":
    unittest.main()
