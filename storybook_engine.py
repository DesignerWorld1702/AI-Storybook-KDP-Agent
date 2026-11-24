import os, json, re, time, base64, io, textwrap, logging, difflib, shutil
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageStat
from reportlab.lib.pagesizes import inch
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from html import escape

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
OUTROOT = ROOT / "output"
OUTROOT.mkdir(exist_ok=True)
CHARACTER_LIBRARY_ROOT = ROOT / "character_library"

CF_ACCOUNT = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
TEXT_MODEL = os.getenv("CLOUDFLARE_TEXT_MODEL", "@cf/meta/llama-3.1-8b-instruct").strip()
IMAGE_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b").strip()

TRIM_SIZES = {
    "8.5 × 8.5 in — square picture book": (8.5, 8.5),
    "8.5 × 11 in — portrait picture book": (8.5, 11.0),
    "6 × 9 in — standard paperback": (6.0, 9.0),
}

def scene_image_dimensions(trim, max_side=None):
    """Return full-page image dimensions; story text is overlaid later."""
    if trim not in TRIM_SIZES:
        raise ValueError(f"Unsupported trim size: {trim}")
    max_side = int(max_side or os.getenv("STORY_IMAGE_MAX_SIDE", "1536"))
    if max_side < 1024 or max_side > 1920 or max_side % 16:
        raise ValueError("STORY_IMAGE_MAX_SIDE must be a multiple of 16 from 1024 to 1920.")
    page_w, page_h = TRIM_SIZES[trim]
    scale = max_side / max(page_w, page_h)
    width = max(256, int(round((page_w * scale) / 16) * 16))
    height = max(256, int(round((page_h * scale) / 16) * 16))
    if width * height > 4_000_000:
        raise ValueError("Selected full-page dimensions exceed FLUX.2 Klein's 4MP output limit.")
    return width, height


GENERIC_CHARACTER_LABELS = {
    "minion", "minions", "guard", "guards", "soldier", "soldiers",
    "villager", "villagers", "townsperson", "townspeople", "crowd", "crowds",
    "bandit", "bandits", "enemy", "enemies", "victim", "victims",
    "suspect", "suspects", "witness", "witnesses", "officer", "officers",
    "police", "detective", "detectives", "doctor", "doctors", "nurse", "nurses",
    "reporter", "reporters", "driver", "drivers", "clerk", "clerks",
    "stranger", "strangers", "attacker", "attackers", "criminal", "criminals",
    "judge", "judges", "lawyer", "lawyers", "prosecutor", "prosecutors",
    "agent", "agents", "investigator", "investigators", "bodyguard", "bodyguards",
    "worker", "workers", "passenger", "passengers", "customer", "customers",
    "man", "men", "woman", "women", "child", "children", "boy", "boys",
    "girl", "girls", "people", "person", "bystander", "bystanders",
    "crew", "staff", "audience", "family", "family members", "officials"
}


def normalize_scene_cast_names(names, registry=None, return_aliases=False):
    """Keep canonical names, map clear title/surname variants, and omit unnamed extras."""
    if not isinstance(names, list):
        raise ValueError("characters_present must be a list.")
    registered = [
        str(item.get("name", "")).strip()
        for item in (registry or []) if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    role_prefix = re.compile(
        r"^(?:chief|detective|officer|dr\.?|mr\.?|mrs\.?|ms\.?|miss|captain|agent|"
        r"judge|doctor|professor|inspector|sergeant|lieutenant|commander|mayor|"
        r"constable|sheriff|director)\s+",
        re.IGNORECASE
    )
    normalized = []
    aliases = {}
    for name in names:
        if not isinstance(name, str):
            raise ValueError("Every characters_present entry must be a string.")
        clean_name = name.strip()
        if not clean_name or clean_name.casefold() in GENERIC_CHARACTER_LABELS:
            continue
        candidate_text = role_prefix.sub("", clean_name).strip()
        exact = next((item for item in registered if item.casefold() == candidate_text.casefold()), None)
        if exact:
            normalized.append(exact)
            if clean_name.casefold() != exact.casefold():
                aliases[clean_name] = exact
            for token in re.findall(r"[A-Za-z]+", candidate_text):
                if token.casefold() not in {part.casefold() for part in exact.split()}:
                    aliases[token] = exact
            continue
        words = re.findall(r"[A-Za-z]+", candidate_text.casefold())
        candidates = set()
        for registered_name in registered:
            registered_words = re.findall(r"[A-Za-z]+", registered_name.casefold())
            if any(
                word == known
                or (len(word) >= 4 and len(known) >= 4
                    and difflib.SequenceMatcher(None, word, known).ratio() >= 0.80)
                for word in words for known in registered_words
            ):
                candidates.add(registered_name)
        canonical = next(iter(candidates)) if len(candidates) == 1 else clean_name
        normalized.append(canonical)
        if canonical != clean_name:
            aliases[clean_name] = canonical
            for token in re.findall(r"[A-Za-z]+", candidate_text):
                if token.casefold() not in {part.casefold() for part in canonical.split()}:
                    aliases[token] = canonical
    result = list(dict.fromkeys(normalized))
    return (result, aliases) if return_aliases else result


def apply_scene_cast_aliases(scene, aliases):
    """Replace clear cast-list aliases with their canonical full registry names."""
    fields = ("heading", "text", "visual_prompt", "composition", "action_pose", "gaze", "expression")
    for alias, canonical in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
        for field in fields:
            value = scene.get(field)
            if isinstance(value, str) and value:
                scene[field] = re.sub(
                    r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])",
                    canonical, value, flags=re.IGNORECASE
                )
    return scene



def normalize_real_world_registry(plan):
    """Enforce human species and replace accidentally selected fantasy portraits with human ones."""
    fantasy_visual_ids = {"visual_009", "visual_010", "visual_011", "visual_012"}
    human_models = [
        model for model in load_character_library()
        if model.get("visual_id") and model.get("visual_id") not in fantasy_visual_ids
    ]
    registry = plan.get("character_registry", [])
    if not isinstance(registry, list) or not registry:
        raise ValueError("Real-world story is missing its human character registry.")
    used_human_ids = {
        item.get("visual_id") for item in registry
        if isinstance(item, dict) and item.get("visual_id") and item.get("visual_id") not in fantasy_visual_ids
    }
    for item in registry:
        if not isinstance(item, dict):
            raise ValueError("Character registry entries must be objects.")
        species = str(item.get("species") or "human").strip().casefold()
        if species not in ("human", "human being", "person"):
            raise ValueError(f"Real-world story contains a non-human character: {item.get('name')} ({species}).")
        item["species"] = "human"
        visual_id = item.get("visual_id")
        if visual_id in fantasy_visual_ids:
            replacement = next((model for model in human_models if model.get("visual_id") not in used_human_ids), None)
            if replacement is None:
                raise ValueError(
                    f"Real-world character {item.get('name')} selected a fantasy portrait and no unused human portrait remains."
                )
            item["visual_id"] = replacement.get("visual_id")
            item["immutable_identity"] = replacement.get("immutable_identity")
            used_human_ids.add(replacement.get("visual_id"))
            logging.warning(
                "Replaced non-human visual slot %s with human slot %s for %s.",
                visual_id, item.get("visual_id"), item.get("name")
            )
        importance = str(item.get("importance", "supporting")).strip().casefold()
        if importance in ("creature", "fantasy_creature"):
            importance = "villain" if str(item.get("name", "")).strip() == str(plan.get("principal_villain", "")).strip() else "supporting"
            item["importance"] = importance
        role = str(item.get("role", "")).strip()
        if "creature" in role.casefold() or "fantasy" in role.casefold():
            fallback = {
                "protagonist": "human lead character",
                "villain": "human antagonist",
                "supporting": "human supporting character",
            }.get(importance, "human supporting character")
            item["role"] = fallback
        if item.get("visual_id"):
            item["visual_bible"] = (
                f"{item.get('name', '')}: human physical identity "
                f"{json.dumps(item.get('immutable_identity', {}), ensure_ascii=False)}; "
                f"story costume: {item.get('story_costume', '')}"
            )



def validate_scene_image_set(scenes_dir, expected_count=50, expected_size=None):
    """Check scene image integrity, resolution, blank outputs, and near-duplicates."""
    scenes_dir = Path(scenes_dir)
    paths = sorted(scenes_dir.glob("scene_*.png"))
    if len(paths) != expected_count:
        raise ValueError(f"Image quality gate: expected {expected_count} scene images; found {len(paths)}.")
    hashes = {}
    for path in paths:
        try:
            with Image.open(path) as opened:
                opened.verify()
            with Image.open(path) as opened:
                image = opened.convert("RGB")
                if expected_size and image.size != tuple(expected_size):
                    raise ValueError(f"Image quality gate: {path.name} is {image.size}; expected {tuple(expected_size)}.")
                if min(image.size) < 1024:
                    raise ValueError(f"Image quality gate: {path.name} is below 1024 pixels on one side.")
                if path.stat().st_size < 1024:
                    raise ValueError(f"Image quality gate: {path.name} is suspiciously small.")
                gray = image.convert("L")
                deviation = ImageStat.Stat(gray).stddev[0]
                if deviation < 5.0:
                    raise ValueError(f"Image quality gate: {path.name} appears blank or nearly uniform.")
                tiny = gray.resize((16, 16), Image.Resampling.LANCZOS)
                pixels = list(tiny.getdata())
                average = sum(pixels) / len(pixels)
                image_hash = sum((1 << index) for index, value in enumerate(pixels) if value >= average)
                for prior_name, prior_hash in hashes.items():
                    if (image_hash ^ prior_hash).bit_count() <= 2:
                        raise ValueError(
                            f"Image quality gate: {path.name} is nearly identical to {prior_name}."
                        )
                hashes[path.name] = image_hash
        except (OSError, ValueError) as exc:
            if str(exc).startswith("Image quality gate:"):
                raise
            raise ValueError(f"Image quality gate: {path.name} is not a valid readable PNG: {exc}") from exc
    return {
        "checked_images": len(paths),
        "dimensions": list(expected_size) if expected_size else None,
        "automated_checks": [
            "valid PNG files",
            "expected image dimensions",
            "minimum resolution",
            "non-blank image content",
            "near-duplicate image detection"
        ],
        "character_consistency_verified": False,
        "visual_story_match_verified": False,
        "human_visual_review_required": True,
        "limitations": (
            "These automated checks do not judge whether each image matches its scene, "
            "keeps faces and costumes consistent, or has strong composition. Review the "
            "contact sheet and all 50 page images before publication."
        )
    }


def load_character_library():
    """Load permanent visual identities and optional local reference images."""
    models = []
    models_dir = CHARACTER_LIBRARY_ROOT / "models"
    if not models_dir.exists():
        return models
    for identity_path in sorted(models_dir.glob("*/identity.json")):
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            visual_id = str(identity.get("visual_id") or identity_path.parent.name).strip()
            if visual_id:
                identity["_reference_path"] = str(identity_path.parent / identity.get("reference_image", "reference.png"))
                models.append(identity)
        except (OSError, ValueError, TypeError):
            continue
    return models


def permanent_visual_slot_catalog():
    """Expose only neutral slot IDs and immutable physical identity facts to story planning."""
    return [
        {
            "slot": f"Character {int(str(model.get('visual_id', 'visual_000').split('_')[-1]))}",
            "visual_id": model.get("visual_id"),
            "immutable_identity": model.get("immutable_identity"),
        }
        for model in load_character_library()
    ]


def library_reference_path(visual_id):
    for model in load_character_library():
        if model.get("visual_id") == visual_id:
            path = Path(model["_reference_path"])
            return path if path.is_file() and path.stat().st_size > 1024 else None
    return None


def previous_story_character_names():
    names = set()
    cast_dir = CHARACTER_LIBRARY_ROOT / "story_cast"
    if not cast_dir.exists():
        return []
    for cast_path in cast_dir.glob("*.json"):
        try:
            data = json.loads(cast_path.read_text(encoding="utf-8"))
            names.update(str(item.get("name", "")).strip() for item in data.get("characters", []) if item.get("name"))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(name for name in names if name)


def save_story_cast(story, workdir):
    if not workdir:
        return
    cast_dir = CHARACTER_LIBRARY_ROOT / "story_cast"
    cast_dir.mkdir(parents=True, exist_ok=True)
    cast_path = cast_dir / f"{Path(workdir).name}.json"
    data = {
        "book_title": story.get("title", ""),
        "scheduled_folder": Path(workdir).name,
        "characters": [
            {"name": item.get("name", ""), "visual_id": item.get("visual_id"),
             "role": item.get("role", ""), "importance": item.get("importance", "")}
            for item in story.get("character_registry", [])
        ]
    }
    cast_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def reference_guidance_for_images(reference_images):
    """Describe style and physical-identity references."""
    if not reference_images:
        return (
            "No reference images are supplied. Follow the visual style and character "
            "descriptions in the prompt exactly. "
        )
    guidance = [
        "Use reference image 0 only for the illustration rendering style and colour treatment. "
    ]
    for index, image_path in enumerate(reference_images[1:], start=1):
        image_path = Path(image_path)
        character_name = (image_path.parent.name if image_path.stem == "reference" else image_path.stem).replace("_", " ").strip()
        guidance.append(
            f"Use reference image {index} only as the physical-identity guide for {character_name}: preserve face, facial proportions, skin tone, hair identity, eye shape/colour, body proportions and permanent marks. Ignore reference clothing and accessories, and also ignore its pose, gaze, camera angle, composition and facial expression; follow the new scene direction instead. Never merge identities. "
        )
    if len(reference_images) == 1:
        guidance.append(
            "Follow the character descriptions in the prompt; do not invent a character identity from the style reference. "
        )
    else:
        guidance.append(
            "Include only characters explicitly present in this scene and preserve each identity exactly. "
        )
    return "".join(guidance)

def cf_headers():
    return {"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"}

def cf_text(prompt, max_tokens=7000):
    if not CF_ACCOUNT or not CF_TOKEN:
        raise RuntimeError("Cloudflare credentials missing. Edit the .env file first.")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{TEXT_MODEL}"
    r = requests.post(url, headers=cf_headers(), json={
        "messages": [
            {"role": "system", "content": "You are a careful children's and general fiction story editor. Follow requested JSON schema exactly. Do not include markdown fences."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens
    }, timeout=180)
    if not r.ok:
        raise RuntimeError(f"Cloudflare text request failed ({r.status_code}): {r.text[:700]}")
    data = r.json()
    result = data.get("result", {})
    text = result.get("response") or result.get("output") or result.get("text")
    if not text and isinstance(result, dict):
        # Some model responses expose a chat-style shape.
        choices = result.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")
    if not text:
        raise RuntimeError("Text model response did not contain text. Check the selected text model's API response format.")

    # Cloudflare models may return either plain text or a structured object.
    # Normalize common nested response shapes before parsing the story JSON.
    if isinstance(text, dict):
        for key in ("response", "output", "text", "content"):
            candidate = text.get(key)
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                break
        else:
            text = json.dumps(text, ensure_ascii=False)
    elif isinstance(text, list):
        text = json.dumps(text, ensure_ascii=False)
    elif not isinstance(text, str):
        text = str(text)

    return text.strip()

def _parse_json_once(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The text model did not return a JSON object.")
    candidate = text[start:end + 1]
    try:
        # Small instruction models sometimes emit literal line breaks or tabs
        # inside JSON string values. Accept those control characters rather than
        # discarding an otherwise complete story/outline response.
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        # Repair the common trailing-comma mistake before asking the model again.
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(candidate, strict=False)

def extract_json(text, repair_context="story data"):
    try:
        return _parse_json_once(text)
    except (ValueError, json.JSONDecodeError) as original_error:
        # Try a local structural JSON repair before spending another model call.
        try:
            from json_repair import repair_json
            locally_repaired = repair_json(text, return_objects=True)
            if isinstance(locally_repaired, (dict, list)):
                return locally_repaired
        except Exception:
            pass
        repair_prompt = (
            "Repair the malformed JSON below. Return ONLY one valid JSON object. "
            "Preserve all keys and story content. Do not summarize or use markdown. "
            "Context: " + repair_context + ".\n\n" + text
        )
        repaired = cf_text(repair_prompt, max_tokens=6000)
        try:
            return _parse_json_once(repaired)
        except Exception as repair_error:
            raise ValueError(
                "JSON generation failed and automatic repair could not validate it. "
                f"Original error: {original_error}. Repair error: {repair_error}"
            ) from repair_error

DEFAULT_CHARACTER_POOL = """
PERMANENT VISUAL MODEL LIBRARY — neutral slots only:
Character 1 = visual_001; Character 2 = visual_002; Character 3 = visual_003; Character 4 = visual_004;
Character 5 = visual_005; Character 6 = visual_006; Character 7 = visual_007; Character 8 = visual_008;
Character 9 = visual_009; Character 10 = visual_010; Character 11 = visual_011; Character 12 = visual_012.
These slot numbers and visual IDs are technical identity references, NOT character names or story identities.
The AI must independently decide which zero-to-twelve visual models, if any, genuinely serve this book.
Only preserve a selected model's recognisable physical identity, species (where applicable), anatomy, colours and permanent marks.
Invent every selected character's name, personality, history, relationships, job, profession, role, rank, designation, social status, costume and (in fantasy only) powers and abilities for the current story.
Never import a model's old name, personality, occupation, costume, role or powers. Keep each new story identity stable throughout its own book.
Real-world stories must not select fantasy creature models and must contain no supernatural elements.
"""

STYLE_LOCK = (
    "Unified 2D cinematic anime realism: hand-painted feature-film illustration, "
    "semi-realistic adult anatomy and expressive faces, natural proportions, "
    "consistent fine linework, painterly cel shading, detailed believable materials "
    "and environments, controlled cinematic colour grading. Never 3D CGI, chibi, "
    "childish cartoon, photorealism, or a mixture of unrelated art styles."
)

def page_text_word_count_valid(text):
    """Publication page text must stay inside the exact 40–55 word range."""
    return 40 <= len(str(text).split()) <= 55


def normalize_dialogue_line(line):
    """Normalize dialogue for duplicate detection without altering the displayed text."""
    text = str(line or "").casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_unwanted_non_english_script(text):
    """Flag scripts that should not appear in an English-only storybook."""
    source = str(text or "")
    script_patterns = {
        "Arabic": r"[\\u0600-\\u06FF]",
        "Cyrillic": r"[\\u0400-\\u04FF]",
        "CJK": r"[\\u3400-\\u4DBF\\u4E00-\\u9FFF]",
        "Hangul": r"[\\uAC00-\\uD7AF]",
        "Japanese kana": r"[\\u3040-\\u30FF]",
        "Devanagari": r"[\\u0900-\\u097F]",
        "Thai": r"[\\u0E00-\\u0E7F]",
        "Greek": r"[\\u0370-\\u03FF]",
    }
    return [name for name, pattern in script_patterns.items() if re.search(pattern, source)]


def validate_story_text_integrity(scenes):
    """Publication gate for repeated page text and unexpected foreign scripts."""
    seen_sentences = {}
    normalized_pages = []
    foreign = []
    for scene in scenes:
        number = int(scene.get("number", 0))
        fields = [scene.get("heading", ""), scene.get("text", "")]
        fields.extend(item.get("line", "") for item in scene.get("dialogue", []) if isinstance(item, dict))
        combined = " ".join(str(x or "") for x in fields)
        scripts = detect_unwanted_non_english_script(combined)
        if scripts:
            foreign.append(f"scene {number}: {', '.join(scripts)} script detected")
        normalized = re.sub(r"[^a-z0-9 ]", " ", combined.casefold())
        normalized = re.sub(r"\\s+", " ", normalized).strip()
        normalized_pages.append(normalized)
        sentences = [
            re.sub(r"[^a-z0-9 ]", " ", sentence.casefold()).strip()
            for sentence in re.split(r"(?<=[.!?])\\s+", combined)
        ]
        for sentence in sentences:
            sentence = re.sub(r"\\s+", " ", sentence).strip()
            if len(sentence.split()) < 5:
                continue
            if sentence in seen_sentences:
                previous = seen_sentences[sentence]
                raise ValueError(
                    f"Repeated page text rejected: scene {number} repeats a sentence from "
                    f"scene {previous}: {sentence!r}."
                )
            seen_sentences[sentence] = number
    if foreign:
        raise ValueError("Unexpected non-English script rejected: " + "; ".join(foreign))
    for i, first in enumerate(normalized_pages):
        if len(first.split()) < 12:
            continue
        for j in range(i + 1, len(normalized_pages)):
            second = normalized_pages[j]
            if len(second.split()) < 12:
                continue
            similarity = difflib.SequenceMatcher(None, first, second).ratio()
            if similarity >= 0.88:
                raise ValueError(
                    f"Near-duplicate page text rejected: scenes {i + 1} and {j + 1} "
                    f"(similarity {similarity:.2f})."
                )
    return {"passed": True, "unique_sentences": len(seen_sentences), "pages_checked": len(scenes)}


def validate_unique_story_dialogue(scenes):
    """Reject repeated spoken lines across pages, including punctuation/case variants."""
    seen = {}
    for scene in scenes:
        number = int(scene.get("number", 0))
        dialogue = scene.get("dialogue", [])
        if not isinstance(dialogue, list):
            raise ValueError(f"Scene {number} dialogue must be a list.")
        for item in dialogue:
            if not isinstance(item, dict):
                raise ValueError(f"Scene {number} contains malformed dialogue.")
            line = str(item.get("line", "")).strip()
            speaker = str(item.get("speaker", "")).strip()
            if not line or not speaker:
                raise ValueError(f"Scene {number} contains blank dialogue speaker/line.")
            key = normalize_dialogue_line(line)
            if not key:
                raise ValueError(f"Scene {number} contains dialogue with no usable words.")
            if key in seen:
                previous_scene, previous_speaker, previous_line = seen[key]
                raise ValueError(
                    f"Repeated dialogue rejected: scene {number} ({speaker}) repeats "
                    f"scene {previous_scene} ({previous_speaker}) line: {line!r}."
                )
            seen[key] = (number, speaker, line)
    return {"dialogue_lines": len(seen), "unique_dialogue_lines": len(seen), "passed": True}


def simple_english_issues(text):
    """Return deterministic readability problems for very easy storybook prose."""
    source = str(text or "").strip()
    words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", source)
    sentences = [
        sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", source)
        if sentence.strip()
    ]
    issues = []
    if not 3 <= len(sentences) <= 5:
        issues.append(f"expected 3-5 short sentences, found {len(sentences)}")
    sentence_lengths = [
        len(re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)?", sentence))
        for sentence in sentences
    ]
    if sentence_lengths and max(sentence_lengths) > 18:
        issues.append(f"longest sentence has {max(sentence_lengths)} words (maximum 18)")
    if sentence_lengths and sum(sentence_lengths) / len(sentence_lengths) > 15:
        issues.append("average sentence length is over 15 words")
    difficult_words = sorted({
        word.casefold() for word in words if len(word) > 12
    })
    if len(difficult_words) > 2:
        issues.append("too many long or difficult words: " + ", ".join(difficult_words[:6]))
    return issues


def repair_scene_page_text(scene, plan, number):
    """Repair page prose after any cast-name normalization and enforce the print word limit."""
    current_text = str(scene.get("text", ""))
    if page_text_word_count_valid(current_text) and not simple_english_issues(current_text):
        return scene
    source_text = str(scene.get("text", "")).strip()
    last_error = None
    for attempt in range(1, 6):
        prompt = (
            "Rewrite this illustrated storybook page as polished narrative prose of 45-50 words "
            "(never fewer than 40 or more than 55). Use 3-5 short sentences, with no sentence over "
            "18 words and an average of no more than 15 words. Use common everyday words; avoid long "
            "or difficult vocabulary. Preserve the exact events, facts, registered character names, "
            "emotional beat, and continuity. Do not add a new event, heading, label, or explanation. "
            "Do not rename or remove any registered character. Count the words before answering. "
            "Return only the page paragraph.\\n\\n"
            f"Book: {plan.get('title', 'Untitled')}\\n"
            f"Registered cast: {json.dumps([x.get('name') for x in plan.get('character_registry', []) if isinstance(x, dict)])}\\n"
            f"Principal villain: {plan.get('principal_villain', '')}\\n"
            f"Scene {number}: {scene.get('heading', '')}\\n"
            f"Required length: 45-50 words; absolute allowed range is 40-55 words.\\n"
            f"Original page text ({len(source_text.split())} words): {source_text}"
        )
        candidate = cf_text(prompt, max_tokens=700).strip().strip('"').strip()
        if page_text_word_count_valid(candidate) and not simple_english_issues(candidate):
            scene["text"] = candidate
            logging.info(
                "Repaired scene %s final page text to %s words after cast normalization.",
                number, len(candidate.split())
            )
            return scene
        last_error = (
            f"repair {attempt} returned {len(candidate.split())} words; "
            + "; ".join(simple_english_issues(candidate))
        )
    raise ValueError(
        f"Scene {number} page text could not be repaired to 40-55 words after cast normalization "
        f"({last_error})."
    )
def normalize_registered_outline_aliases(outline, registry):
    """Expand unambiguous first-name, surname, or nickname mentions to canonical cast names.

    This is deliberately limited to tokens unique across the locked registry. It prevents
    an otherwise correct outline from failing cast coverage merely because the model uses
    a registered character's short name (for example, Hawk) instead of the full name.
    """
    aliases = {}
    stop_tokens = {"the", "and", "for", "with", "from", "into", "onto", "sir", "lady", "lord"}
    for item in registry if isinstance(registry, list) else []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("name", "")).strip()
        if not canonical:
            continue
        for token in re.findall(r"[A-Za-z]+", canonical):
            if len(token) >= 3 and token.casefold() not in stop_tokens:
                aliases.setdefault(token.casefold(), set()).add(canonical)
    unique_aliases = {
        token: next(iter(names))
        for token, names in aliases.items()
        if len(names) == 1
    }
    if not isinstance(outline, list):
        return outline
    for row in outline:
        if not isinstance(row, dict):
            continue
        beat = str(row.get("beat", ""))
        # Protect already-canonical full names before expanding individual tokens.
        protected = {}
        for index, item in enumerate(registry if isinstance(registry, list) else []):
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("name", "")).strip()
            if canonical and re.search(r"(?<![A-Za-z])" + re.escape(canonical) + r"(?![A-Za-z])", beat, re.IGNORECASE):
                marker = f"ZZCANONICALCAST{index}ZZ"
                beat = re.sub(r"(?<![A-Za-z])" + re.escape(canonical) + r"(?![A-Za-z])", marker, beat, flags=re.IGNORECASE)
                protected[marker] = canonical
        for token, canonical in sorted(unique_aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
            beat = re.sub(r"(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])", canonical, beat, flags=re.IGNORECASE)
        for marker, canonical in protected.items():
            beat = beat.replace(marker, canonical)
        row["beat"] = beat
    return outline


def missing_registered_outline_names(outline, registry):
    """Return cast names that the outline failed to use exactly as registered."""
    normalize_registered_outline_aliases(outline, registry)
    outline_text = " ".join(
        str(item.get("beat", "")) for item in outline if isinstance(item, dict)
    ).casefold()
    return [
        str(item.get("name", "")).strip()
        for item in registry if isinstance(item, dict)
        and str(item.get("name", "")).strip()
        and str(item.get("name", "")).strip().casefold() not in outline_text
    ]


def unregistered_named_entities_in_outline(outline, registry):
    """Find named people introduced by the outline who are absent from the locked cast."""
    known_names = [
        str(item.get("name", "")).strip()
        for item in registry if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    known = {name.casefold() for name in known_names}
    text = " ".join(
        str(item.get("beat", "")) for item in outline if isinstance(item, dict)
    )
    patterns = (
        r"\b(?:named|called|known as|known only as|whose name is)\s+[\"']?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
        r"\b(?i:chief|detective|officer|dr\.?|mr\.?|mrs\.?|ms\.?|captain|agent|judge|doctor|professor|inspector|sergeant|lieutenant|mayor|sheriff)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"\b(?:victim|partner|informant|businessman|socialite|hacker|bartender|owner|thief|leader|mentor|figure|guard|suspect|witness|police chief)\s*,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        r"\b(?:against|with|from|to|by|for|near|beside|behind|before|after|met|meet|saw|calls?|called|asks?|asked|tells?|told|warns?|warned|follows?|followed|visits?|visited|questions?|questioned|approaches?|approached|confronts?|confronted|contacts?|contacted|knows|knew)\s+([A-Z][a-z]+)\b",
    )
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = re.sub(r"['\".,;:!?]+$", "", match.group(1)).strip()
            # Models sometimes repeat a first name as a malformed full name.
            candidate = re.sub(
                r"\b([A-Z][a-z]+)\s+\1\b", r"\1", candidate
            )
            is_registered_alias = any(
                re.search(
                    r"(?<![A-Za-z])" + re.escape(candidate) + r"(?![A-Za-z])",
                    registered_name, flags=re.IGNORECASE
                )
                for registered_name in known_names
            )
            if candidate and candidate.casefold() not in known and not is_registered_alias:
                found.append((match.start(), candidate))
    ordered = []
    for _, candidate in sorted(found, key=lambda item: item[0]):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def sanitize_outline_unregistered_names(outline, registry, extra_names=None):
    """Map clear aliases to canonical cast names and anonymize incidental named extras."""
    # Small text models occasionally duplicate a first name (for example, "Sophia Sophia").
    # Treat this as an accidental unnamed extra rather than a new character identity.
    for item in outline:
        if isinstance(item, dict):
            beat = str(item.get("beat", ""))
            item["beat"] = re.sub(
                r"\b([A-Z][a-z]+)\s+\1\b", r"\1", beat
            )
    unknown = list(dict.fromkeys(
        unregistered_named_entities_in_outline(outline, registry) + list(extra_names or [])
    ))
    unknown = [
        name for name in unknown
        if not any(
            name.casefold() != other.casefold() and name.casefold() in other.casefold()
            for other in unknown
        )
    ]
    registered = [
        str(item.get("name", "")).strip()
        for item in registry if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    for name in unknown:
        title_match = re.match(
            r"^(police chief|chief|detective|officer|dr\.?|mr\.?|mrs\.?|ms\.?|captain|agent|judge|doctor|professor|inspector|sergeant|lieutenant|mayor|sheriff)\s+(.+)$",
            name, flags=re.IGNORECASE
        )
        title_label = None
        candidate_source = name
        if title_match:
            title = title_match.group(1).casefold().replace(".", "")
            candidate_source = title_match.group(2).strip()
            title_label = {
                "police chief": "the police chief", "chief": "the chief",
                "detective": "the detective", "officer": "the officer",
                "dr": "the doctor", "mr": "the man", "mrs": "the woman",
                "ms": "the woman", "captain": "the captain", "agent": "the agent",
                "judge": "the judge", "doctor": "the doctor", "professor": "the professor",
                "inspector": "the inspector", "sergeant": "the sergeant",
                "lieutenant": "the lieutenant", "mayor": "the mayor", "sheriff": "the sheriff"
            }.get(title, "the person")
        words = re.findall(r"[A-Za-z]+", candidate_source.casefold())
        candidates = set()
        for registered_name in registered:
            known_words = re.findall(r"[A-Za-z]+", registered_name.casefold())
            if any(
                word == known
                or (len(word) >= 4 and len(known) >= 4
                    and difflib.SequenceMatcher(None, word, known).ratio() >= 0.80)
                for word in words for known in known_words
            ):
                candidates.add(registered_name)
        canonical = next(iter(candidates)) if len(candidates) == 1 else None
        escaped = re.escape(name)
        for item in outline:
            if not isinstance(item, dict):
                continue
            beat = str(item.get("beat", ""))
            if title_match:
                beat = re.sub(
                    r"(?<![A-Za-z])" + escaped + r"(?![A-Za-z])",
                    canonical or title_label, beat, flags=re.IGNORECASE
                )
                item["beat"] = re.sub(r"\s{2,}", " ", beat).strip()
                continue
            if canonical:
                beat = re.sub(
                    r"(?<![A-Za-z])" + escaped + r"(?![A-Za-z])",
                    canonical, beat, flags=re.IGNORECASE
                )
                for title_prefix in (
                    "police chief", "chief", "detective", "officer", "dr\\.?", "mr\\.?",
                    "mrs\\.?", "ms\\.?", "captain", "agent", "judge", "doctor",
                    "professor", "inspector", "sergeant", "lieutenant", "mayor", "sheriff"
                ):
                    beat = re.sub(
                        r"\b" + title_prefix + r"\s+" + re.escape(canonical),
                        canonical, beat, flags=re.IGNORECASE
                    )
            else:
                # Remove invented aliases after role phrases, leaving the role unnamed.
                beat = re.sub(
                    r",?\s+(?:named|called|known as|known only as|whose name is)\s+[\"']?" + escaped + r"[\"']?,?",
                    "", beat, flags=re.IGNORECASE
                )
                role_patterns = (
                    (r"\bpolice\s+chief\s+" + escaped, "the police chief"),
                    (r"\bchief\s+" + escaped, "the chief"),
                    (r"\bdetective\s+" + escaped, "the detective"),
                    (r"\bofficer\s+" + escaped, "the officer"),
                    (r"\bdr\.?\s+" + escaped, "the doctor"),
                    (r"\bmr\.?\s+" + escaped, "the man"),
                    (r"\bmrs\.?\s+" + escaped, "the woman"),
                    (r"\bprofessor\s+" + escaped, "the professor"),
                    (r"\bcaptain\s+" + escaped, "the captain"),
                    (r"\bagent\s+" + escaped, "the agent"),
                    (r"\bjudge\s+" + escaped, "the judge"),
                    (r"\bsergeant\s+" + escaped, "the sergeant"),
                    (r"\blieutenant\s+" + escaped, "the lieutenant"),
                    (r"\bmayor\s+" + escaped, "the mayor"),
                    (r"\bsheriff\s+" + escaped, "the sheriff"),
                )
                for pattern, replacement in role_patterns:
                    beat = re.sub(pattern, replacement, beat, flags=re.IGNORECASE)
                beat = re.sub(
                    r"(\b(?:victim|partner|informant|businessman|socialite|hacker|bartender|owner|"
                    r"thief|leader|mentor|figure|guard|suspect|witness)\b)\s*,\s*" + escaped + r",?",
                    r"\1", beat, flags=re.IGNORECASE
                )
                beat = re.sub(
                    r"(?<![A-Za-z])" + escaped + r"(?![A-Za-z])",
                    "the person", beat, flags=re.IGNORECASE
                )
            item["beat"] = re.sub(r"\s{2,}", " ", beat).strip()
    # Final deterministic sweep: if an unknown full name survives contextual
    # cleanup, replace every exact occurrence rather than retrying the model.
    for _ in range(2):
        remaining = unregistered_named_entities_in_outline(outline, registry)
        if not remaining:
            break
        for unknown_name in remaining:
            escaped_unknown = re.escape(unknown_name)
            for item in outline:
                if not isinstance(item, dict):
                    continue
                beat = str(item.get("beat", ""))
                beat = re.sub(
                    r"(?<![A-Za-z])" + escaped_unknown + r"(?![A-Za-z])",
                    "the person", beat, flags=re.IGNORECASE
                )
                item["beat"] = re.sub(r"\s{2,}", " ", beat).strip()
    # Expand a unique surname/first-name alias to its exact registered full name.
    combined_text = " ".join(
        str(item.get("beat", "")) for item in outline if isinstance(item, dict)
    )
    title_words = {
        "detective", "officer", "doctor", "professor", "captain", "agent",
        "judge", "inspector", "sergeant", "lieutenant", "mayor", "sheriff",
        "chief", "police", "mr", "mrs", "ms", "dr"
    }
    for character in registry:
        if not isinstance(character, dict):
            continue
        full_name = str(character.get("name", "")).strip()
        if not full_name or full_name.casefold() in combined_text.casefold():
            continue
        tokens = [
            token for token in re.findall(r"[A-Za-z]+", full_name)
            if len(token) >= 4 and token.casefold() not in title_words
        ]
        for token in tokens:
            other_owners = [
                item for item in registry if isinstance(item, dict)
                and str(item.get("name", "")).strip() != full_name
                and token.casefold() in str(item.get("name", "")).casefold().split()
            ]
            if other_owners:
                continue
            if re.search(r"(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])",
                         combined_text, flags=re.IGNORECASE):
                for item in outline:
                    if not isinstance(item, dict):
                        continue
                    beat = str(item.get("beat", ""))
                    beat = re.sub(
                        r"(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])",
                        full_name, beat, flags=re.IGNORECASE
                    )
                    item["beat"] = re.sub(r"\s{2,}", " ", beat).strip()
                combined_text = " ".join(
                    str(item.get("beat", "")) for item in outline if isinstance(item, dict)
                )
                break
    return outline


def sanitize_scene_unregistered_cast(scene, registry):
    """Anonymize incidental unregistered people and keep only canonical cast entries."""
    fields = ("heading", "text", "visual_prompt", "composition", "action_pose", "gaze", "expression")
    known = {
        str(item.get("name", "")).strip()
        for item in registry if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    raw_names = scene.get("characters_present", [])
    normalized, aliases = normalize_scene_cast_names(raw_names, registry, return_aliases=True)
    if aliases:
        apply_scene_cast_aliases(scene, aliases)
    unknown_cast = [name for name in normalized if name not in known]
    for field in fields:
        value = scene.get(field)
        if isinstance(value, str) and value:
            temporary = [{"beat": value}]
            sanitize_outline_unregistered_names(temporary, registry, extra_names=unknown_cast)
            scene[field] = temporary[0]["beat"]
    if unknown_cast:
        logging.warning(
            "Anonymized incidental unregistered scene cast entries: %s",
            unknown_cast
        )
    scene["characters_present"] = [name for name in normalized if name in known]
    return scene


def ensure_concrete_story_costumes(plan):
    """Give every character one concrete, repeatable outfit for the entire book."""
    registry = plan.get("character_registry", [])
    if not registry:
        raise ValueError("Cannot lock costumes without a character registry.")
    prompt = (
        "You are a professional costume designer for a cinematic storybook. Create ONE fixed, "
        "concrete outfit for each registered character. Every outfit must name actual garments, "
        "colours, footwear, and any small accessories. Avoid vague phrases such as 'police uniform', "
        "'journalist attire', 'ordinary clothes', 'dark clothing', 'disguise', or 'deception'. "
        "Each character wears this exact same outfit throughout the entire book. No costume changes, "
        "disguises, alternate looks, or unexplained accessories. Do not change character names, roles, "
        "species, or physical identities. Return ONLY JSON: {costumes:[{name,story_costume}]}. "
        "Each story_costume should be 10–24 simple words.\\n\\n"
        + json.dumps({
            "genre": plan.get("genre", ""),
            "title": plan.get("title", ""),
            "character_registry": registry
        }, ensure_ascii=False)
    )
    response = extract_json(cf_text(prompt, max_tokens=1800), "fixed character costume design")
    costumes = response.get("costumes", []) if isinstance(response, dict) else []
    mapping = {
        str(item.get("name", "")).strip(): str(item.get("story_costume", "")).strip()
        for item in costumes if isinstance(item, dict)
    }
    expected_names = [str(item.get("name", "")).strip() for item in registry]
    if len(mapping) != len(expected_names) or any(name not in mapping for name in expected_names):
        raise ValueError("Costume designer did not return one exact costume for every registered character.")
    garment_terms = (
        "jacket", "coat", "shirt", "blouse", "trousers", "pants", "jeans", "skirt",
        "dress", "boots", "shoes", "uniform", "blazer", "sweater", "vest", "robe",
        "tunic", "belt", "gloves", "scarf", "overalls", "suit", "waistcoat"
    )
    def fallback_costume(item):
        role = str(item.get("role", "")).casefold()
        importance = str(item.get("importance", "")).casefold()
        species = str(item.get("species", "human")).casefold()
        if species not in ("human", "human being", "person"):
            return (
                "A dark woven travel cloak secured by a bronze clasp, layered leather straps, "
                "reinforced bracers, sturdy leg wraps, and a weathered utility satchel."
            )
        if any(term in role for term in ("detective", "police", "officer", "inspector", "sheriff")):
            return (
                "A dark navy field jacket with a silver badge over a pale blue shirt, "
                "charcoal trousers, black leather boots, and a black duty belt."
            )
        if any(term in role for term in ("journalist", "reporter", "writer")):
            return (
                "An olive blazer over a cream blouse, dark straight-leg trousers, "
                "brown leather ankle boots, and a tan leather shoulder bag."
            )
        if any(term in role for term in ("doctor", "nurse", "medic")):
            return (
                "A clean white lab coat over a pale blue shirt, charcoal trousers, "
                "dark leather shoes, and a clipped identification badge."
            )
        if importance == "villain" or "antagonist" in role:
            return (
                "A charcoal wool overcoat over a black shirt, dark tailored trousers, "
                "polished black leather boots, and a plain steel ring."
            )
        if importance == "protagonist":
            return (
                "A dark grey canvas jacket over a plain cotton shirt, navy trousers, "
                "black leather shoes, and a simple leather wristwatch."
            )
        return (
            "A deep charcoal jacket over a light grey cotton shirt, dark navy trousers, "
            "black leather shoes, and a simple brown leather wristwatch."
        )

    for item in registry:
        name = str(item.get("name", "")).strip()
        costume = mapping[name]
        if not 10 <= len(costume.split()) <= 24 or sum(term in costume.casefold() for term in garment_terms) < 2:
            logging.warning("Costume response for %s was too vague; applying a concrete role-based outfit.", name)
            costume = fallback_costume(item)
        if not 10 <= len(costume.split()) <= 24 or sum(term in costume.casefold() for term in garment_terms) < 2:
            raise ValueError(f"Could not create a concrete fixed costume for {name}.")
        item["story_costume"] = costume
        item["visual_bible"] = (
            f"{name}: immutable physical identity "
            f"{json.dumps(item.get('immutable_identity', {}), ensure_ascii=False)}; "
            f"fixed story costume: {costume}"
        )
    plan["character_bible"] = "\n\n".join(
        f"{item.get('name', '')} — {item.get('role', '')}: {item.get('visual_bible', '')}"
        for item in registry
    )
    return plan




def validate_outline_cast_distribution(outline, registry):
    """Ensure every registered role has a meaningful, recurring presence in the plot."""
    normalize_registered_outline_aliases(outline, registry)
    appearances = {}
    for item in registry:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        appearances[name] = [
            row.get("number") for row in outline if isinstance(row, dict)
            and name.casefold() in str(row.get("beat", "")).casefold()
        ]
    for item in registry:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        role = str(item.get("importance", "")).casefold()
        scenes = appearances.get(name, [])
        if role == "protagonist" and len(scenes) < 8:
            raise ValueError(f"Protagonist {name} appears in only {len(scenes)} outline beats; at least 8 are required.")
        if role == "supporting" and len(scenes) < 3:
            raise ValueError(f"Supporting character {name} appears in only {len(scenes)} outline beats; at least 3 are required.")
        if role == "villain":
            if len(scenes) < 5:
                raise ValueError(f"Principal villain {name} appears in only {len(scenes)} outline beats; at least 5 are required.")
            if not any(isinstance(n, int) and n <= 15 for n in scenes):
                raise ValueError(f"Principal villain {name} must be foreshadowed within the first 15 scenes.")
            if not any(isinstance(n, int) and n >= 41 for n in scenes):
                raise ValueError(f"Principal villain {name} must remain active in the final ten scenes.")
    return outline


def validate_principal_villain_foreshadowing(outline, principal_villain, max_scene=15):
    """Require the registered antagonist to be seeded early, not introduced as a late twist."""
    villain = str(principal_villain or "").strip()
    if not villain:
        raise ValueError("A registered principal villain is required.")
    early_beats = [
        item for item in outline
        if isinstance(item, dict) and isinstance(item.get("number"), int)
        and 1 <= item["number"] <= max_scene
    ]
    if not any(
        villain.casefold() in str(item.get("beat", "")).casefold()
        for item in early_beats
    ):
        raise ValueError(
            f"The registered principal villain {villain} must be foreshadowed by full name "
            f"within scenes 1-{max_scene}."
        )
    return outline


def validate_outline_ending_progression(rows):
    """Reject endings padded with too many awards, praise, and repeated aftermath beats."""
    if not isinstance(rows, list) or len(rows) < 15:
        return
    ending = rows[-15:]
    epilogue_patterns = (
        r"\b(?:praised|hailed|celebrated|celebration|honou?red|awarded|recognized|recognised)\b",
        r"\b(?:remembered|cautionary tale|reminder of the importance)\b",
        r"\b(?:bestseller|book is published|research is published|leading authority)\b",
        r"\b(?:exhibition (?:is )?(?:extended|a huge success)|artwork is praised)\b",
        r"\b(?:city.{0,35}(?:forever changed|revitalized|comes together))\b",
        r"\b(?:career.{0,35}(?:success|recognition)|receiv(?:e|es|ed) (?:a )?(?:medals?|awards?))\b",
    )
    filler_numbers = []
    for item in ending:
        beat = str(item.get("beat", "")).casefold()
        matches = sum(bool(re.search(pattern, beat)) for pattern in epilogue_patterns)
        if matches:
            filler_numbers.append(item.get("number"))
    if len(filler_numbers) >= 4:
        raise ValueError(
            "Outline ending relies on too many epilogue-only praise, award, or aftermath beats "
            f"in scenes {filler_numbers}; replace them with distinct story actions and consequences."
        )


def validate_outline_rows(rows, registry, strict_repetition=True):
    """Validate outline shape, page-beat length, cast coverage, and extra named people."""
    if not isinstance(rows, list) or len(rows) != 50:
        raise ValueError(f"Outline must contain exactly 50 beats; received {len(rows) if isinstance(rows, list) else 'invalid data'}.")
    if any(not isinstance(item, dict) for item in rows):
        raise ValueError("Every outline beat must be an object.")
    if [item.get("number") for item in rows] != list(range(1, 51)):
        raise ValueError("Outline beats must be numbered sequentially from 1 through 50.")
    for item in rows:
        count = len(str(item.get("beat", "")).split())
        if not 8 <= count <= 45:
            raise ValueError(f"Outline beat {item['number']} has {count} words; required range is 8–45.")
    normalized = [
        re.sub(r"[^a-z0-9 ]", "", str(item.get("beat", "")).lower()).strip()
        for item in rows
    ]
    seen_beats = {}
    for index, beat in enumerate(normalized, start=1):
        if beat in seen_beats:
            first = seen_beats[beat]
            excerpt = str(rows[index - 1].get("beat", "")).strip()
            raise ValueError(
                f"Outline beats {first} and {index} repeat the same event: "
                + repr(excerpt[:180])
                + ". Rewrite the later beat as a new action that advances the plot."
            )
        seen_beats[beat] = index
    validate_outline_ending_progression(rows)
    if strict_repetition:
        for first_index in range(len(normalized)):
            for second_index in range(first_index + 1, len(normalized)):
                similarity = difflib.SequenceMatcher(
                    None, normalized[first_index], normalized[second_index]
                ).ratio()
                if similarity >= 0.86:
                    raise ValueError(
                        f"Outline beats {first_index + 1} and {second_index + 1} are near-identical "
                        f"(similarity {similarity:.2f})."
                    )
    rows = sanitize_outline_unregistered_names(rows, registry)
    missing = missing_registered_outline_names(rows, registry)
    unknown = unregistered_named_entities_in_outline(rows, registry)
    if missing or unknown:
        raise ValueError(
            "Outline cast mismatch after sanitization. Missing: "
            + ", ".join(missing) + "; unregistered: " + ", ".join(unknown)
        )
    return rows



def ground_editorial_issues(issues, outline):
    """Keep only reviewer findings tied to an exact quote in the cited outline beat."""
    supported = []
    unverified = []
    by_number = {
        item.get("number"): str(item.get("beat", ""))
        for item in outline if isinstance(item, dict)
    }
    for issue in issues if isinstance(issues, list) else [issues]:
        if not isinstance(issue, dict):
            unverified.append(str(issue))
            continue
        number = issue.get("scene_number", issue.get("number"))
        quote = str(issue.get("evidence_quote", "")).strip()
        reason = str(issue.get("reason", "")).strip()
        if number is None and quote:
            matches = [
                scene_number for scene_number, beat_text in by_number.items()
                if quote.casefold() in beat_text.casefold()
            ]
            if len(matches) == 1:
                number = matches[0]
        beat = by_number.get(number, "")
        if isinstance(number, int) and beat and quote and quote.casefold() in beat.casefold() and reason:
            supported.append({"number": number, "reason": reason, "evidence_quote": quote})
        else:
            unverified.append({"number": number, "reason": reason, "evidence_quote": quote})
    return supported, unverified



def synchronize_book_description(plan, outline):
    """Create KDP copy that accurately describes the final, locked outline."""
    registry = plan.get("character_registry", [])
    protagonist = next(
        (str(item.get("name", "")).strip() for item in registry
         if isinstance(item, dict) and item.get("importance") == "protagonist"),
        ""
    )
    villain = str(plan.get("principal_villain", "")).strip()
    prompt = (
        "Write the final Amazon book description in VERY easy English. Use 100-140 words and "
        "5-7 short, clear sentences. Describe ONLY events and facts explicitly supported by the "
        "locked 50-scene outline below. Name the protagonist exactly as registered and name the "
        "principal villain exactly as registered. Explain the central crime or threat, why it matters, "
        "the key clue trail, the danger, and how the main conflict is resolved. Do not invent murders, "
        "victims, characters, kidnappings, trials, sentences, or events that are not in the outline. "
        "Do not use vague filler such as 'a web of deceit' without explaining the actual conflict. "
        "Do not add a sequel cliffhanger. Return ONLY the description text, no heading or quotes.\\n\\n"
        + json.dumps({
            "title": plan.get("title", ""),
            "protagonist": protagonist,
            "principal_villain": villain,
            "character_registry": registry,
            "outline": outline
        }, ensure_ascii=False)
    )
    last = ""
    for attempt in range(1, 3):
        last = cf_text(prompt, max_tokens=420).strip()
        words = last.split()
        if (
            90 <= len(words) <= 155
            and protagonist.casefold() in last.casefold()
            and villain.casefold() in last.casefold()
        ):
            plan["description"] = last
            return last
        prompt += (
            "\\n\\nRETRY: The last description had " + str(len(words))
            + " words or omitted an exact registered name. Return 100-140 words, "
            "5-7 simple sentences, and include both exact names."
        )
    raise ValueError(
        "Could not create a grounded 100-140 word book description naming the protagonist "
        "and registered villain. Last response: " + last[:300]
    )


def review_story_outline(plan, outline, workdir=None):
    """Editorially check plot logic and the ending before spending resources on scenes."""
    prompt = (
        "You are a strict senior story editor. Audit this 50-beat story outline BEFORE scene writing. "
        "Judge cause-and-effect, a clear central mystery, useful clues, believable motives, escalating "
        "stakes, distinct events, character roles, principal-villain payoff, and a satisfying complete "
        "ending. Carefully audit the final 15 beats: each must add a distinct action, consequence, or "
        "meaningful emotional change. Do not accept a long list of praise, awards, career success, public "
        "celebrations, repeated exhibition updates, or repeated statements that the case changed the city "
        "as a substitute for story events. If the climax ends early and the remaining beats repeat an "
        "epilogue, score pacing and ending below 7. Reject contradictions, unexplained kidnappings/rescues, "
        "repeated events, villains who escape accountability, victims who remain unresolved, sacrifices "
        "that happen without a coherent story payoff, and final-scene cliffhangers that leave the main case unresolved. The exact registered "
        "principal villain must drive the central conflict and face a clear consequence. The story must be "
        "standalone and close its main mystery. Use very simple English. Score plot, continuity, pacing, "
        "character consistency, and ending from 0 to 10. Return ONLY JSON with passed, score (0–100), "
        "critical_issues and minor_issues as arrays of objects with scene_number, reason, and "
        "evidence_quote. Every evidence_quote MUST be copied verbatim from the exact numbered outline beat. "
        "Never report a scene number unless the quoted words occur in that scene. Never invent an event, "
        "kidnapping, shootout, court sentence, escape, character, or action that is not explicitly present. "
        "If an issue cannot be tied to an exact beat quote, do not report it. Include dimension_scores "
        "with exactly these numeric keys from 0 to 10: plot, continuity, pacing, character_consistency, ending. "
        "Set passed=false if any major contradiction, unresolved central mystery, weak ending, or any "
        "dimension below 7 remains. Do not inflate scores.\\n\\n"
        + json.dumps({
            "title": plan.get("title", ""),
            "description": plan.get("description", ""),
            "principal_villain": plan.get("principal_villain", ""),
            "character_registry": plan.get("character_registry", []),
            "outline": outline
        }, ensure_ascii=False)
    )
    report = extract_json(cf_text(prompt, max_tokens=1800), "strict pre-scene outline editorial review")
    if not isinstance(report, dict) or not isinstance(report.get("passed"), bool):
        raise ValueError("Outline reviewer returned an invalid report.")
    required = ("plot", "continuity", "pacing", "character_consistency", "ending")

    def valid_dimensions(candidate):
        dimensions = candidate.get("dimension_scores") if isinstance(candidate, dict) else None
        return isinstance(dimensions, dict) and all(
            key in dimensions and isinstance(dimensions[key], (int, float))
            and not isinstance(dimensions[key], bool) and 0 <= dimensions[key] <= 10
            for key in required
        )

    # Some model responses return the editorial verdict but omit the score object.
    # Request only the missing structured scorecard once; never infer or fabricate scores.
    if not valid_dimensions(report):
        score_prompt = (
            "Review the following 50-beat outline and return ONLY valid JSON with exactly "
            "one key: dimension_scores. It must contain numeric scores from 0 to 10 for "
            "plot, continuity, pacing, character_consistency, and ending. Score honestly; "
            "do not inflate.\nOUTLINE:\n"
            + json.dumps(outline, ensure_ascii=False)
        )
        scorecard = extract_json(
            cf_text(score_prompt, max_tokens=500),
            "outline reviewer scorecard recovery"
        )
        recovered = scorecard.get("dimension_scores") if isinstance(scorecard, dict) else None
        if isinstance(recovered, dict):
            report["dimension_scores"] = recovered
    if not valid_dimensions(report):
        raise ValueError("Outline reviewer returned missing or invalid dimension scores after one recovery attempt.")
    dimensions = report["dimension_scores"]
    score = round(sum(float(dimensions[key]) for key in required) / len(required) * 10, 1)
    critical, unverified_critical = ground_editorial_issues(
        report.get("critical_issues", []), outline
    )
    minor_issues, unverified_minor = ground_editorial_issues(
        report.get("minor_issues", []), outline
    )
    report["unverified_review_notes"] = unverified_critical + unverified_minor
    for key in required:
        if float(dimensions[key]) < 7:
            critical.append(f"Core outline dimension '{key}' scored below 7/10.")
    retained_critical = []
    for issue in critical:
        issue_text = str(issue)
        lower_issue = issue_text.casefold()
        explicitly_minor = any(phrase in lower_issue for phrase in (
            "minor plot hole", "minor issue", "not a major issue",
            "not a major contradiction", "doesn't affect the overall coherence",
            "does not affect the overall coherence", "could be improved but"
        ))
        if explicitly_minor:
            minor_issues.append(issue_text)
        else:
            retained_critical.append(issue_text)
    report["score"] = score
    report["critical_issues"] = retained_critical
    report["minor_issues"] = minor_issues
    core_dimensions_pass = all(float(dimensions[key]) >= 7 for key in required)
    report["passed"] = bool(score >= 70 and core_dimensions_pass and not retained_critical)
    if workdir:
        (Path(workdir) / "outline_quality_review.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return report


def enforce_principal_villain_identity(outline, principal_villain):
    """Prevent an alternate or anonymous mastermind from replacing the registered villain."""
    villain = str(principal_villain or "").strip()
    if not villain:
        raise ValueError("A registered principal villain is required to repair an outline.")
    for item in outline:
        if not isinstance(item, dict):
            continue
        beat = str(item.get("beat", ""))
        if villain.casefold() not in beat.casefold():
            match = re.search(
                r"\bthe mastermind behind (.+?) is revealed to be (.+?)(?=[,.;]|$)",
                beat, flags=re.IGNORECASE
            )
            if match:
                replacement = f"{villain} is revealed as the mastermind behind {match.group(1)}"
                beat = beat[:match.start()] + replacement + beat[match.end():]
            elif re.search(r"\bthe mastermind\b", beat, flags=re.IGNORECASE):
                beat = re.sub(
                    r"\bthe mastermind\b", villain, beat, count=1, flags=re.IGNORECASE
                )
        item["beat"] = re.sub(r"\s{2,}", " ", beat).strip()
    return outline


def repair_outline_from_review(plan, outline, report):
    """Repair an outline, retrying model outputs that still contain repeated beats."""
    base_prompt = (
        "Rewrite this entire story outline to fix every critical issue in the editorial review. "
        "This is an automatic quality repair, not a new unrelated story. Preserve the title, genre, "
        "setting, all registered character names, their roles, and the exact principal villain. "
        "The principal villain is the ONLY mastermind, culprit, or person behind the central crime. "
        "Never reveal a business partner, henchman, anonymous leader, or another registered character "
        "as the mastermind. Every mastermind reference must name the exact registered principal villain. "
        "Use ONLY exact full names from the character registry; no aliases and no new named people. "
        "The exact registered principal villain must be named in a meaningful clue or event within scenes 1-15. "
        "Every registered supporting character must be necessary to the case; do not introduce an unregistered helper. "
        "Keep the story grounded in its existing world. Make cause and effect clear in every beat. "
        "Every clue must lead somewhere and receive a payoff. Every kidnapping or rescue must have "
        "one clear sequence. Before a suspect is revealed, introduce that suspect earlier and plant at "
        "least one fair clue. Before heroes locate or capture anyone, show the clue, witness, record, "
        "tracking step, or deduction that reveals the location. Never let a character appear suddenly "
        "only to solve the mystery. Before anyone is declared guilty, show concrete evidence and a "
        "believable legal or public accountability process. Never state that evidence exists without "
        "showing where it came from. The villain must be exposed, confronted, and face a believable "
        "consequence. If people are abducted, rescue them before the ending; do not let an unexplained "
        "sacrifice happen. Every registered character must keep the same identity, name, and role. "
        "The final ten beats must resolve the main case, rescue or account for victims, expose the truth, "
        "hold the villain accountable, and give the protagonists emotional closure. No unresolved "
        "anonymous threat or sequel cliffhanger. Keep English very easy: common words and short sentences. "
        "Every one of the 50 beats MUST describe a different concrete event or decision. Do not repeat "
        "the same discovery, confrontation, rescue, travel, discussion, or resolution in different words. "
        "Advance the story in every beat. Return ONLY valid JSON with exactly two keys: description "
        "(100–140 words, 5–6 complete simple sentences that match the repaired story) and outline "
        "(an array of exactly 50 objects, each with number 1–50 and beat 8–45 words). Never make the "
        "description shorter than 100 words. No extra keys.\\n\\nEDITORIAL REVIEW:\\n"
        + json.dumps(report, ensure_ascii=False)
        + "\\n\\nLOCKED BOOK AND CAST:\\n"
        + json.dumps({
            "title": plan.get("title", ""),
            "principal_villain": plan.get("principal_villain", ""),
            "character_registry": plan.get("character_registry", []),
            "current_outline": outline
        }, ensure_ascii=False)
    )
    last_error = None
    for attempt in range(1, 4):
        prompt = base_prompt
        if last_error:
            prompt += (
                "\\n\\nREPAIR RETRY " + str(attempt) + ": The previous answer was rejected because "
                + last_error + ". Rewrite all 50 beats again. Ensure every beat is distinct and pass "
                "the full cast and numbering rules."
            )
        try:
            repaired = extract_json(
                cf_text(prompt, max_tokens=5200),
                "full outline repair after editorial rejection"
            )
        except (ValueError, TypeError) as exc:
            last_error = "the model returned invalid JSON: " + str(exc)[:500]
            logging.warning(
                "Outline repair attempt %s/3 returned invalid JSON: %s", attempt, last_error
            )
            continue
        if not isinstance(repaired, dict) or not isinstance(repaired.get("outline"), list):
            last_error = "the JSON did not contain a 50-beat outline"
            continue
        try:
            repaired_outline = validate_outline_rows(
                repaired["outline"], plan.get("character_registry", []), strict_repetition=True
            )
            repaired_outline = enforce_principal_villain_identity(
                repaired_outline, plan.get("principal_villain", "")
            )
            repaired_outline = validate_principal_villain_foreshadowing(
                repaired_outline, plan.get("principal_villain", ""), max_scene=15
            )
            repaired_outline = validate_outline_cast_distribution(
                repaired_outline, plan.get("character_registry", [])
            )
        except ValueError as exc:
            last_error = str(exc)
            logging.warning(
                "Outline repair attempt %s/3 rejected: %s", attempt, last_error
            )
            continue
        description = str(repaired.get("description", "")).strip()
        if len(description.split()) < 80:
            expansion_prompt = (
                "Write a complete, easy-English book description in 100–140 words and 5–6 short sentences. "
                "Describe the exact story below, including the main characters, mystery, stakes, key discovery, "
                "villain, and resolved ending. Do not invent events or characters. Return only the description.\\n\\n"
                + json.dumps({
                    "title": plan.get("title", ""),
                    "genre": plan.get("genre", ""),
                    "character_registry": plan.get("character_registry", []),
                    "principal_villain": plan.get("principal_villain", ""),
                    "outline": repaired_outline,
                    "short_draft": description
                }, ensure_ascii=False)
            )
            expanded = cf_text(expansion_prompt, max_tokens=500).strip()
            if 80 <= len(expanded.split()) <= 160:
                description = expanded
            else:
                original_description = str(plan.get("description", "")).strip()
                if len(original_description.split()) >= 80:
                    description = original_description
                elif len(expanded.split()) >= 50:
                    description = expanded
                else:
                    raise ValueError("Could not produce a complete readable description for the repaired story.")
        return repaired_outline, description
    raise ValueError(
        "Automatic outline repair failed after 3 attempts: "
        + (last_error or "the model did not return a valid outline")
    )


def review_story_quality(story, outline, workdir=None):
    """Reject books with broken cast continuity, weak plot logic, or hard-to-read prose."""
    # Deterministic structural gates run before the language-model editorial review.
    scenes = story.get("scenes", [])
    if not isinstance(scenes, list) or len(scenes) != 50:
        raise ValueError(f"Quality gate: expected exactly 50 scenes; received {len(scenes) if isinstance(scenes, list) else 'invalid data'}.")
    numbers = [scene.get("number") for scene in scenes if isinstance(scene, dict)]
    if len(numbers) != 50 or numbers != list(range(1, 51)):
        raise ValueError("Quality gate: scenes must be numbered sequentially from 1 through 50.")
    for scene in scenes:
        number = scene["number"]
        words = len(str(scene.get("text", "")).split())
        if not 40 <= words <= 55:
            raise ValueError(f"Quality gate: scene {number} has {words} words; every page must contain 40–55 words.")
        readability_issues = simple_english_issues(scene.get("text", ""))
        if readability_issues:
            raise ValueError(
                f"Quality gate: scene {number} is not written in very easy English: "
                + "; ".join(readability_issues)
            )
        if not str(scene.get("heading", "")).strip():
            raise ValueError(f"Quality gate: scene {number} is missing its heading.")
        if not str(scene.get("visual_prompt", "")).strip():
            raise ValueError(f"Quality gate: scene {number} is missing its illustration direction.")
    registry = story.get("character_registry", [])
    if not isinstance(registry, list) or not registry:
        raise ValueError("Quality gate: character registry is missing.")
    registered_names = [str(item.get("name", "")).strip() for item in registry if isinstance(item, dict)]
    if len(registered_names) != len(registry) or any(not name for name in registered_names) or len(set(registered_names)) != len(registered_names):
        raise ValueError("Quality gate: character registry has missing or duplicate names.")
    villain_name = str(story.get("principal_villain", "")).strip()
    villains = [item for item in registry if item.get("importance") == "villain"]
    if not villain_name or villain_name not in registered_names or len(villains) != 1 or villains[0].get("name") != villain_name:
        raise ValueError("Quality gate: exactly one registered principal villain is required.")
    registered_set = set(registered_names)
    scene_outline_rows = [
        {
            "number": scene.get("number"),
            "beat": str(scene.get("heading", "")) + " " + str(scene.get("text", "")),
        }
        for scene in scenes
    ]
    validate_outline_cast_distribution(scene_outline_rows, registry)
    for scene in scenes:
        present = scene.get("characters_present", [])
        if not isinstance(present, list):
            raise ValueError(f"Quality gate: scene {scene['number']} has an invalid characters_present list.")
        unknown = [name for name in present if name not in registered_set]
        if unknown:
            raise ValueError(f"Quality gate: scene {scene['number']} contains unregistered character name(s): {unknown}.")
    review_input = {
        "title": story.get("title", ""),
        "description": story.get("description", ""),
        "principal_villain": story.get("principal_villain", ""),
        "character_registry": story.get("character_registry", []),
        "outline": outline,
        "scenes": [
            {
                "number": scene.get("number"),
                "heading": scene.get("heading", ""),
                "text": scene.get("text", ""),
                "characters_present": scene.get("characters_present", []),
                "visual_prompt": scene.get("visual_prompt", ""),
                "shot_type": scene.get("shot_type", ""),
                "camera_angle": scene.get("camera_angle", ""),
                "composition": scene.get("composition", ""),
                "action_pose": scene.get("action_pose", ""),
                "gaze": scene.get("gaze", ""),
                "expression": scene.get("expression", ""),
                "lighting": scene.get("lighting", "")
            }
            for scene in story.get("scenes", [])
        ]
    }
    prompt = (
        "You are the strict continuity editor and story editor for a professionally illustrated "
        "feature film storybook. Audit the complete 50-scene book below. Do not be polite; protect "
        "the reader from a broken book. Check: (1) every named speaking or recurring character "
        "belongs to the character_registry and keeps one name and identity; (2) the registered "
        "principal villain is truly the main villain and has a clear, believable ending; "
        "(3) events happen in a logical order with no repeated kidnappings/rescues, verdicts before "
        "trials, dead characters returning, unexplained changes, or timeline breaks; "
        "(4) clues, motives and the main mystery have a clear payoff; (5) scenes progress and do "
        "not repeat the same event or use filler endings; (6) the book description matches the "
        "actual story; (7) page prose uses simple, common English and is easy to follow. "
        "A story fails if any major contradiction, unregistered named character, name drift, "
        "identity drift, missing villain payoff, unresolved central mystery, repeated major event, "
        "or difficult prose remains. Audit every scene, not a sample. Verify that every registered "
        "character keeps the exact same spelling of their name, role, physical identity and costume; "
        "no alias, nickname, renamed version or unexplained appearance change is allowed. "
        "Judge English for a beginner reader: common everyday words, short sentences, clear actions, "
        "simple dialogue, no confusing metaphors or complex phrasing. Score these dimensions from "
        "0 to 10: plot, continuity, character consistency, easy English, pacing, emotional impact, "
        "scene variety, illustration-direction clarity, originality, ending. Return ONLY JSON with "
        "keys: {\"passed\":true/false,\"critical_issues\":[\"issue and scene numbers\"],"
        "\"minor_issues\":[\"issue\"],\"dimension_scores\":{\"plot\":0-10,\"continuity\":0-10,"
        "\"character_consistency\":0-10,\"easy_english\":0-10,\"pacing\":0-10,"
        "\"emotional_impact\":0-10,\"scene_variety\":0-10,\"art_direction\":0-10,"
        "\"originality\":0-10,\"ending\":0-10},\"score\":0-100}. "
        "Calculate score as the average of the ten dimension scores multiplied by ten. "
        "A score below 70 is an automatic failure. Do not inflate scores to be encouraging. "
        "Set passed=false for any critical issue or score below 70.\\n\\nBOOK DATA:\\n"
        + json.dumps(review_input, ensure_ascii=False)
    )
    report = extract_json(cf_text(prompt, max_tokens=1500), "strict full-book continuity and readability audit")
    if not isinstance(report, dict) or not isinstance(report.get("passed"), bool):
        raise ValueError("The story quality reviewer did not return a valid pass/fail report.")
    score_dimensions = (
        "plot", "continuity", "character_consistency", "easy_english", "pacing",
        "emotional_impact", "scene_variety", "art_direction", "originality", "ending"
    )
    dimension_scores = report.get("dimension_scores")
    if not isinstance(dimension_scores, dict) or any(
        key not in dimension_scores
        or not isinstance(dimension_scores[key], (int, float))
        or isinstance(dimension_scores[key], bool)
        or not 0 <= dimension_scores[key] <= 10
        for key in score_dimensions
    ):
        raise ValueError("The story quality reviewer returned missing or invalid category scores.")
    # Calculate the final score locally so the reviewer cannot inflate or contradict it.
    score = round(sum(float(dimension_scores[key]) for key in score_dimensions) / len(score_dimensions) * 10, 1)
    report["score"] = score
    report["rating_out_of_10"] = round(score / 10, 1)
    report["minimum_publish_rating"] = 7.0
    # Core reader-facing dimensions cannot be hidden by high scores elsewhere.
    core_dimensions = (
        "plot", "continuity", "character_consistency", "easy_english",
        "art_direction", "ending"
    )
    critical_issues = report.get("critical_issues", [])
    if not isinstance(critical_issues, list):
        critical_issues = [str(critical_issues)]
    for key in core_dimensions:
        if float(dimension_scores[key]) < 7.0:
            critical_issues.append(
                f"Core quality dimension '{key}' scored {dimension_scores[key]}/10; minimum is 7/10."
            )
    report["critical_issues"] = critical_issues
    report["reviewed_at"] = datetime.utcnow().isoformat() + "Z"
    if workdir:
        (Path(workdir) / "quality_review.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if float(score) < 70 or not report["passed"] or report.get("critical_issues"):
        issues = "; ".join(str(issue) for issue in report.get("critical_issues", []))
        raise ValueError(
            "The story failed the full-book quality gate and will not be marked publishable. "
            + (issues or "The reviewer found critical story problems.")
        )
    logging.info("Story quality gate passed with score %s/100.", report.get("score", "unscored"))
    return report


def discard_cached_story_plan(plan_path, workdir):
    """Remove an invalid cached outline and every scene batch tied to that story."""
    if plan_path and Path(plan_path).exists():
        Path(plan_path).unlink()
    if workdir:
        for cached_batch in Path(workdir).glob("scene_batch_*.json"):
            cached_batch.unlink(missing_ok=True)


def make_story(genre, style, audience, title_hint, character_notes, scene_words, workdir=None):
    """Create a 50-scene story, caching the outline and each small scene batch."""
    workdir = Path(workdir) if workdir else None
    if workdir:
        workdir.mkdir(parents=True, exist_ok=True)
    plan_path = workdir / "story_plan.json" if workdir else None
    genre_label = str(genre or "").lower()
    fantasy_mode = any(term in genre_label for term in ("fantasy", "magical", "mythic", "enchanted"))
    if fantasy_mode:
        world_rules = """WORLD MODE: FANTASY. Build an original fantasy world with its own rules, history, factions and atmosphere. Mystery, suspense and thriller tension are mandatory: use a consequential mystery or threat, escalating clues, secrets, reversals, danger, time pressure and a satisfying reveal. Fantasy creatures may appear only when the plot needs them; they are not compulsory. Give every character a story-specific role, rank, profession, powers and abilities appropriate to this world's rules. Powers and ranks are not permanent library traits and may change in another story. Do not make every fantasy story a light-hearted adventure; maintain genuine suspense and thriller stakes."""
    else:
        world_rules = """WORLD MODE: REAL-WORLD ONLY. This is a grounded contemporary or historical human-world crime / suspense / thriller story. Use realistic human characters, believable institutions, locations, professions, jobs, ranks and technology appropriate to the chosen period. NO fantasy creatures, dragons, goblins, unicorns, talking animals, magic, supernatural powers, mythical species, enchanted objects or fantasy-world elements. Crime, investigation, psychological suspense, conspiracy, danger, twists and thriller pacing should drive the plot. Roles, professions, ranks, designations and costumes are created specifically for this story and are not permanent library traits."""
    common = f"""
Create an original illustrated feature-film story.
Story direction: {genre}
Genre rules: {world_rules}
Genre emphasis: {genre}
Audience: {audience}. Cast age, occupations, social roles and character mix must suit the genre and plot; do not force every protagonist to be human unless the selected world mode requires it.
Visual style (LOCKED): {STYLE_LOCK}
Title hint: {title_hint or "invent a memorable title"}.
Character notes / reusable character pool: {character_notes or DEFAULT_CHARACTER_POOL}
Permanent visual library — numbered slots with identity facts only (no legacy names or story roles):
{json.dumps(permanent_visual_slot_catalog(), ensure_ascii=False)}
TWO-STAGE CAST DESIGN (mandatory):
STAGE 1 — Read the plot and genre, then independently select only the visual slots that genuinely improve this story. You may select zero, one, several or all; do not force a fixed cast. Real-world mode may select only human visual slots.
STAGE 2 — Only AFTER selecting slots, create this book's character registry. For each selected visual slot, invent a completely new story-specific name and assign a plot-appropriate personality, backstory, relationships, profession/job, role, rank/designation, social status, costume and (fantasy mode only) powers/abilities. Do not use or mention old library names. The technical visual_id links the new story identity to its permanent portrait. For original non-library characters use visual_id:null and invent their complete identity.
The library is ONLY a set of reusable physical identity portraits. Preserve only the selected model's recognisable physical identity, anatomy/species and permanent marks. Everything else is newly invented for this book. Never carry over a prior book's name, personality, role, powers, job, rank, costume or relationships.
Every recurring character must have a stable identity and costume within this book. Every scene must use the story-specific registry names.
HOLLYWOOD FILM DIRECTION — mandatory:
Think like a film director and cinematographer, not a portrait illustrator. Plan camera placement,
actor blocking, eyelines, foreground action, background action and visual storytelling.
Use varied coverage: extreme wide establishing shots, wide master shots, medium two-shots,
over-the-shoulder dialogue shots, reverse shots, side profiles, rear three-quarter views,
shots from behind a speaker, low and high angles, overheads, close-ups, insert details,
tracking viewpoints and motivated 360-degree orbit frames. Adjacent scenes must not repeat
the same camera setup. During dialogue, the speaker looks at the listener and the listener
looks at the speaker. Characters may face away from camera, appear in profile, or be partly
seen from behind. Direct eye contact with the viewer is rare and story-motivated. Never pose
a group as a front-facing lineup. Use simple, natural English with common words and short sentences.
Avoid copyrighted characters, existing franchises and imitation of living artists.
"""
    plan = None
    cached_plan_loaded = False
    if plan_path and plan_path.exists():
        try:
            cached = json.loads(plan_path.read_text(encoding="utf-8"))
            if cached.get("story_schema_version") == 6 and len(cached.get("outline", [])) == 50 and cached.get("character_registry") and cached.get("principal_villain"):
                plan = cached
                cached_plan_loaded = True
        except Exception:
            plan = None

    if plan is None:
        metadata_prompt = common + """
Create ONLY the book metadata and character registry for an original illustrated feature-film story. Write the description in clear, simple English and describe only events that the actual story will deliver.
Return one compact valid JSON object, no markdown, with these keys:
{
 "title":"...",
 "subtitle":"...",
 "author":"Your Author Name",
 "description":"A compelling 100-140 word book description",
 "keywords":["seven distinct short search phrases"],
 "categories":["suggested broad category 1","suggested broad category 2"],
 "audience":"Teen and adult readers",
 "character_registry":[
  {"name":"Fresh story-specific name","species":"human","visual_id":null,"role":"story role","importance":"protagonist/supporting/villain (creature only in fantasy)","immutable_identity":"physical traits and permanent marks","story_costume":"this book's clothing and accessories","visual_bible":"complete physical identity plus story costume"}
 ],
 "principal_villain":"Exact name of the single principal villain",
 "art_direction":"Consistent palette, lighting and world atmosphere"
}
Before writing the metadata, invent one specific, original central crime or threat with a clear victim/stakes, a believable motive, a useful first clue, and a complete resolution. Avoid generic "web of corruption" plots with no concrete crime or evidence. The principal villain must be a real, consequential person connected to the central crime from the beginning; their full name should appear in the first act through a clue or known connection, even if their face is not yet seen. Supporting characters must have distinct, necessary roles in the case. Do not create a mysterious helper halfway through the story; every recurring person must be in the registry. Keep the registry concise and plot-driven: create only the characters the story genuinely needs. A typical story may have one to three protagonists, supporting roles as required, and a consequential antagonist; do not pad the cast to hit a fixed count. In real-world mode every character must be human and grounded; importance must be protagonist, supporting, or villain only (never creature). In fantasy mode add creatures only if they serve the plot. The 100-140 word description must name the protagonist and principal villain and describe the concrete crime, stakes, and resolved outcome. Never include an outline or extra keys.
"""
        plan = extract_json(cf_text(metadata_prompt, max_tokens=2600), "compact book metadata and character registry")
        if not isinstance(plan, dict):
            raise ValueError("Book metadata response was not a JSON object.")
        if not fantasy_mode:
            normalize_real_world_registry(plan)

        # Repair a common metadata inconsistency before spending tokens on the outline.
        registry_for_villain = plan.get("character_registry", [])
        if isinstance(registry_for_villain, list):
            registry_names = [
                str(item.get("name", "")).strip()
                for item in registry_for_villain if isinstance(item, dict)
            ]
            declared_villain = str(plan.get("principal_villain", "")).strip()
            villain_rows = [
                item for item in registry_for_villain
                if isinstance(item, dict) and item.get("importance") == "villain"
            ]
            if (declared_villain in registry_names and
                    (len(villain_rows) != 1 or villain_rows[0].get("name") != declared_villain)):
                for item in registry_for_villain:
                    if isinstance(item, dict) and item.get("importance") == "villain":
                        item["importance"] = "supporting"
                for item in registry_for_villain:
                    if isinstance(item, dict) and str(item.get("name", "")).strip() == declared_villain:
                        item["importance"] = "villain"
                logging.info(
                    "Normalized villain registry to the exact declared principal villain: %s",
                    declared_villain
                )
            else:
                villain_prompt = common + """
The metadata JSON below has an inconsistent principal villain declaration.
Repair ONLY the character_registry importance fields and principal_villain field.
There must be exactly ONE registry entry with importance="villain".
principal_villain must exactly equal that entry's full name, character role must make
them a genuine consequential antagonist, and no other entry may be marked villain.
Do not invent characters or rename anyone. Preserve all other metadata and character
details exactly. Return ONLY the complete corrected JSON object.
Metadata:
""" + json.dumps(plan, ensure_ascii=False)
                if (not declared_villain or declared_villain not in registry_names or
                        len(villain_rows) != 1):
                    repaired_plan = extract_json(
                        cf_text(villain_prompt, max_tokens=2200),
                        "principal villain metadata repair"
                    )
                    if not isinstance(repaired_plan, dict):
                        raise ValueError("Principal villain metadata repair did not return a JSON object.")
                    plan = repaired_plan

        outline_prompt = common + """
BOOK METADATA AND LOCKED CAST:
""" + json.dumps({
            "title": plan.get("title", ""),
            "description": plan.get("description", ""),
            "character_registry": plan.get("character_registry", []),
            "principal_villain": plan.get("principal_villain", "")
        }, ensure_ascii=False) + """

Create a fresh, original 50-scene cinematic story outline for THIS exact book.
CANONICAL NAME RULE: Every registered character must be referred to only by their exact full registry name.
Never use aliases, surnames alone, nicknames, former names, or substitute characters.
Do not invent or name any other person. Use unnamed extras only when needed.
Every registered protagonist and supporting character must have a meaningful part.
The exact registered principal villain must drive the central conflict and be confronted in the ending.
The outline, metadata and cast registry must describe the same story; do not replace a registered person
with a generic detective, partner, businessman, victim, leader or other unnamed substitute.
Return EXACTLY 50 numbered lines as plain text, not JSON, not markdown bullets.
Format every line exactly: 01 | one or two sentences describing one distinct event.
Use 8-45 words per scene. No quotation marks, no JSON, no headings, no blank lines.
Five acts: scenes 1-10 setup and inciting mystery; 11-20 investigation and setback;
21-30 revelation and changed plan; 31-40 escalation and sacrifice; 41-50 confrontation and resolution.
REQUIRED CAUSAL STORY DESIGN:
- Establish ONE clear central crime or threat, its victim/stakes, and why the protagonist must act.
- Introduce the exact registered principal villain by full name within scenes 1-15 through a clue,
  record, witness statement, known connection, or direct action. Do not save the villain's name
  for a sudden reveal near the end.
- Connect every registered supporting character to the central case with a clear goal and useful action.
  The protagonist must appear in at least 8 beats, each supporting character in at least 3 beats,
  and the villain in at least 5 beats including one of the final ten. Do not introduce a helpful stranger
  or new named person halfway through the story.
- Every clue must be found for a believable reason, point to the next step, and later receive a payoff.
- If the protagonist goes undercover, state the exact evidence or access they need, how they prepare,
  and why this plan is safer or more useful than another option. Otherwise do not use an undercover plot.
- Do not add a shootout, chase, kidnapping, rescue, escape, or ambush unless earlier beats establish
  who causes it, how it happens, and what new consequence it creates.
- The villain's final defeat must follow from evidence and a believable plan. Show the actual capture,
  exposure, or other consequence. Do not invent a court sentence or legal result without a trial,
  evidence, and process shown in the outline.
- The final five scenes must close the central case, account for victims, show the villain's outcome,
  and give the protagonist a clear emotional ending. No repeated wrap-up scenes.
- Use only the registered character names. A role-only unnamed extra may appear briefly but must not
  become a new recurring ally, suspect, mastermind, or plot solver.
Every scene must introduce a new physical action and consequence. Never repeat a chase,
clue discovery, argument, journey, or other event with slightly changed wording.
"""
        last_outline_error = None
        parsed_outline = None
        for outline_attempt in range(1, 6):
            retry_prompt = outline_prompt
            if outline_attempt > 1 and last_outline_error:
                retry_prompt += (
                    "\n\nREPAIR FEEDBACK FROM THE PREVIOUS OUTLINE (attempt "
                    + str(outline_attempt - 1) + "): "
                    + last_outline_error
                    + "\nRewrite the specifically identified beat(s); if the feedback identifies "
                    + "short beats, expand them to 8-45 words; if it identifies repetition, replace "
                    + "the repeated event with a genuinely different action and consequence. Do not "
                    + "reuse the same wording or event. Ensure each registered supporting character "
                    + "appears by exact full name in at least 3 distinct beats. Preserve cast, causal "
                    + "continuity, and all other requirements."
                )
            raw_outline = cf_text(retry_prompt, max_tokens=2400)
            rows = []
            for line in raw_outline.splitlines():
                match = re.match(r"^\s*(\d{1,2})\s*[|.)-]\s*(.+?)\s*$", line)
                if match:
                    rows.append({"number": int(match.group(1)), "beat": match.group(2).strip()})
            if len(rows) == 50 and [item["number"] for item in rows] == list(range(1, 51)):
                beats = [re.sub(r"[^a-z0-9 ]", "", item["beat"].lower()).strip() for item in rows]
                registry = plan.get("character_registry", [])
                missing_cast = missing_registered_outline_names(rows, registry)
                unknown_cast = unregistered_named_entities_in_outline(rows, registry)
                if unknown_cast:
                    rows = sanitize_outline_unregistered_names(rows, registry)
                    unknown_cast = unregistered_named_entities_in_outline(rows, registry)
                    missing_cast = missing_registered_outline_names(rows, registry)
                    logging.warning("Sanitized unregistered outline names; remaining: %s", unknown_cast)
                villain_name = str(plan.get("principal_villain", "")).strip()
                villain_foreshadowed = bool(villain_name) and any(
                    villain_name.casefold() in str(item.get("beat", "")).casefold()
                    for item in rows[:15]
                )
                cast_distribution_ok = True
                try:
                    validate_outline_cast_distribution(rows, registry)
                except ValueError as exc:
                    cast_distribution_ok = False
                    last_outline_error = str(exc)
                if cast_distribution_ok and all(8 <= len(str(item.get("beat", "")).split()) <= 45 for item in rows) and len({
                    re.sub(r"[^a-z0-9 ]", "", str(item.get("beat", "")).lower()).strip()
                    for item in rows
                }) == 50 and not missing_cast and not unknown_cast and villain_foreshadowed:
                    try:
                        validate_outline_rows(rows, registry, strict_repetition=True)
                    except ValueError as exc:
                        last_outline_error = str(exc)
                    else:
                        parsed_outline = rows
                        break
                if missing_cast or unknown_cast:
                    problems = []
                    if missing_cast:
                        problems.append("omitted registered names: " + ", ".join(missing_cast))
                    if unknown_cast:
                        problems.append("introduced unregistered named people: " + ", ".join(unknown_cast))
                    last_outline_error = "Outline cast mismatch — " + "; ".join(problems)
                elif not cast_distribution_ok:
                    # Keep the precise cast-distribution error captured above.
                    pass
                elif not villain_foreshadowed:
                    last_outline_error = (
                        "The exact registered principal villain must be foreshadowed by full name "
                        "in a meaningful clue or event within scenes 1-15."
                    )
                elif not last_outline_error:
                    word_range_issues = [
                        f"beat {item.get('number')} has {len(str(item.get('beat', '')).split())} words"
                        for item in rows
                        if not 8 <= len(str(item.get("beat", "")).split()) <= 45
                    ]
                    if word_range_issues:
                        last_outline_error = (
                            "STRICT LENGTH ERROR: Each numbered line must contain ONLY one scene beat "
                            "of 8–45 words. Correct these: " + "; ".join(word_range_issues)
                            + ". Do not put the full story, multiple scenes, or commentary in a line."
                        )
                    else:
                        last_outline_error = (
                            "Outline failed validation. Rewrite all beats with 8-45 words each, "
                            "distinct physical actions, clear consequences, and no repeated or near-identical events."
                        )
            else:
                last_outline_error = f"Expected 50 numbered lines; parsed {len(rows)}."
            if outline_attempt < 5:
                outline_prompt += f"""

RETRY {outline_attempt}: {last_outline_error}
You MUST use every registered character's exact full name from the cast registry above.
Do not substitute aliases or invent any named person. Remove every unregistered named person
from the outline; use an unnamed extra only when a one-scene background person is essential.
Return all 50 lines again, using the exact two-digit number | event format.
Replace repeated scenes with new actions, discoveries, decisions and consequences.
Ensure the exact registered principal villain is named in a meaningful clue or event within scenes 1-15.
Every registered supporting character must be necessary to the case; do not introduce an unregistered helper.
Do not add JSON, quotation marks, headings or explanatory text.
"""
        if parsed_outline is None:
            raise ValueError(f"Outline generation failed after 5 attempts: {last_outline_error}")
        plan["outline"] = parsed_outline
        plan["character_bible"] = "\n".join(
            f'{item.get("name", "")}: {item.get("visual_bible", "")}'
            for item in plan.get("character_registry", []) if isinstance(item, dict)
        )
        plan["story_schema_version"] = 6
        registry = plan.get("character_registry", [])
        if not isinstance(registry, list) or not registry:
            raise ValueError("Story plan is missing its character_registry.")
        names = [str(item.get("name", "")).strip() for item in registry if isinstance(item, dict)]
        known_visual_ids = {model.get("visual_id") for model in load_character_library()}
        selected_visual_ids = [item.get("visual_id") for item in registry if item.get("visual_id")]
        if len(selected_visual_ids) != len(set(selected_visual_ids)):
            raise ValueError("A permanent visual slot may be selected only once in a book.")
        for item in registry:
            visual_id = item.get("visual_id")
            if visual_id and visual_id not in known_visual_ids:
                # The text model sometimes invents visual IDs. Treat those as
                # original characters instead of binding them to a nonexistent identity.
                logging.warning(
                    "Character %s referenced unavailable visual model %s; "
                    "clearing the invalid ID and generating an original identity.",
                    item.get("name"), visual_id
                )
                item["visual_id"] = None
            if item.get("visual_id") and (not item.get("immutable_identity") or not item.get("story_costume")):
                raise ValueError(f"Character {item.get('name')} must separate immutable_identity from story_costume.")
        if len(names) != len(registry) or any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Character registry has missing or duplicate character names.")
        villain_name = str(plan.get("principal_villain", "")).strip()
        villain_entries = [item for item in registry if item.get("importance") == "villain"]
        if not villain_name or villain_name not in names or len(villain_entries) != 1 or villain_entries[0]["name"] != villain_name:
            raise ValueError("Every book must define exactly one registered principal villain.")
        plan["character_bible"] = "\n\n".join(
            f"{item['name']} — {item.get('role', '')}: {item.get('visual_bible', '')}"
            for item in registry
        )
        if plan_path:
            plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    if not fantasy_mode:
        normalize_real_world_registry(plan)
        plan["character_bible"] = "\n\n".join(
            f"{item.get('name', '')} — {item.get('role', '')}: {item.get('visual_bible', '')}"
            for item in plan.get("character_registry", []) if isinstance(item, dict)
        )
        if plan_path:
            plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    plan["genre"] = genre
    plan = ensure_concrete_story_costumes(plan)
    if plan_path:
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    cached_outline_missing = missing_registered_outline_names(
        plan.get("outline", []), plan.get("character_registry", [])
    )
    cached_outline_unknown = unregistered_named_entities_in_outline(
        plan.get("outline", []), plan.get("character_registry", [])
    )
    if cached_outline_missing or cached_outline_unknown:
        logging.warning(
            "Cached story outline has cast mismatches (missing=%s; unregistered=%s); "
            "discarding the incompatible outline and scene batches.",
            cached_outline_missing, cached_outline_unknown
        )
        discard_cached_story_plan(plan_path, workdir)
        return make_story(
            genre, style, audience, title_hint, character_notes, scene_words, workdir=workdir
        )

    try:
        outline = validate_outline_rows(plan["outline"], plan.get("character_registry", []))
        outline = validate_principal_villain_foreshadowing(
            outline, plan.get("principal_villain", ""), max_scene=15
        )
        outline = validate_outline_cast_distribution(
            outline, plan.get("character_registry", [])
        )
    except ValueError as exc:
        if not cached_plan_loaded:
            raise
        logging.warning(
            "Cached outline failed the current publication gates (%s); discarding its scene batches "
            "and regenerating the story structure.",
            exc
        )
        discard_cached_story_plan(plan_path, workdir)
        return make_story(
            genre, style, audience, title_hint, character_notes, scene_words, workdir=workdir
        )
    plan["description"] = synchronize_book_description(plan, outline)
    outline_report = review_story_outline(plan, outline, workdir)
    for repair_attempt in range(1, 7):
        if outline_report.get("passed"):
            break
        if repair_attempt == 6:
            break
        logging.warning(
            "Outline quality review failed (%s/100); automatically repairing story structure.",
            outline_report.get("score")
        )
        outline, repaired_description = repair_outline_from_review(plan, outline, outline_report)
        plan["outline"] = outline
        plan["description"] = repaired_description
        plan["description"] = synchronize_book_description(plan, outline)
        if workdir:
            for cached_batch in workdir.glob("scene_batch_*.json"):
                cached_batch.unlink(missing_ok=True)
        outline_report = review_story_outline(plan, outline, workdir)
    if not outline_report.get("passed"):
        raise ValueError(
            "The story outline could not reach the minimum 7/10 quality standard after automatic repair. "
            + "; ".join(str(issue) for issue in outline_report.get("critical_issues", []))
        )
    plan["outline"] = outline
    plan["outline_quality_review"] = outline_report
    if plan_path:
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    scenes = []
    # Five scenes per request keeps responses smaller and makes recovery granular.
    for first in range(1, 51, 5):
        last = min(first + 4, 50)
        batch_path = workdir / f"scene_batch_{first:02d}_{last:02d}.json" if workdir else None
        batch_scenes = None
        if batch_path and batch_path.exists():
            try:
                cached_batch = json.loads(batch_path.read_text(encoding="utf-8"))
                if len(cached_batch.get("scenes", [])) == last - first + 1 and all(isinstance(x, dict) and len(str(x.get("text", "")).split()) >= 35 and x.get("heading") and x.get("visual_prompt") and isinstance(x.get("dialogue"), list) for x in cached_batch["scenes"]):
                    batch_scenes = cached_batch["scenes"]
            except Exception:
                batch_scenes = None

        if batch_scenes is None:
            batch_outline = outline[first - 1:last]
            batch_prompt = common + f"""
Book title: {plan.get("title", title_hint or "Untitled")}
Character registry (canonical identities; never rename or redesign within this book):
{json.dumps(plan.get("character_registry", []), ensure_ascii=False)}
CAST RULE: For characters_present, use ONLY exact names from the character registry above. Never use permanent-library base names unless that exact name is registered for this book. Never invent incidental named people (for example, Mr. Jenkins). Unnamed role labels such as victim, suspect, witness, guard, officer, crowd or bystander are environmental extras, not cast names; do not put them in characters_present.
Principal villain: {plan.get("principal_villain", "")}
Character bible: {plan.get("character_bible", "")}
Art direction: {plan.get("art_direction", "")}
Full story outline for continuity:
{json.dumps(outline, ensure_ascii=False)}
Specific beats to expand now:
{json.dumps(batch_outline, ensure_ascii=False)}

Expand ONLY scenes {first} through {last} into detailed illustrated-book scene records.
Each scene page text must be 40–55 words (aim for {scene_words}), written in simple, natural English. Use common words, clear sentence order and short sentences. Avoid fancy vocabulary, vague dramatic filler and repeated phrases. Include concrete action, emotion and useful dialogue. Every scene must move the plot forward; no recap-only pages, repeated planning conversations, repeated rescues or filler endings. Dialogue lines must be original, short, natural and distinct: NEVER repeat a spoken line anywhere in the 50 scenes, even when spoken by another character. Avoid catchphrases reused across scenes.
For every illustration, write a director's shot plan: camera position, shot size, camera angle, nearest character, body orientation of EACH visible character, exact eyeline target of EACH visible character, foreground/background layers, and why this angle tells the story. Use varied film coverage across the book. In dialogue, speaker looks at listener and listener looks at speaker. Use over-the-shoulder, reverse, profile, rear-view and side views often. Avoid front-facing group poses and repeated eye-level medium shots. Do not repeat the same camera setup in nearby scenes. For characters with visual_id, preserve immutable_identity and permanent marks, but follow story_costume for clothing; costume stays consistent within this book. The visual_prompt must describe only scene action, camera, environment, lighting and composition. Do NOT invent or restate hair, eyes, face, age, skin tone, body type, scars, marks, or clothing; those details come only from the locked character registry and visual bible.
Return ONLY valid JSON with this exact shape:
{{
 "scenes":[
  {{
   "number":{first},
   "heading":"short scene heading",
   "text":"page text",
   "shot_type":"wide / medium / close-up / detail / overhead / other",
   "camera_angle":"low / high / eye-level / overhead / dutch / other",
   "composition":"foreground, subject placement, background and depth",
   "action_pose":"specific physical action and body pose",
   "gaze":"where each visible character is looking",
   "expression":"specific emotion visible on face/body",
   "lighting":"cinematic lighting and atmosphere",
   "characters_present":["character names actually visible"],
   "visual_prompt":"Detailed scene illustration prompt",
   "dialogue":[{"speaker":"Exact registered character name","line":"Short spoken line"}]
  }}
 ]
}}
Return exactly {last - first + 1} scenes, numbered {first} through {last}, in order.
No extra keys, no markdown fences, no text outside JSON.
"""
            batch_data = extract_json(
                cf_text(batch_prompt, max_tokens=3000),
                f"scene batch {first} through {last}"
            )
            batch_scenes = batch_data.get("scenes", [])
            if len(batch_scenes) != last - first + 1:
                raise ValueError(
                    f"Scene batch {first}-{last}: expected {last - first + 1} scenes "
                    f"but received {len(batch_scenes)}."
                )
        for offset, scene in enumerate(batch_scenes):
            number = first + offset
            scene["number"] = number
            if not scene.get("text") or not scene.get("heading"):
                raise ValueError(f"Scene {number} is missing its heading or page text.")
            if not scene.get("visual_prompt"):
                beat = outline[number - 1].get("beat", "")
                scene["visual_prompt"] = (
                    f"Illustrate this story event clearly and cinematically: {beat}. "
                    f"Show only the registered characters listed for this scene; use the established "
                    f"character identities, story costumes and art direction. Scene details: {scene.get('text', '')}"
                )
            scene = repair_scene_page_text(scene, plan, number)
            for key in ("shot_type", "camera_angle", "composition", "action_pose", "gaze", "expression", "lighting"):
                scene.setdefault(key, "")
            scene.setdefault("dialogue", [])
            if not isinstance(scene["dialogue"], list):
                scene["dialogue"] = []
            scene["dialogue"] = [
                {"speaker": str(d["speaker"]), "line": str(d["line"]).strip()[:180]}
                for d in scene["dialogue"]
                if isinstance(d, dict) and d.get("speaker") and d.get("line")
                and d["speaker"] in scene.get("characters_present", [])
            ][:2]
            scene.setdefault("characters_present", [])
            known_names = {item["name"] for item in plan.get("character_registry", [])}
            visible_names = scene.get("characters_present", [])
            if not isinstance(visible_names, list):
                raise ValueError(f"Scene {number} characters_present must be a list.")
            # Permanent visual slots have no story names. The current book's
            # registry is the sole authority for character names.
            visible_names, cast_aliases = normalize_scene_cast_names(
                visible_names, plan.get("character_registry", []), return_aliases=True
            )
            if cast_aliases:
                apply_scene_cast_aliases(scene, cast_aliases)
                logging.warning("Canonicalized scene %s character aliases: %s", number, cast_aliases)
            # Repair small model spelling drift only when there is exactly one
            # unambiguous registered-name match. Never silently accept a new cast member.
            corrected_names = []
            for visible_name in visible_names:
                if visible_name in known_names:
                    corrected_names.append(visible_name)
                    continue
                candidates = [
                    registered_name for registered_name in known_names
                    if abs(len(visible_name.casefold()) - len(registered_name.casefold())) <= 1
                    and difflib.SequenceMatcher(
                        None, visible_name.casefold(), registered_name.casefold()
                    ).ratio() >= 0.86
                ]
                if len(candidates) == 1:
                    logging.warning(
                        "Correcting near-match scene character name %r to registered name %r.",
                        visible_name, candidates[0]
                    )
                    corrected_names.append(candidates[0])
                else:
                    corrected_names.append(visible_name)
            visible_names = list(dict.fromkeys(corrected_names))
            scene["characters_present"] = visible_names
            scene = sanitize_scene_unregistered_cast(scene, plan.get("character_registry", []))
            visible_names = scene.get("characters_present", [])
            unknown_names = [name for name in visible_names if name not in known_names]
            scene_name_audit_text = " ".join(
                str(scene.get(field, "")) for field in ("heading", "text", "visual_prompt")
            )
            unknown_names.extend(
                unregistered_named_entities_in_outline(
                    [{"beat": scene_name_audit_text}], plan.get("character_registry", [])
                )
            )
            unknown_names = list(dict.fromkeys(unknown_names))
            if unknown_names:
                # Attempt a focused canonical-name repair before rejecting the book.
                # The model must map aliases/surnames to exact registry names and
                # preserve the scene; unregistered characters are never silently dropped.
                repair_prompt = (
                    "Repair this one story scene so its cast uses ONLY exact canonical names "
                    "from the supplied character registry. Do not invent names, aliases, "
                    "surnames, or additional people. If a registered character is mentioned "
                    "by a shortened name or surname, use that character's exact registry name. "
                    "Preserve the event, facts, emotional beat, continuity, visual action, and "
                    "camera direction. Keep page text between 40 and 55 words in very simple "
                    "English. characters_present must contain only exact registry names for "
                    "people or creatures actually visible. Return ONLY a JSON object with keys "
                    "heading, text, shot_type, camera_angle, composition, action_pose, gaze, "
                    "expression, lighting, characters_present, visual_prompt.\\n\\n"
                    f"Registry: {json.dumps(plan.get('character_registry', []), ensure_ascii=False)}\\n"
                    f"Principal villain: {plan.get('principal_villain', '')}\\n"
                    f"Unregistered names detected: {json.dumps(unknown_names)}\\n"
                    f"Scene to repair: {json.dumps(scene, ensure_ascii=False)}"
                )
                repaired = extract_json(
                    cf_text(repair_prompt, max_tokens=1400),
                    f"scene {number} canonical-name repair"
                )
                if not isinstance(repaired, dict):
                    raise ValueError(f"Scene {number} name repair did not return a JSON object.")
                for key in (
                    "heading", "text", "shot_type", "camera_angle", "composition",
                    "action_pose", "gaze", "expression", "lighting",
                    "characters_present", "visual_prompt", "dialogue"
                ):
                    if key in repaired:
                        scene[key] = repaired[key]
                repaired_names = scene.get("characters_present")
                if not isinstance(repaired_names, list):
                    raise ValueError(f"Scene {number} name repair returned an invalid cast list.")
                scene = sanitize_scene_unregistered_cast(scene, plan.get("character_registry", []))
                repaired_names = scene.get("characters_present", [])
                scene = repair_scene_page_text(scene, plan, number)
                remaining_unknown = [name for name in repaired_names if name not in known_names]
                remaining_named_people = unregistered_named_entities_in_outline(
                    [{"beat": " ".join(str(scene.get(field, "")) for field in ("heading", "text", "visual_prompt"))}],
                    plan.get("character_registry", [])
                )
                if remaining_unknown or remaining_named_people:
                    raise ValueError(
                        f"Scene {number} still uses unregistered character name(s) "
                        f"{list(dict.fromkeys(remaining_unknown + remaining_named_people))} after targeted repair."
                    )
                scene["characters_present"] = list(dict.fromkeys(repaired_names))
            scene = repair_scene_page_text(scene, plan, number)
            scenes.append(scene)

        # Cache only a fully validated batch so interrupted/invalid responses are regenerated.
        if batch_path:
            batch_path.write_text(
                json.dumps({"scenes": batch_scenes}, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

    # Enforce dialogue uniqueness after all scene repairs and cache merges.\n    dialogue_report = validate_unique_story_dialogue(scenes)\n    text_integrity_report = validate_story_text_integrity(scenes)\n\n    # Final publication-boundary pass: run after every cast/name rewrite and
    # cached-batch merge, immediately before the deterministic quality gate.
    for final_scene in scenes:
        final_number = int(final_scene.get("number", 0))
        repair_scene_page_text(final_scene, plan, final_number)
        final_word_count = len(str(final_scene.get("text", "")).split())
        if not 40 <= final_word_count <= 55:
            raise ValueError(
                f"Final page-text normalization failed for scene {final_number}: "
                f"{final_word_count} words."
            )
        final_readability_issues = simple_english_issues(final_scene.get("text", ""))
        if final_readability_issues:
            raise ValueError(
                f"Final page-text normalization failed the easy-English gate for scene "
                f"{final_number}: " + "; ".join(final_readability_issues)
            )
    plan["scenes"] = scenes
    plan["art_direction"] = f"{STYLE_LOCK}\n{plan.get('art_direction', '')}"
    plan["quality_review"] = review_story_quality(plan, outline, workdir)
    plan.pop("outline", None)
    save_story_cast(plan, workdir)
    return plan

def cf_image(prompt, dest, width=1024, height=1024, reference_images=None):
    if not CF_ACCOUNT or not CF_TOKEN:
        raise RuntimeError("Cloudflare credentials missing. Edit the .env file first.")

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{IMAGE_MODEL}"
    reference_images = reference_images or []
    reference_guidance = reference_guidance_for_images(reference_images)

    full_prompt = (
        prompt
        + "\n" + reference_guidance
        + "Render with crisp, tack-sharp focus and clearly resolved fine detail. "
        + "Keep eyes, facial features, hair strands, fabric texture, material edges and environmental details distinct. "
        + "Use clean silhouettes, precise linework, controlled painterly shading and strong foreground-to-background separation. "
        + "The main subjects must remain sharply focused; atmospheric depth belongs in the environment, not as blur over faces. "
        + "No softness, defocus, motion blur, smeared textures, muddy details, low-resolution appearance, or excessive noise. "
        + "Vary camera direction and physical action according to the scene description. "
        + "Illustration only. No text, typography, captions, letters, logos or watermark."
    )

    # FLUX.2 Klein requires multipart form data even when no reference images are supplied.
    multipart = {
        "prompt": (None, full_prompt),
        "width": (None, str(width)),
        "height": (None, str(height)),
    }
    image_buffers = []
    if len(reference_images) > 4:
        raise ValueError("Maximum four reference images are supported.")

    try:
        for index, image_path in enumerate(reference_images):
            with Image.open(image_path) as source:
                im = source.convert("RGB")
                im.thumbnail((511, 511), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                im.save(buffer, format="PNG")
                image_buffers.append(buffer)
                multipart[f"input_image_{index}"] = (
                    f"reference_{index}.png",
                    buffer.getvalue(),
                    "image/png",
                )

        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {CF_TOKEN}"},
            files=multipart,
            timeout=240,
        )
        if not response.ok:
            raise RuntimeError(
                f"Cloudflare image request failed ({response.status_code}): {response.text[:1000]}"
            )

        raw = response.content
        if "json" in response.headers.get("content-type", ""):
            obj = response.json()
            result = obj.get("result", {})
            if isinstance(result, dict) and result.get("image"):
                raw = base64.b64decode(result["image"])
            elif isinstance(result, str):
                raw = base64.b64decode(result)
            else:
                raise RuntimeError("Cloudflare image response did not contain an image.")

        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image.save(dest, "PNG")
    except Exception as exc:
        raise RuntimeError(f"Image generation or saving failed: {exc}") from exc
    finally:
        for buffer in image_buffers:
            buffer.close()


def character_reference_path(outdir, character_name):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", character_name).strip("_")
    return Path(outdir) / "character_refs" / f"{safe}.png"


def character_reference_for_entry(outdir, item):
    # Prefer this book's own model sheet: it locks the new story costume as well
    # as the reused face. Fall back to the permanent portrait only before it exists.
    story_portrait = character_reference_path(outdir, item.get("name", ""))
    if story_portrait.is_file() and story_portrait.stat().st_size > 1024:
        return story_portrait
    visual_id = item.get("visual_id")
    if visual_id:
        permanent = library_reference_path(visual_id)
        if permanent:
            return permanent
    return story_portrait


def scene_reference_images(story, scene, outdir, style_ref):
    """Return style reference plus up to three scene-specific identity portraits."""
    registry = story.get("character_registry", [])
    by_name = {item.get("name"): item for item in registry}
    refs = [style_ref]
    priority = {"villain": 0, "protagonist": 1, "supporting": 2, "creature": 3}
    scene_names = sorted(
        scene.get("characters_present", []),
        key=lambda name: priority.get(by_name.get(name, {}).get("importance"), 4)
    )
    for name in scene_names:
        item = by_name.get(name)
        if not item:
            continue
        path = character_reference_for_entry(outdir, item)
        if path.exists():
            refs.append(path)
        if len(refs) == 4:
            break
    return refs


def director_camera_brief(scene):
    """Cycle through motivated movie-shot setups to prevent repeated front portraits."""
    number = int(scene.get("number", 1))
    cast = scene.get("characters_present", [])
    setups = [
        "Extreme wide establishing view from behind the location; people small in frame, environment tells the story.",
        "Over-the-shoulder dialogue shot from behind the listener; speaker looks toward listener, not lens.",
        "Reverse close-up from beside the listener; speaker in three-quarter profile, eyes aimed just off lens at listener.",
        "Side-profile two-shot; both characters face each other with clear connected eyelines.",
        "Rear three-quarter view behind the main character as they watch the action ahead.",
        "Low-angle action shot from near floor level; characters look toward the threat or event.",
        "High-angle view from above and off to one side; characters focus on a clue or object below.",
        "Insert detail of hands, evidence or a meaningful object; faces absent or secondary.",
        "Side tracking view as characters move through a layered foreground and background.",
        "Wide master shot from behind one character toward others across the room; nobody looks at lens.",
        "Dutch-angle reaction shot from beside the action; gaze points toward the cause of danger.",
        "Motivated 360-degree orbit viewpoint at an unusual rear/side position; characters focus on each other or the event."
    ]
    setup = setups[(number - 1) % len(setups)]
    if len(cast) <= 1 and number % 12 in (1, 3, 8):
        setup += " Keep the lone character turned away, in profile, or looking into the environment; not a front portrait."
    if len(cast) >= 2:
        setup += " Dialogue eyelines connect character-to-character. Speaker looks at listener; listener looks at speaker. Never both stare at camera."
    return setup


def build_scene_prompt(story, scene, style):
    cast_names = scene.get("characters_present", [])
    cast = ", ".join(cast_names) or "no named character; environment-focused scene"
    registry = {item.get("name"): item for item in story.get("character_registry", [])}
    scene_bibles = "\\n".join(
        f"{name}: immutable physical identity: {registry[name].get('immutable_identity', registry[name].get('visual_bible', ''))}; this book's costume: {registry[name].get('story_costume', 'follow visual_bible')}; full visual bible: {registry[name].get('visual_bible', '')}"
        for name in cast_names if name in registry
    )
    dialogue = scene.get("dialogue", [])
    dialogue_text = "\\n".join(
        f'{item.get("speaker", "Unknown")}: "{item.get("line", "")}"'
        for item in dialogue if isinstance(item, dict)
    ) or "No spoken dialogue in this scene."
    direction = (
        f"Shot: {scene.get('shot_type', 'cinematic medium shot')}. "
        f"Camera: {scene.get('camera_angle', 'eye-level')}. "
        f"Composition: {scene.get('composition', '')}. "
        f"Action and pose: {scene.get('action_pose', '')}. "
        f"Gaze: {scene.get('gaze', '')}. "
        f"Expression: {scene.get('expression', '')}. "
        f"Lighting: {scene.get('lighting', '')}. "
    )
    scene_text = str(scene.get("text", "")).strip()
    return (
        f"Art direction: {story.get('art_direction', STYLE_LOCK)}. "
        f"Global character bible: {story.get('character_bible', '')}. "
        f"Characters visible in this scene: {cast}. "
        f"Exact visual identities required for this scene: {scene_bibles}. "
        "Match every recurring character's face, facial proportions, hair, eye shape/colour, skin tone, body proportions and permanent marks exactly. Do not merge identities or substitute one character for another. Clothing follows this book's story_costume and stays consistent unless the story explicitly describes a change. "
        "SCENE TEXT IS THE SOURCE OF TRUTH: depict the literal events, location, time, objects, character actions, positions, relationships and emotional beat described in the text below. Do not merely create a generic mood illustration. Do not omit important actions or props, invent contradictory actions, change who is doing what, or move the scene to a different place. "
        f"Exact story text for this scene: {scene_text}. "
        "When the text says characters are talking together in a room, visibly stage them in that room as a believable conversation: place each person in the stated position (standing, sitting, leaning, walking, etc.), facing and looking toward their conversation partner, with natural conversational gestures and clear spatial relationships. If one is standing and another sitting, show that difference explicitly. Show the room and relevant furniture/props, not an abstract background. If dialogue is present, depict the listed speakers as the people visibly speaking/listening in the scene; their positions and expressions must support the exchange. "
        f"Dialogue for this scene (speaker and wording must match exactly; bubble text is added during page layout): {dialogue_text}. "
        "Do not depict speech bubbles or lettering inside the generated illustration; the layout renderer will add the exact dialogue bubbles afterward. Do not visually imply a speaker is saying a line assigned to someone else. "
        f"DIRECTOR'S CAMERA COVERAGE GUIDE (adapt to the exact story beat): {director_camera_brief(scene)} "
        f"{direction} "
        "Use the planned shot_type, camera_angle, composition, action_pose and gaze as scene-specific staging when clear, but the narrative text overrides any conflicting generic camera advice. The camera coverage guide must never force an unmotivated shot. "
        "Blocking and eyeline rules: characters face and look toward the person they address, the object they handle, or the action they follow. Do not make characters face the viewer by default. No front-facing lineup, cast portrait, or everyone looking into the lens. A speaker must not look into the lens when speaking to another person. Use natural backs, profiles, over-the-shoulder views and off-camera eyelines when the shot calls for them. "
        f"Style lock: {STYLE_LOCK}. Create one composed feature-film frame with strong foreground, midground and background depth, motivated camera placement, natural anatomy and believable physical action. Preserve each character's exact identity and costume across all scenes; only camera angle, pose and expression may change. "
        f"Scene-specific visual direction: {scene.get('visual_prompt', '')}. "
        f"FINAL NON-NEGOTIABLE IDENTITY LOCK — this overrides any conflicting appearance detail above: {scene_bibles}. "
        "Every visible recurring character must retain the exact same face, hair, eye colour, permanent marks, body type, and fixed outfit defined in the registry. Never add a disguise, alternate hairstyle, changed eye colour, different build, or new identifying mark. "
        "Do not add characters who are not in this scene. No words, lettering, logos or watermark."
    )

def create_reference_images(story, outdir):
    scenes_dir = outdir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    style_ref = outdir / "style_reference.png"
    cast_ref = outdir / "character_reference_sheet.png"
    character_refs_dir = outdir / "character_refs"
    character_refs_dir.mkdir(parents=True, exist_ok=True)

    if not style_ref.exists():
        style_prompt = (
            "Create a master visual-style reference image for an original mystery-adventure "
            "anime feature film: a dramatic ancient mountain city at twilight, cinematic "
            "composition, believable architecture and natural materials, painterly 2D anime "
            "realism, semi-realistic detail, elegant consistent linework, restrained cel shading, "
            "rich but controlled colour grading, atmospheric depth, premium hand-painted film frame. "
            "No text, no characters, no logos, no watermark. " + STYLE_LOCK
        )
        cf_image(style_prompt, style_ref)

    if not cast_ref.exists():
        cast_prompt = (
            "Create a clean character identity reference sheet showing ONLY the following "
            "story characters, each as a separate full-body figure with clear spacing, neutral "
            "standing pose, consistent scale, front three-quarter view, and a simple muted "
            "background. Preserve each person's distinctive face, hairstyle, eye colour, body "
            "type, clothing and identifying marks exactly as described. Do not add extra people, "
            "creatures, labels, text or panels. Character bible: "
            + str(story.get("character_bible", "Original young-adult human protagonists"))
            + ". Render in this exact style: " + STYLE_LOCK
        )
        cf_image(cast_prompt, cast_ref, reference_images=[style_ref])

    # Generate identity portraits for every recurring character, including supporting
    # characters and the villain. One-scene characters are described in prompts but do
    # not consume an extra reference-image generation.
    registry = story.get("character_registry", [])
    appearances = {}
    for scene in story.get("scenes", []):
        for visible_name in scene.get("characters_present", []):
            appearances[visible_name] = appearances.get(visible_name, 0) + 1
    for item in registry:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        if appearances.get(name, 0) < 2 and item.get("importance") != "villain":
            continue
        portrait_path = character_reference_path(outdir, name)
        if portrait_path.exists() and portrait_path.stat().st_size > 1024:
            continue
        permanent_ref = library_reference_path(item.get("visual_id")) if item.get("visual_id") else None
        portrait_prompt = (
            f"Create this book's canonical full-body model sheet for {name}. "
            f"Story role: {item.get('role', '')}. Importance: {item.get('importance', '')}. "
            f"CANONICAL VISUAL BIBLE — follow exactly: {item.get('visual_bible', '')}. "
            f"STORY COSTUME — follow exactly: {item.get('story_costume', '')}. "
            "Show one figure only, front three-quarter view, neutral standing pose, clear face, "
            "body proportions and permanent marks, against a simple muted background. "
            "This is a continuity reference, not an action scene. If a second reference image "
            "is supplied, use it ONLY to preserve the character's face, anatomy, hair, colours "
            "and permanent marks. Do NOT copy its clothing; use this book's story costume. "
            "Keep the face, hair, body, marks, outfit design and outfit colours fixed for every "
            "scene in this book. " + STYLE_LOCK
        )
        refs = [style_ref] + ([permanent_ref] if permanent_ref else [])
        cf_image(portrait_prompt, portrait_path, reference_images=refs)
    return [style_ref, cast_ref]


def ensure_character_library_references(force_refresh=False):
    """Validate all 12 identity references and use a character-free style sample.

    Never use Character 001 (or any other character portrait) as a style reference:
    image models can transfer distinctive facial marks and identity features.
    """
    models = sorted(load_character_library(), key=lambda item: item.get("visual_id") != "visual_001")
    if not models:
        raise RuntimeError("The permanent character library is empty.")
    if not CF_ACCOUNT or not CF_TOKEN:
        raise RuntimeError("Cloudflare credentials are required to build character references.")

    style_path = ROOT / "character_library" / "style_reference.png"
    style_path.parent.mkdir(parents=True, exist_ok=True)

    def valid_reference(path):
        try:
            with Image.open(path) as im:
                im.verify()
            return path.is_file() and path.stat().st_size > 1024
        except Exception:
            return False

    if not valid_reference(style_path):
        style_prompt = (
            "Create a CHARACTER-FREE style reference image: a cinematic misty mountain village "
            "street at blue-hour with lantern light, believable architecture, natural stone, "
            "wood and fabric textures, rich but restrained film colour grading, painterly 2D "
            "anime realism, grounded perspective, semi-realistic detail, elegant fine linework, "
            "soft painterly cel shading, atmospheric depth. Environment only; absolutely no "
            "people, faces, creatures, silhouettes, statues, portraits, scars, or character marks. "
            "No text, labels, logos or watermark. This image is for rendering STYLE ONLY, not identity."
        )
        cf_image(style_prompt, style_path, width=1024, height=1024)
        if not valid_reference(style_path):
            raise RuntimeError("Could not create the character-free style reference.")

    created = []
    force_refresh = force_refresh or os.environ.get("REFRESH_CHARACTER_REFERENCES", "").strip() == "1"
    backup_root = ROOT / "character_library" / "models" / "reference_backups"
    backup_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if force_refresh:
        backup_root = backup_root / backup_stamp
        backup_root.mkdir(parents=True, exist_ok=True)
        logging.warning("Forced reference refresh requested; existing portraits will be backed up to %s", backup_root)

    for model in models:
        path = Path(model["_reference_path"])
        if valid_reference(path) and not force_refresh:
            continue
        name = f"Character {int(str(model['visual_id']).split('_')[-1])}"
        backup = backup_root / f"{model['visual_id']}.png" if force_refresh else None
        if force_refresh and valid_reference(path):
            shutil.copy2(path, backup)
        prompt = (
            f"Create a permanent full-body identity reference portrait for {name}. Show exactly "
            "one character, front three-quarter view, neutral standing pose, entire body visible, "
            "face clearly readable, simple muted background. Follow ONLY this character's canonical "
            "identity: " + str(model.get("immutable_identity", "")) + ". "
            "Permanent marks belong exclusively to this character; do not invent or copy scars, "
            "moles, freckles, birthmarks, or other identifying marks from any other character. "
            "Keep anatomy, colours, proportions, species and marks fixed. Use the style of the "
            "supplied character-free environment image only; do not derive identity, face, hair, "
            "body, clothing or marks from it. Style: cinematic 2D anime realism, hand-painted "
            "feature-film illustration, grounded believable anatomy, natural expressions, realistic "
            "materials, restrained fantasy, fine linework, painterly cel shading, controlled colour. "
            "No 3D CGI, chibi, childish cartoon, plastic skin or photorealism. No text, labels, "
            "panels, logos or watermark."
        )
        try:
            cf_image(prompt, path, width=1024, height=1024, reference_images=[style_path])
            if not valid_reference(path):
                raise RuntimeError(f"Reference image generation failed validation for {name}.")
        except Exception:
            if backup and backup.is_file():
                shutil.copy2(backup, path)
                logging.warning("Restored prior reference after failed refresh for %s", name)
            raise
        created.append(name)
        logging.info("Created permanent visual reference: %s (%s)", name, model["visual_id"])

    missing = [m["visual_id"] for m in models if not valid_reference(Path(m["_reference_path"]))]
    if missing:
        raise RuntimeError("Character reference library remains incomplete: " + ", ".join(missing))
    logging.info("Permanent character library ready: %s models; newly created: %s",
                 len(models), ", ".join(created) if created else "none")
    return [Path(m["_reference_path"]) for m in models]

def wrap_text(text, font_name, font_size, max_width, c):
    words = text.split()
    lines, line = [], ""
    for word in words:
        trial = word if not line else line + " " + word
        if c.stringWidth(trial, font_name, font_size) <= max_width:
            line = trial
        else:
            if line: lines.append(line)
            line = word
    if line: lines.append(line)
    return lines

def _page_font(size, bold=False):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def build_page_images(story, scenes_dir, pages_dir, trim):
    """Place readable story text in a translucent black box over the bottom of each image."""
    page_w, page_h = TRIM_SIZES[trim]
    scale = 1536 / max(page_w, page_h)
    target_size = (int(round(page_w * scale)), int(round(page_h * scale)))
    pages_dir = Path(pages_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)
    for scene in story["scenes"]:
        source = Path(scenes_dir) / f"scene_{scene['number']:03d}.png"
        target = pages_dir / f"page_{scene['number']:03d}.png"
        with Image.open(source) as opened:
            page = ImageOps.fit(opened.convert("RGB"), target_size, method=Image.Resampling.LANCZOS)
        width, height = page.size
        pad = max(30, int(width * 0.032))
        box_top = int(height * 0.70)
        overlay = Image.new("RGBA", page.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            (pad, box_top, width - pad, height - pad),
            radius=max(18, int(width * 0.018)),
            fill=(0, 0, 0, 150)
        )
        heading_font = _page_font(max(24, int(width * 0.026)), bold=True)
        body_font = _page_font(max(22, int(width * 0.022)), bold=False)
        inner_width = width - 2 * (pad + int(width * 0.025))

        def wrap_pixels(value, font):
            lines, current = [], ""
            for word in str(value).split():
                trial = word if not current else current + " " + word
                if draw.textbbox((0, 0), trial, font=font)[2] <= inner_width:
                    current = trial
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)
            return lines

        heading_lines = wrap_pixels(scene.get("heading", ""), heading_font)
        body_lines = wrap_pixels(scene.get("text", ""), body_font)
        line_height = int(body_font.size * 1.32)
        heading_height = int(heading_font.size * 1.3) if heading_lines else 0
        available = height - pad - box_top - int(height * 0.045) - heading_height
        max_lines = max(1, int(available / line_height))
        if len(body_lines) > max_lines:
            raise ValueError(
                f"Scene {scene['number']} needs {len(body_lines)} caption lines but only "
                f"{max_lines} fit. Shorten the text before publishing."
            )
        x = pad + int(width * 0.025)
        y = box_top + int(height * 0.025)
        if heading_lines:
            draw.text((x, y), heading_lines[0], font=heading_font, fill=(255, 224, 145, 255))
            y += heading_height + int(height * 0.008)
        for line in body_lines:
            draw.text((x, y), line, font=body_font, fill=(255, 255, 255, 255))
            y += line_height
        # Render authored dialogue as readable speech bubbles in the upper/middle
        # image area, keeping the bottom caption panel separate.
        dialogue = scene.get("dialogue", [])
        if isinstance(dialogue, list):
            bubble_font = _page_font(max(22, int(width * 0.020)), bold=False)
            speaker_font = _page_font(max(20, int(width * 0.017)), bold=True)
            bubble_top = int(height * 0.06)
            bubble_max_width = int(width * 0.58)
            bubble_gap = int(height * 0.018)
            for bubble_index, item in enumerate(dialogue[:2]):
                if not isinstance(item, dict) or not item.get("line"):
                    continue
                speaker = str(item.get("speaker", "")).strip()
                line = str(item.get("line", "")).strip()
                bubble_lines = wrap_pixels(line, bubble_font)
                speaker_lines = wrap_pixels(speaker, speaker_font)[:1]
                text_h = len(bubble_lines) * int(bubble_font.size * 1.25)
                speaker_h = len(speaker_lines) * int(speaker_font.size * 1.2)
                bubble_h = speaker_h + text_h + int(height * 0.035)
                bubble_w = min(bubble_max_width, max(int(width * 0.24), max(
                    draw.textbbox((0, 0), ln, font=bubble_font)[2] for ln in bubble_lines
                ) + int(width * 0.05)))
                bx = pad if bubble_index == 0 else width - pad - bubble_w
                by = bubble_top + bubble_index * (bubble_h + bubble_gap)
                if by + bubble_h >= box_top - int(height * 0.02):
                    continue
                draw.rounded_rectangle(
                    (bx, by, bx + bubble_w, by + bubble_h),
                    radius=max(18, int(width * 0.022)),
                    fill=(255, 250, 236, 232),
                    outline=(40, 35, 30, 210),
                    width=max(2, int(width * 0.002))
                )
                tx = bx + int(width * 0.022)
                ty = by + int(height * 0.012)
                for ln in speaker_lines:
                    draw.text((tx, ty), ln, font=speaker_font, fill=(85, 55, 35, 255))
                    ty += int(speaker_font.size * 1.2)
                for ln in bubble_lines:
                    draw.text((tx, ty), ln, font=bubble_font, fill=(25, 25, 25, 255))
                    ty += int(bubble_font.size * 1.25)
        page = Image.alpha_composite(page.convert("RGBA"), overlay).convert("RGB")
        page.save(target, "PNG", optimize=True)
    return pages_dir


def build_pdf(story, images_dir, pdf_path, trim, bleed, text_size):
    w_in, h_in = TRIM_SIZES[trim]
    W, H = w_in * inch, h_in * inch
    c = canvas.Canvas(str(pdf_path), pagesize=(W, H), pageCompression=1)
    for scene in story["scenes"]:
        img_path = Path(images_dir) / f"page_{scene['number']:03d}.png"
        with Image.open(img_path) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=95)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=W, height=H, preserveAspectRatio=False, mask="auto")
        c.showPage()
    c.save()

def make_kindle_html(story, images_dir, out_path):
    title = escape(story.get("title", "Untitled"))
    html = ["""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>""" + title + """</title>
<style>html,body{margin:0;padding:0;background:#111}body{max-width:900px;margin:auto}img{width:100%;height:auto;display:block}</style></head><body>"""]
    html.append(f"<h1 style='color:white;text-align:center'>{title}</h1>")
    for scene in story["scenes"]:
        page = Path(images_dir) / f"page_{scene['number']:03d}.png"
        html.append(f"<section><img src='pages/{page.name}' alt='{escape(scene.get('heading', 'Story page'))}'/></section>")
    html.append("</body></html>")
    Path(out_path).write_text("\\n".join(html), encoding="utf-8")


def create_contact_sheet(images_dir, out_path):
    paths = sorted(images_dir.glob("scene_*.png"))
    if not paths: return
    thumb_w, thumb_h = 180, 180
    cols = 5
    rows = (len(paths)+cols-1)//cols
    sheet = Image.new("RGB", (cols*thumb_w, rows*(thumb_h+22)), "white")
    d = ImageDraw.Draw(sheet)
    for idx, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        im.thumbnail((thumb_w,thumb_h))
        x = (idx%cols)*thumb_w; y = (idx//cols)*(thumb_h+22)
        sheet.paste(im, (x+(thumb_w-im.width)//2,y))
        d.text((x+6,y+thumb_h+3), p.stem.replace("scene_","Scene "), fill="black")
    sheet.save(out_path, quality=88)

def make_listing(story, outdir):
    listing = {
        "title": story.get("title",""),
        "subtitle": story.get("subtitle",""),
        "author": story.get("author","Your Author Name"),
        "description": story.get("description",""),
        "keywords": story.get("keywords",[])[:7],
        "suggested_categories": story.get("categories",[]),
        "language": "English",
        "formats_to_prepare": ["Paperback", "Kindle eBook"],
        "ai_generated_content": "Review current KDP policy and answer accurately before submission.",
        "status": "DRAFT — verify every field and current KDP requirements"
    }
    (outdir/"kdp_listing_draft.json").write_text(json.dumps(listing, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "AMAZON KDP LISTING DRAFT — REVIEW BEFORE USE",
        f"Title: {listing['title']}",
        f"Subtitle: {listing['subtitle']}",
        f"Author: {listing['author']}",
        "",
        "Description:",
        listing["description"],
        "",
        "Suggested keywords (verify in KDP):",
        *[f"- {k}" for k in listing["keywords"]],
        "",
        "Suggested categories (verify availability in KDP):",
        *[f"- {k}" for k in listing["suggested_categories"]],
        "",
        "AI disclosure: Review KDP's current AI-generated-content policy and disclose accurately.",
        "Price: Choose after reviewing print cost, marketplace, royalty, and competitor positioning.",
        "Rights: Confirm you hold the rights to all text and images before submission.",
    ]
    (outdir/"kdp_listing_draft.txt").write_text("\n".join(lines), encoding="utf-8")

def write_checklist(outdir):
    content = """KDP PUBLISHING CHECKLIST — HUMAN REVIEW REQUIRED

ACCOUNT
[ ] Sign in directly at https://kdp.amazon.com/ (do not give credentials to this app)
[ ] Complete any account, identity, payment, or tax verification yourself

BOOK DETAILS
[ ] Confirm title/subtitle/author exactly match manuscript and cover
[ ] Review description, keywords, categories, language, reading age if relevant
[ ] Verify rights and ownership of all generated content
[ ] Disclose AI-generated text/images accurately under current KDP rules

ILLUSTRATION AND CHARACTER REVIEW — REQUIRED BEFORE PUBLICATION
[ ] Review the 50-scene contact sheet and all 50 full-size page images
[ ] Check that every character keeps the same face, hair, eyes, body, and identifying marks
[ ] Check that each character's clothing stays consistent within the book
[ ] Check that every image matches its scene action, emotion, and camera direction
[ ] Check that story text is readable and does not cover important image details
[ ] Fix any weak, repeated, distorted, or mismatched illustrations
NOTE: Automated image checks verify file integrity, resolution, blank images, and near-duplicates.
They do NOT verify character identity, scene accuracy, or artistic quality.

PAPERBACK
[ ] Confirm current trim size and bleed settings
[ ] Confirm current interior PDF and image specifications
[ ] Generate a full-wrap cover using KDP's calculator/template for exact page count and paper
[ ] Upload interior and cover separately
[ ] Inspect KDP Print Previewer; resolve every warning and error

KINDLE EBOOK
[ ] Use the HTML as a source only; package/convert and validate with Kindle Create or another KDP-supported tool
[ ] Check images, navigation, accessibility, and reading experience on device previews
[ ] Do not assume the print PDF is the correct Kindle file

FINAL
[ ] Review pricing and territories
[ ] Review all terms and details
[ ] Publish only after your own explicit approval
"""
    (outdir/"publishing_checklist.txt").write_text(content, encoding="utf-8")

