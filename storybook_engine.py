import os, json, re, time, base64, io, textwrap
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageDraw
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
    """Choose a high-resolution image size matching the printable art panel."""
    if trim not in TRIM_SIZES:
        raise ValueError(f"Unsupported trim size: {trim}")
    max_side = int(max_side or os.getenv("STORY_IMAGE_MAX_SIDE", "1536"))
    if max_side < 1024 or max_side > 1920 or max_side % 16:
        raise ValueError("STORY_IMAGE_MAX_SIDE must be a multiple of 16 from 1024 to 1920.")
    page_w, page_h = (value * 72 for value in TRIM_SIZES[trim])
    panel_h = max(1.55 * 72, page_h * 0.25)
    art_w, art_h = page_w, page_h - panel_h
    scale = max_side / max(art_w, art_h)
    width = max(256, int(round((art_w * scale) / 16) * 16))
    height = max(256, int(round((art_h * scale) / 16) * 16))
    if width * height > 4_000_000:
        raise ValueError("Selected scene dimensions exceed FLUX.2 Klein's 4MP output limit.")
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
REUSABLE CHARACTER POOL — select only those needed by this standalone story; never force the full cast into every book.
ADULT HUMANS:
Adam (27): dark chestnut slightly wavy hair, grey-hazel eyes, vertical scar at outer left eyebrow, tall lean athletic build; charcoal jacket and dark shirt; strategic thinker.
Zayn (24): tousled black hair, amber-brown eyes, small mole below right eye, slim athletic build; olive utility jacket; inventive problem-solver.
Rayan (28): short textured dark-brown hair, deep-brown eyes, thin diagonal chin scar, broad athletic build; brown expedition jacket; bold adventurer.
Noah (26): straight black side-parted hair, blue-grey eyes, crescent birthmark near right temple, lean build; black long coat; observant investigator.
Aira (25): long espresso-brown hair, hazel-green eyes, beauty mark below left side of mouth, athletic build; burgundy jacket; decisive leader.
Zara (23): shoulder-length layered black hair, dark-brown eyes, notch in left eyebrow, slim athletic build; black leather jacket; sharp detective.
Hania (22): medium soft chestnut hair, warm dark-hazel eyes, freckles across nose, petite-average build; beige jacket and teal top; empathetic analyst.
Meher (29): dark auburn-black hair tied back, steel-brown eyes, diagonal scar on right cheek, athletic build; forest-green field jacket; survival expert.
FANTASY CREATURES — optional, rare supporting roles:
Drako: intelligent proud dragon with obsidian scales, bronze undertones, amber eyes, bronze-black horns, large dark wings and a glowing ancient chest symbol.
Grim: three-foot moss-green goblin with yellow eyes, pointed ears, one bent ear and a collection of old keys; clever trickster.
Lumi: gentle wise unicorn with pearl-white coat, silver mane, icy-blue eyes, crystal horn and star mark on forehead.
Bamboo: clever loyal black-and-white panda with brown eyes, crescent mark on right shoulder and explorer satchel.
Treat these as visual starting points, not a fixed cast. Create fresh story-specific names, roles, personalities and backgrounds for every book; do not reuse pool names as story names. Keep physical traits and permanent marks stable when reusing a saved visual model. Design a new costume for each book and keep it consistent within that book.
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
    common = f"""
Create an original illustrated feature-film story.
Story direction: mystery, suspense, thriller, adventure and restrained fantasy.
Genre emphasis: {genre}
Audience: family / all ages. Main protagonists must be humans in their 20s.
Visual style (LOCKED): {STYLE_LOCK}
Title hint: {title_hint or "invent a memorable title"}.
Character notes / reusable character pool: {character_notes or DEFAULT_CHARACTER_POOL}
Permanent visual library available to this project:
{json.dumps([{k: v for k, v in model.items() if not k.startswith("_")} for model in load_character_library()], ensure_ascii=False)}
Every story must use fresh character names, not reusable pool labels, and must avoid all previous story character names listed here: {json.dumps(previous_story_character_names(), ensure_ascii=False)}. Select permanent visual models only when they naturally fit the plot; do not force any character into a book. If visual model visual_001 is selected, give it a new story name (never "Adam") while preserving its immutable physical identity exactly. Add visual_id: "visual_001" to that registry entry. Other characters may be original story-specific designs. Humans drive the plot. Build a lively ensemble: several clear main protagonists plus
supporting characters who join, leave, form alliances, disagree, and reveal secrets
naturally as the adventure unfolds. Build at least two clearly defined main protagonists
and at least three meaningful recurring supporting characters, in addition to the villain.
Do not force every pool character into the story.
Include exactly ONE principal villain with a consequential role, distinctive personality,
clear motive, methods, and a meaningful connection to the central conflict. The villain
may be a reusable identity from a villain pool, but create a fresh story-specific costume,
equipment, styling and visual presentation for this book. Keep that new design unchanged
throughout this book. Fantasy creatures remain rare supporting roles.
CRITICAL VISUAL-CONTINUITY RULE: every recurring character, including supporting characters and the villain, must have a stable physical identity. Give every character a fresh story-specific name for this book; do not reuse names from the reusable pool. Each character_registry entry must include name, role, importance, immutable_identity (face shape/features, skin tone, hair, eye colour, body/build, height impression and permanent marks), story_costume (this book's clothing, accessories and palette), and visual_bible. If a permanent library model is used, include its exact visual_id and preserve its immutable_identity exactly while creating a new story-specific costume. The costume must remain stable throughout this book unless a visible story event changes it. Every scene's characters_present must use exact registry names and include only visible characters.
Use cinematic shot variety, purposeful action, gaze, expressions, lighting and depth.
Avoid copyrighted characters, existing franchises and imitation of living artists.
"""
    plan = None
    if plan_path and plan_path.exists():
        try:
            cached = json.loads(plan_path.read_text(encoding="utf-8"))
            if cached.get("story_schema_version") == 2 and len(cached.get("outline", [])) == 50 and cached.get("character_registry") and cached.get("principal_villain"):
                plan = cached
        except Exception:
            plan = None

    if plan is None:
        outline_prompt = common + f"""
First create the book metadata and a concise outline of exactly 50 sequential scenes.
Each outline entry must have only "number" and "beat"; beat is one or two sentences.
Return ONLY valid JSON, no markdown, with this exact shape:
{{
 "title":"...",
 "subtitle":"...",
 "author":"Your Author Name",
 "description":"A compelling 100-180 word book description",
 "keywords":["seven distinct short search phrases"],
 "categories":["suggested broad category 1","suggested broad category 2"],
 "audience":"Family / all ages",
 "character_registry":[
   {{"name":"Fresh story-specific name","visual_id":null,"role":"story role","importance":"protagonist/supporting/villain/creature","immutable_identity":"physical traits and permanent marks","story_costume":"this book's clothing and accessories","visual_bible":"complete physical identity plus story costume"}}
 ],
 "principal_villain":"Exact name of the single principal villain",
 "character_bible":"Repeat each registry entry's complete visual_bible verbatim under its exact name",
 "art_direction":"Consistent palette, lighting and world atmosphere",
 "outline":[{{"number":1,"beat":"..."}}]
}}
Exactly 50 outline entries. Keep metadata concise.
"""
        # Retry outline generation when the model returns repetitive or malformed beats.
        # Keep the quality gate strict; ask for a genuinely different outline instead of
        # silently accepting duplicated scenes.
        last_outline_error = None
        for outline_attempt in range(1, 4):
            try:
                plan = extract_json(
                    cf_text(outline_prompt, max_tokens=6500),
                    "book metadata and 50-scene outline"
                )
                outline = plan.get("outline", [])
                if not isinstance(outline, list) or len(outline) != 50:
                    raise ValueError(
                        f"Expected 50 outline entries but received {len(outline) if isinstance(outline, list) else 'a non-list value'}."
                    )
                expected_numbers = list(range(1, 51))
                if any(not isinstance(entry, dict) for entry in outline):
                    raise ValueError("Every outline entry must be a JSON object.")
                actual_numbers = [entry.get("number") for entry in outline]
                if actual_numbers != expected_numbers:
                    raise ValueError("Outline scenes must be numbered sequentially from 1 to 50.")
                beats = [
                    re.sub(r"[^a-z0-9 ]", "", str(entry.get("beat", "")).lower()).strip()
                    for entry in outline
                ]
                if any(len(beat.split()) < 8 for beat in beats):
                    raise ValueError("Every outline beat must describe a meaningful story event, not a fragment.")
                if len(set(beats)) < 45:
                    raise ValueError("Outline contains repeated or near-identical beats.")
                last_outline_error = None
                break
            except (ValueError, TypeError, KeyError) as exc:
                last_outline_error = exc
                if outline_attempt == 3:
                    break
                duplicate_rows = []
                if "beats" in locals() and isinstance(locals().get("beats"), list):
                    duplicate_rows = [
                        {"scene": index + 1, "beat": outline[index].get("beat", "")}
                        for index, beat in enumerate(beats)
                        if beats.count(beat) > 1 and index < len(outline)
                    ]
                outline_prompt += f"""

IMPORTANT RETRY {outline_attempt}: The previous outline failed validation because: {exc}
Duplicate or repeated scene entries that MUST be replaced with genuinely different events:
{json.dumps(duplicate_rows, ensure_ascii=False)}
Structure the story as five distinct acts of ten scenes each:
Scenes 1-10: establish protagonists, goal, world, inciting mystery and first danger.
Scenes 11-20: investigation, new allies, conflicting clues and a costly setback.
Scenes 21-30: deepen the mystery, reveal a betrayal or reversal, change the plan.
Scenes 31-40: escalate danger, expose the villain's method and force a major sacrifice.
Scenes 41-50: decisive confrontation, consequences, emotional resolution and a memorable final image.
Do not restate the same clue, chase, discovery, argument or travel event in multiple scenes.
Every scene must have a distinct physical action and a new consequence that changes what happens next.
If a scene number is listed above, replace its beat entirely; do not paraphrase it.
Keep all 50 sequential numbers and return the complete original JSON schema only.
"""
        if last_outline_error is not None:
            raise ValueError(
                f"Outline validation failed after 3 attempts: {last_outline_error}"
            ) from last_outline_error
        plan["story_schema_version"] = 2
        registry = plan.get("character_registry", [])
        if not isinstance(registry, list) or not registry:
            raise ValueError("Story plan is missing its character_registry.")
        names = [str(item.get("name", "")).strip() for item in registry if isinstance(item, dict)]
        known_visual_ids = {model.get("visual_id") for model in load_character_library()}
        for item in registry:
            visual_id = item.get("visual_id")
            if visual_id and visual_id not in known_visual_ids:
                raise ValueError(f"Character {item.get('name')} references unknown visual model: {visual_id}")
            if visual_id and (not item.get("immutable_identity") or not item.get("story_costume")):
                raise ValueError(f"Character {item.get('name')} must separate immutable_identity from story_costume.")
        if len(names) != len(registry) or any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Character registry has missing or duplicate character names.")
        villain_name = str(plan.get("principal_villain", "")).strip()
        villain_entries = [item for item in registry if item.get("importance") == "villain"]
        if not villain_name or villain_name not in names or len(villain_entries) != 1 or villain_entries[0]["name"] != villain_name:
            raise ValueError("Every book must define exactly one registered principal villain.")
        protagonist_count = sum(item.get("importance") == "protagonist" for item in registry)
        supporting_count = sum(item.get("importance") == "supporting" for item in registry)
        if protagonist_count < 2:
            raise ValueError("Story must define a group of at least two main protagonists.")
        if supporting_count < 3:
            raise ValueError("Story must define at least three meaningful supporting characters.")
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
                raise ValueError(f"Scene {number} page text has {word_count} words; expected 35–65. Regenerate this batch.")
            for key in ("shot_type", "camera_angle", "composition", "action_pose", "gaze", "expression", "lighting"):
                scene.setdefault(key, "")
            scene.setdefault("characters_present", [])
            known_names = {item["name"] for item in plan.get("character_registry", [])}
            visible_names = scene.get("characters_present", [])
            if not isinstance(visible_names, list):
                raise ValueError(f"Scene {number} characters_present must be a list.")
            # Normalize known aliases and remove generic crowd labels that are not registry identities.
            character_aliases = {"Kaelin": "Kaelin Darkshadow"}
            generic_labels = {
                "minions", "guards", "soldiers", "villagers", "townspeople",
                "crowd", "crowds", "bandits", "enemies"
            }
            visible_names = [
                character_aliases.get(name, name) if isinstance(name, str) else name
                for name in visible_names
                if not (isinstance(name, str) and name.strip().lower() in generic_labels)
            ]
            scene["characters_present"] = visible_names
            unknown_names = [name for name in visible_names if name not in known_names]
            if unknown_names:
                raise ValueError(f"Scene {number} uses unregistered character names: {unknown_names}")
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

