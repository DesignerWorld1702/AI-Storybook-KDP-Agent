import unittest
import storybook_engine as engine
from storybook_engine import _parse_json_once

class JsonParsingTests(unittest.TestCase):
    def test_parses_valid_object(self):
        self.assertEqual(_parse_json_once('{"title":"Test","scenes":[]}'), {"title": "Test", "scenes": []})

    def test_repairs_trailing_comma(self):
        self.assertEqual(_parse_json_once('{"title":"Test",}'), {"title": "Test"})

    def test_accepts_literal_newline_inside_string(self):
        parsed = _parse_json_once('{"title":"A story\\nwith a line break"}')
        self.assertEqual(parsed["title"], "A story\nwith a line break")

    def test_accepts_literal_tab_inside_string(self):
        parsed = _parse_json_once('{"title":"A story\\twith a tab"}')
        self.assertEqual(parsed["title"], "A story\twith a tab")


    def test_extract_json_repairs_missing_comma_without_model_retry(self):
        parsed = engine.extract_json('{"title":"Test" "scenes":[]}')
        self.assertEqual(parsed.get("title"), "Test")
        self.assertEqual(parsed.get("scenes"), [])

    def test_rejects_broken_json(self):
        with self.assertRaises(Exception):
            _parse_json_once('{"title":"Test" "scenes":[]}')

if __name__ == "__main__":
    unittest.main()
