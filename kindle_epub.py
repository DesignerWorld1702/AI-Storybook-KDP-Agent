from __future__ import annotations

import html
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET


def build_fixed_layout_epub(story: dict, images_dir: Path, output_path: Path) -> Path:
    """Build a Kindle-compatible EPUB 3 fixed-layout picture book (square pages)."""
    scenes = story.get("scenes") or []
    if len(scenes) != 50:
        raise ValueError(f"Expected exactly 50 scenes for EPUB; received {len(scenes)}.")
    images_dir = Path(images_dir)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    title = str(story.get("title") or "Untitled Storybook")
    author = str(story.get("author") or "Unknown Author")
    language = str(story.get("language") or "en")
    book_id = "urn:uuid:" + str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with zipfile.ZipFile(output_path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")

        css = """
html, body { margin:0; padding:0; width:100%; height:100%; background:#111; }
body { font-family: serif; }
.page { box-sizing:border-box; width:100%; height:100%; min-height:100vh;
        page-break-after:always; overflow:hidden; }
.art { width:100%; height:100%; object-fit:contain; display:block; margin:0 auto; }
"""
        book.writestr("EPUB/styles/book.css", css)

        manifest = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="css" href="styles/book.css" media-type="text/css"/>',
        ]
        spine = []
        nav_items = []
        for i, scene in enumerate(scenes, 1):
            image_name = f"page_{i:03d}.png"
            img_path = images_dir / image_name
            if not img_path.is_file():
                raise FileNotFoundError(f"Missing integrated page image for EPUB page {i}: {img_path}")
            page_name = f"page_{i:03d}.xhtml"
            heading = html.escape(str(scene.get("heading") or f"Scene {i}"))
            alt = html.escape(heading)
            xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(language)}">
<head><title>{heading}</title><meta charset="utf-8"/><meta name="viewport" content="width=1600,height=1600"/><link rel="stylesheet" type="text/css" href="../styles/book.css"/></head>
<body><section class="page"><img class="art" src="../images/{image_name}" alt="{alt}"/></section></body></html>"""
            book.writestr(f"EPUB/text/{page_name}", xhtml)
            book.write(img_path, f"EPUB/images/{image_name}", compress_type=zipfile.ZIP_DEFLATED)
            manifest.append(f'<item id="page{i}" href="text/{page_name}" media-type="application/xhtml+xml"/>')
            manifest.append(f'<item id="img{i}" href="images/{image_name}" media-type="image/png"/>')
            spine.append(f'<itemref idref="page{i}"/>')
            nav_items.append(f'<li><a href="text/{page_name}">{heading}</a></li>')

        nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(language)}">
<head><title>Contents</title><meta charset="utf-8"/></head>
<body><nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{''.join(nav_items)}</ol></nav></body></html>"""
        book.writestr("EPUB/nav.xhtml", nav)

        package = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" prefix="rendition: http://www.idpf.org/vocab/rendition/#" xml:lang="{html.escape(language)}">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{book_id}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:creator>{html.escape(author)}</dc:creator><dc:language>{html.escape(language)}</dc:language><meta property="dcterms:modified">{modified}</meta>
<meta property="rendition:layout">pre-paginated</meta><meta property="rendition:orientation">auto</meta><meta property="rendition:spread">none</meta>
</metadata><manifest>{''.join(manifest)}</manifest><spine page-progression-direction="ltr">{''.join(spine)}</spine></package>"""
        book.writestr("EPUB/package.opf", package)

    # Basic ZIP and XML integrity validation before returning the deliverable.
    with zipfile.ZipFile(output_path, "r") as check:
        if check.namelist()[0] != "mimetype" or check.read("mimetype") != b"application/epub+zip":
            raise ValueError("EPUB container header is invalid.")
        ET.fromstring(check.read("EPUB/package.opf"))
        ET.fromstring(check.read("EPUB/nav.xhtml"))
        if len([n for n in check.namelist() if n.startswith("EPUB/text/page_")]) != 50:
            raise ValueError("EPUB does not contain all 50 page documents.")
    return output_path
