import base64
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from kindle_epub import build_fixed_layout_epub


# Valid tiny PNG fixture; EPUB tests only the package structure, not image quality.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "lV8AAAAASUVORK5CYII="
)


class KindleEpubTests(unittest.TestCase):
    def test_builds_valid_fixed_layout_epub_with_50_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = root / "scenes"
            images.mkdir()
            for number in range(1, 51):
                (images / f"scene_{number:03d}.png").write_bytes(PNG_1X1)
            story = {
                "title": "EPUB Test Story",
                "author": "Test Author",
                "scenes": [
                    {"heading": f"Scene {n}", "text": f"Story text for scene {n}."}
                    for n in range(1, 51)
                ],
            }
            output = build_fixed_layout_epub(story, images, root / "book.epub")
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist()[0], "mimetype")
                self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
                package = ET.fromstring(archive.read("EPUB/package.opf"))
                ns = {"opf": "http://www.idpf.org/2007/opf"}
                self.assertEqual(package.attrib["version"], "3.0")
                layout = package.find(".//opf:meta[@property='rendition:layout']", ns)
                self.assertEqual(layout.text, "pre-paginated")
                pages = [n for n in archive.namelist() if n.startswith("EPUB/text/page_")]
                self.assertEqual(len(pages), 50)


if __name__ == "__main__":
    unittest.main()
