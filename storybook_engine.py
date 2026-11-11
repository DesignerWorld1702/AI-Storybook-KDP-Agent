import os, json, re, time, base64, io, textwrap, logging, difflib
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageDraw, ImageFont
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
            f"Use reference image {index} only as the physical-identity guide for {character_name}: preserve face, facial proportions, skin tone, hair identity, eye shape/colour, body proportions and permanent marks. Ignore reference clothing and accessories when the story prompt specifies a different costume. Never merge identities. "
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
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Repair the common trailing-comma mistake before asking the model again.
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(candidate)

def extract_json(text, repair_context="story data"):
    try:
        return _parse_json_once(text)
    except (ValueError, json.JSONDecodeError) as original_error:
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
    if plan_path and plan_path.exists():
        try:
            cached = json.loads(plan_path.read_text(encoding="utf-8"))
            if cached.get("story_schema_version") == 4 and len(cached.get("outline", [])) == 50 and cached.get("character_registry") and cached.get("principal_villain"):
                plan = cached
        except Exception:
            plan = None

    if plan is None:
        metadata_prompt = common + """
Create ONLY the book metadata and character registry for an original illustrated feature-film story.
Return one compact valid JSON object, no markdown, with these keys:
{
 "title":"...",
 "subtitle":"...",
 "author":"Your Author Name",
 "description":"A compelling 100-140 word book description",
 "keywords":["seven distinct short search phrases"],
 "categories":["suggested broad category 1","suggested broad category 2"],
 "audience":"Family / all ages",
 "character_registry":[
  {"name":"Fresh story-specific name","visual_id":null,"role":"story role","importance":"protagonist/supporting/villain/creature","immutable_identity":"physical traits and permanent marks","story_costume":"this book's clothing and accessories","visual_bible":"complete physical identity plus story costume"}
 ],
 "principal_villain":"Exact name of the single principal villain",
 "art_direction":"Consistent palette, lighting and world atmosphere"
}
Keep the registry concise and plot-driven: create only the characters the story genuinely needs. A typical story may have one to three protagonists, supporting roles as required, and a consequential antagonist; do not pad the cast to hit a fixed count. In real-world mode every character must be human and grounded. In fantasy mode add creatures only if they serve the plot. Never include an outline or extra keys.
"""
        plan = extract_json(cf_text(metadata_prompt, max_tokens=2600), "compact book metadata and character registry")
        if not isinstance(plan, dict):
            raise ValueError("Book metadata response was not a JSON object.")

        outline_prompt = common + """
Create a fresh, original 50-scene cinematic story outline for the metadata and characters already established.
Return EXACTLY 50 numbered lines as plain text, not JSON, not markdown bullets.
Format every line exactly: 01 | one or two sentences describing one distinct event.
Use 8-18 words per scene. No quotation marks, no JSON, no headings, no blank lines.
Five acts: scenes 1-10 setup and inciting mystery; 11-20 investigation and setback;
21-30 revelation and changed plan; 31-40 escalation and sacrifice; 41-50 confrontation and resolution.
Every scene must introduce a new physical action and consequence. Never repeat a chase,
clue discovery, argument, journey, or other event with slightly changed wording.
"""
        last_outline_error = None
        parsed_outline = None
        for outline_attempt in range(1, 4):
            raw_outline = cf_text(outline_prompt, max_tokens=2400)
            rows = []
            for line in raw_outline.splitlines():
                match = re.match(r"^\s*(\d{1,2})\s*[|.)-]\s*(.+?)\s*$", line)
                if match:
                    rows.append({"number": int(match.group(1)), "beat": match.group(2).strip()})
            if len(rows) == 50 and [item["number"] for item in rows] == list(range(1, 51)):
                beats = [re.sub(r"[^a-z0-9 ]", "", item["beat"].lower()).strip() for item in rows]
                if all(len(beat.split()) >= 8 for beat in beats) and len(set(beats)) >= 45:
                    parsed_outline = rows
                    break
                last_outline_error = "Outline contains short or repeated scene beats."
            else:
                last_outline_error = f"Expected 50 numbered lines; parsed {len(rows)}."
            if outline_attempt < 3:
                outline_prompt += f"""

RETRY {outline_attempt}: {last_outline_error}
Return all 50 lines again, using the exact two-digit number | event format.
Replace repeated scenes with new actions, discoveries, decisions and consequences.
Do not add JSON, quotation marks, headings or explanatory text.
"""
        if parsed_outline is None:
            raise ValueError(f"Outline generation failed after 3 attempts: {last_outline_error}")
        plan["outline"] = parsed_outline
        plan["character_bible"] = "\n".join(
            f'{item.get("name", "")}: {item.get("visual_bible", "")}'
            for item in plan.get("character_registry", []) if isinstance(item, dict)
        )
        plan["story_schema_version"] = 4
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
        if not fantasy_mode:
            forbidden_visual_ids = {"visual_009", "visual_010", "visual_011", "visual_012"}
            forbidden_terms = ("dragon", "goblin", "unicorn", "fantasy creature", "mythical", "magical", "supernatural", "talking panda")
            for item in registry:
                identity_text = " ".join(str(item.get(key, "")) for key in ("role", "importance", "immutable_identity", "visual_bible")).lower()
                if item.get("visual_id") in forbidden_visual_ids or item.get("importance") in ("creature", "fantasy_creature") or any(term in identity_text for term in forbidden_terms):
                    raise ValueError(f"Real-world story contains a fantasy character: {item.get('name')}")
        plan["character_bible"] = "\\n\\n".join(
            f"{item['name']} — {item.get('role', '')}: {item.get('visual_bible', '')}"
            for item in registry
        )
        if plan_path:
            plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    outline = plan["outline"]
    scenes = []
    # Five scenes per request keeps responses smaller and makes recovery granular.
    for first in range(1, 51, 5):
        last = min(first + 4, 50)
        batch_path = workdir / f"scene_batch_{first:02d}_{last:02d}.json" if workdir else None
        batch_scenes = None
        if batch_path and batch_path.exists():
            try:
                cached_batch = json.loads(batch_path.read_text(encoding="utf-8"))
                if len(cached_batch.get("scenes", [])) == last - first + 1 and all(isinstance(x, dict) and len(str(x.get("text", "")).split()) >= 35 and x.get("heading") and x.get("visual_prompt") for x in cached_batch["scenes"]):
                    batch_scenes = cached_batch["scenes"]
            except Exception:
                batch_scenes = None

        if batch_scenes is None:
            batch_outline = outline[first - 1:last]
            batch_prompt = common + f"""
Book title: {plan.get("title", title_hint or "Untitled")}
Character registry (canonical identities; never rename or redesign within this book):
{json.dumps(plan.get("character_registry", []), ensure_ascii=False)}
CAST RULE: For characters_present, use ONLY exact names from the character registry above. Never use permanent-library base names unless that exact name is registered for this book. Never invent incidental named people (for example, Mr. Jenkins); unnamed crowds/guards may remain part of the environment but are not named characters.
Principal villain: {plan.get("principal_villain", "")}
Character bible: {plan.get("character_bible", "")}
Art direction: {plan.get("art_direction", "")}
Full story outline for continuity:
{json.dumps(outline, ensure_ascii=False)}
Specific beats to expand now:
{json.dumps(batch_outline, ensure_ascii=False)}

Expand ONLY scenes {first} through {last} into detailed illustrated-book scene records.
Each scene page text must be 40–55 words (aim for {scene_words}), written as polished narrative prose rather than camera notes. Include concrete action, emotion, sensory detail and purposeful dialogue where natural. Every scene must advance the plot or change a relationship; no recap-only pages, repeated planning conversations, repeated rescues, or filler epilogues. Keep headings distinct. Make visual prompts cinematic, specific and varied. Do not repeat the same camera angle or static talking pose. For characters with visual_id, preserve immutable_identity and permanent marks, but follow story_costume for clothing; costume stays consistent within this book.
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
   "visual_prompt":"Detailed scene illustration prompt"
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
            word_count = len(str(scene.get("text", "")).split())
            if word_count < 35 or word_count > 65:
                # Repair an out-of-range page locally instead of failing the whole book.
                # Keep the existing scene's events, characters and tone; only revise its prose.
                # Keep the full original page unchanged across retries. Previously, each
                # too-short response replaced the source, causing later repairs to shrink further.
                source_text = str(scene.get("text", "")).strip()
                last_repair_error = None
                repaired_text = None
                previous_attempt_note = ""
                for repair_attempt in range(1, 6):
                    repair_prompt = (
                        "Rewrite the following illustrated storybook page as polished narrative prose "
                        "of 45–55 words (never fewer than 35). Preserve the exact events, facts, "
                        "characters, emotional beat, and continuity. Do not add a new event, heading, "
                        "label, or explanation. Do not summarize. Count the words before answering. "
                        "Return only the page paragraph.\n\n"
                        f"Book: {plan.get('title', 'Untitled')}\n"
                        f"Scene {number}: {scene.get('heading', '')}\n"
                        f"Required length: 45–55 words. You MUST write at least 40 words.\n"
                        f"{previous_attempt_note}"
                        f"Original page text ({len(source_text.split())} words): {source_text}"
                    )
                    candidate = cf_text(repair_prompt, max_tokens=700)
                    candidate = candidate.strip().strip('"').strip()
                    candidate_count = len(candidate.split())
                    if 35 <= candidate_count <= 65:
                        repaired_text = candidate
                        break
                    last_repair_error = f"repair {repair_attempt} returned {candidate_count} words"
                    previous_attempt_note = (
                        f"Your previous attempt contained {candidate_count} words and was invalid. "
                        "Rewrite the complete original page at the required length; do not shorten it.\\n"
                    )
                if repaired_text is None:
                    raise ValueError(
                        f"Scene {number} page text could not be repaired to 35–65 words "
                        f"after 5 attempts ({last_repair_error})."
                    )
                scene["text"] = repaired_text
                logging.info("Repaired scene %s page text to %s words.", number, len(repaired_text.split()))
            for key in ("shot_type", "camera_angle", "composition", "action_pose", "gaze", "expression", "lighting"):
                scene.setdefault(key, "")
            scene.setdefault("characters_present", [])
            known_names = {item["name"] for item in plan.get("character_registry", [])}
            visible_names = scene.get("characters_present", [])
            if not isinstance(visible_names, list):
                raise ValueError(f"Scene {number} characters_present must be a list.")
            # Permanent visual slots have no story names. The current book's
            # registry is the sole authority for character names.
            character_aliases = {}
            generic_labels = {
                "minions", "guards", "soldiers", "villagers", "townspeople",
                "crowd", "crowds", "bandits", "enemies"
            }
            normalized_names = []
            for name in visible_names:
                if not isinstance(name, str):
                    normalized_names.append(name)
                    continue
                clean_name = name.strip()
                if clean_name.casefold() in generic_labels:
                    continue
                normalized_names.append(
                    character_aliases.get(clean_name, character_aliases.get(clean_name.casefold(), clean_name))
                )
            visible_names = list(dict.fromkeys(normalized_names))
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
            unknown_names = [name for name in visible_names if name not in known_names]
            if unknown_names:
                # The scene model sometimes invents incidental names or reuses a
                # permanent-library name that this story did not select. The story
                # registry is the authority for visible cast; do not let an
                # unregistered name create an unintended character design.
                logging.warning(
                    "Scene %s mentioned unregistered visible names %s; excluding them from the illustrated cast.",
                    number, unknown_names
                )
                visible_names = [name for name in visible_names if name in known_names]
                scene["characters_present"] = visible_names
                scene["visual_prompt"] = (
                    str(scene.get("visual_prompt", "")).strip()
                    + "\\nSTRICT CAST LOCK: Depict only these registered characters: "
                    + (", ".join(visible_names) if visible_names else "no named character; environment-only composition")
                    + ". Do not depict or invent any other named person or character."
                )
            scenes.append(scene)

        # Cache only a fully validated batch so interrupted/invalid responses are regenerated.
        if batch_path:
            batch_path.write_text(
                json.dumps({"scenes": batch_scenes}, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

    plan.pop("outline", None)
    plan["scenes"] = scenes
    plan["art_direction"] = f"{STYLE_LOCK}\n{plan.get('art_direction', '')}"
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
    visual_id = item.get("visual_id")
    if visual_id:
        permanent = library_reference_path(visual_id)
        if permanent:
            return permanent
    return character_reference_path(outdir, item.get("name", ""))


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


def build_scene_prompt(story, scene, style):
    cast_names = scene.get("characters_present", [])
    cast = ", ".join(cast_names) or "no named character; environment-focused scene"
    registry = {item.get("name"): item for item in story.get("character_registry", [])}
    scene_bibles = "\\n".join(
        f"{name}: immutable physical identity: {registry[name].get('immutable_identity', registry[name].get('visual_bible', ''))}; this book's costume: {registry[name].get('story_costume', 'follow visual_bible')}; full visual bible: {registry[name].get('visual_bible', '')}"
        for name in cast_names if name in registry
    )
    direction = (
        f"Shot: {scene.get('shot_type', 'cinematic medium shot')}. "
        f"Camera: {scene.get('camera_angle', 'eye-level')}. "
        f"Composition: {scene.get('composition', '')}. "
        f"Action and pose: {scene.get('action_pose', '')}. "
        f"Gaze: {scene.get('gaze', '')}. "
        f"Expression: {scene.get('expression', '')}. "
        f"Lighting: {scene.get('lighting', '')}. "
    )
    return (
        f"Art direction: {story.get('art_direction', STYLE_LOCK)}. "
        f"Global character bible: {story.get('character_bible', '')}. "
        f"Characters visible in this scene: {cast}. "
        f"Exact visual identities required for this scene: {scene_bibles}. "
        "Match each recurring character's face, facial proportions, hair identity, eye shape/colour, skin tone, body proportions and permanent marks exactly. Do not merge identities or substitute one character for another. Clothing and accessories must follow this book's story_costume, not the permanent reference image; keep the story costume consistent unless the scene explicitly shows a change. "
        f"{direction} Scene {scene.get('number')}: {scene.get('visual_prompt', '')}. "
        f"Style lock: {STYLE_LOCK}. "
        "Create one cinematic movie frame with clear foreground/background depth, "
        "natural anatomy, purposeful character blocking and a visually distinct camera composition. "
        "Prioritise crisp focus on faces and important story objects, fine but clean linework, "
        "clearly separated hair strands, readable eyes, defined fabric and surface textures, "
        "and sharp environmental details without turning the image photorealistic. "
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
        portrait_path = character_reference_for_entry(outdir, item)
        if item.get("visual_id") and library_reference_path(item.get("visual_id")):
            continue
        if portrait_path.exists() and portrait_path.stat().st_size > 1024:
            continue
        portrait_prompt = (
            f"Create a single-character full-body identity reference portrait of {name}. "
            f"Story role: {item.get('role', '')}. Importance: {item.get('importance', '')}. "
            f"CANONICAL VISUAL IDENTITY — follow exactly and do not improvise: "
            f"{item.get('visual_bible', '')}. "
            "Show one figure only, head and entire body visible, front three-quarter view, "
            "neutral standing pose, clear face and identifying marks, uncluttered muted background. "
            "This is a continuity model sheet, not an action scene. Preserve exact face, hairstyle, "
            "eye colour, proportions, outfit, accessories and colours. " + STYLE_LOCK
        )
        cf_image(portrait_prompt, portrait_path, reference_images=[style_ref])
    return [style_ref, cast_ref]


def ensure_character_library_references():
    """Create missing permanent identity portraits for the complete 12-character pool.

    The first existing Adam portrait is used as the style-only benchmark. If it is
    unavailable in a clean cloud runner, create an Adam benchmark portrait from
    the locked visual bible, then use that same image as the style reference for
    every remaining model.
    """
    models = sorted(load_character_library(), key=lambda item: item.get("visual_id") != "visual_001")
    if not models:
        raise RuntimeError("The permanent character library is empty.")
    if not CF_ACCOUNT or not CF_TOKEN:
        raise RuntimeError("Cloudflare credentials are required to build character references.")

    first = next((m for m in models if m.get("visual_id") == "visual_001"), models[0])
    first_path = Path(first["_reference_path"])
    first_path.parent.mkdir(parents=True, exist_ok=True)

    def valid_reference(path):
        try:
            with Image.open(path) as im:
                im.verify()
            return path.is_file() and path.stat().st_size > 1024
        except Exception:
            return False

    if not valid_reference(first_path):
        prompt = (
            "Create the master character reference portrait for the first character in a "
            "unified illustrated storybook character library. This portrait will be the "
            "style benchmark for the visual library. Show one adult human character, "
            "full body, front three-quarter view, neutral confident standing pose, face clearly "
            "readable, uncluttered muted cinematic background. Permanent identity: "
            + str(first.get("immutable_identity", ""))
            + ". Clothing: charcoal jacket over a dark shirt. Use unified 2D cinematic anime "
            "realism: hand-painted feature-film illustration, semi-realistic adult anatomy, "
            "natural facial proportions, expressive but restrained eyes, consistent fine linework, "
            "painterly cel shading, detailed believable fabric and materials, controlled cinematic "
            "colour grading. Not 3D, not chibi, not childish cartoon, not photorealistic. "
            "No text, labels, panels, logos or watermark."
        )
        cf_image(prompt, first_path, width=1024, height=1024)
        if not valid_reference(first_path):
            raise RuntimeError("Could not create the Character 1 identity reference image.")

    created = []
    for model in models:
        path = Path(model["_reference_path"])
        if valid_reference(path):
            continue
        name = f"Character {int(str(model['visual_id']).split('_')[-1])}"
        prompt = (
            f"Create a permanent full-body character identity reference portrait for {name}. "
            "Show exactly one character, front three-quarter view, neutral natural standing pose, "
            "head and entire body visible, face and identifying features clearly readable, "
            "uncluttered muted cinematic background. This is a clean continuity reference, not "
            "an action scene or a multi-character sheet. Preserve this canonical identity exactly: "
            + str(model.get("immutable_identity", ""))
            + ". The character's identity, anatomy, markings, colours and proportions are fixed. "
            "Clothing should match the character's reference outfit where specified; avoid adding "
            "unnecessary accessories. Use the exact rendering style and colour treatment of the "
            "first reference image, but do NOT copy the first character's face, hair, body, species "
            "or clothing. Style only: unified 2D cinematic anime realism, hand-painted feature-film "
            "illustration, grounded believable anatomy, natural expressions, realistic materials, "
            "restrained purposeful fantasy, consistent fine linework, painterly cel shading, "
            "controlled cinematic colour grading. No 3D CGI, chibi, childish cartoon, oversized "
            "anime eyes, plastic skin or photorealism. No text, labels, panels, logos or watermark."
        )
        cf_image(prompt, path, width=1024, height=1024, reference_images=[first_path])
        if not valid_reference(path):
            raise RuntimeError(f"Reference image generation failed validation for {name}.")
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

def build_pdf(story, images_dir, pdf_path, trim, bleed, text_size):
    w_in, h_in = TRIM_SIZES[trim]
    # Interior PDF is trim-sized. Bleed is intentionally not added automatically:
    # KDP bleed needs precise image extension and margin handling for the selected template.
    W, H = w_in*inch, h_in*inch
    c = canvas.Canvas(str(pdf_path), pagesize=(W,H), pageCompression=1)
    scenes = story["scenes"]
    margin = 0.38*inch
    panel_h = max(1.55*inch, H*0.25)
    for scene in scenes:
        img_path = images_dir / f"scene_{scene['number']:03d}.png"
        im = Image.open(img_path).convert("RGB")
        # Full-width art area; image cropped to fit the upper part of the page.
        art_h = H-panel_h
        target = (int(W*2), int(art_h*2))
        im = ImageOps.fit(im, target, method=Image.Resampling.LANCZOS, centering=(0.5,0.45))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=94)
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, panel_h, width=W, height=art_h, preserveAspectRatio=False, mask="auto")
        c.setFillColorRGB(1,1,1)
        c.rect(0, 0, W, panel_h, fill=1, stroke=0)
        c.setFillColorRGB(0.12,0.12,0.15)
        c.setFont("Helvetica-Bold", 13)
        heading = scene.get("heading", f"Scene {scene['number']}")
        max_width = W - 2*margin
        title_lines = wrap_text(heading, "Helvetica-Bold", 13, max_width, c)[:2]
        y = panel_h - 0.30*inch
        for line in title_lines:
            c.drawString(margin, y, line); y -= 16
        page_text_size = text_size
        c.setFont("Helvetica", page_text_size)
        text_lines = wrap_text(scene["text"], "Helvetica", page_text_size, max_width, c)
        available_lines = max(1, int((y - 0.18*inch) / (page_text_size*1.35)))
        if len(text_lines) > available_lines:
            # Fit this page conservatively without shrinking below 8pt or affecting later pages.
            while len(text_lines) > available_lines and page_text_size > 8:
                page_text_size -= 0.5
                c.setFont("Helvetica", page_text_size)
                text_lines = wrap_text(scene["text"], "Helvetica", page_text_size, max_width, c)
                available_lines = max(1, int((y - 0.18*inch) / (page_text_size*1.35)))
        for line in text_lines[:available_lines]:
            c.drawString(margin, y, line); y -= page_text_size*1.35
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.35,0.35,0.38)
        c.drawRightString(W-margin, 0.14*inch, str(scene["number"]))
        c.showPage()
    c.save()

def make_kindle_html(story, images_dir, out_path):
    title = escape(story.get("title","Untitled"))
    html = ["""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>"""+title+"""</title>
<style>body{font-family:Georgia,serif;max-width:720px;margin:2rem auto;padding:0 1rem;line-height:1.55}h1,h2{text-align:center}figure{margin:1rem 0}img{max-width:100%;height:auto;display:block;margin:auto}figcaption{margin-top:.8rem}</style></head><body>"""]
    html.append(f"<h1>{title}</h1><h2>{escape(story.get('subtitle',''))}</h2><p style='text-align:center'>By {escape(story.get('author','Your Author Name'))}</p>")
    for s in story["scenes"]:
        p = images_dir / f"scene_{s['number']:03d}.png"
        html.append(f"<section><h2>{escape(s.get('heading',''))}</h2><figure><img src='scenes/{p.name}' alt='{escape(s.get('heading','Scene'))}'/><figcaption>{escape(s['text'])}</figcaption></figure></section>")
    html.append("</body></html>")
    out_path.write_text("\n".join(html), encoding="utf-8")

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

