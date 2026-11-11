import unittest
from storybook_engine import _parse_json_once

class JsonParsingTests(unittest.TestCase):
    def test_parses_valid_object(self):
        self.assertEqual(_parse_json_once('{"title":"Test","scenes":[]}'), {"title": "Test", "scenes": []})

    def test_repairs_trailing_comma(self):
        self.assertEqual(_parse_json_once('{"title":"Test",}'), {"title": "Test"})

    def test_rejects_broken_json(self):
        with self.assertRaises(Exception):
            _parse_json_once('{"title":"Test" "scenes":[]}')

if __name__ == "__main__":
    unittest.main()
