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

CF_ACCOUNT = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CF_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
TEXT_MODEL = os.getenv("CLOUDFLARE_TEXT_MODEL", "@cf/meta/llama-3.1-8b-instruct").strip()
IMAGE_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b").strip()

TRIM_SIZES = {
    "8.5 × 8.5 in — square picture book": (8.5, 8.5),
    "8.5 × 11 in — portrait picture book": (8.5, 11.0),
    "6 × 9 in — standard paperback": (6.0, 9.0),
}

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
Keep each selected character's defining face, hair, eye colour, marks, proportions and signature clothing stable throughout the book. Names and roles may be changed for a standalone story if the user asks, but do not silently alter the canonical appearance when using this pool.
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
Humans drive the plot. Build a lively ensemble: several clear main protagonists plus
supporting characters who join, leave, form alliances, disagree, and reveal secrets
naturally as the adventure unfolds. Do not force every pool character into the story.
Include exactly ONE principal villain with a consequential role, distinctive personality,
clear motive, methods, and a meaningful connection to the central conflict. The villain
may be a reusable identity from a villain pool, but create a fresh story-specific costume,
equipment, styling and visual presentation for this book. Keep that new design unchanged
throughout this book. Fantasy creatures remain rare supporting roles.
CRITICAL VISUAL-CONTINUITY RULE: every recurring character, including minor/supporting
characters and the villain, must have a stable visual identity. Define a character_registry
with one entry per selected character: name, role, importance ("protagonist",
"supporting", "villain", or "creature"), and a highly specific visual_bible covering
adult age, face shape/features, skin tone, hair, eye colour, body/build, height impression,
identifying marks, exact clothing, accessories and colour palette. Do not vary these
features between scenes unless an explicit story event changes them. Every scene's
characters_present must use exact registry names and include only visible characters.
Use cinematic shot variety, purposeful action, gaze, expressions, lighting and depth.
Avoid copyrighted characters, existing franchises and imitation of living artists.
"""
    plan = None
    if plan_path and plan_path.exists():
        try:
            cached = json.loads(plan_path.read_text(encoding="utf-8"))
            if len(cached.get("outline", [])) == 50 and cached.get("character_registry") and cached.get("principal_villain"):
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
   {{"name":"Exact character name","role":"story role","importance":"protagonist/supporting/villain/creature","visual_bible":"Complete fixed visual identity; for the villain include this book's unique costume and equipment design"}}
 ],
 "principal_villain":"Exact name of the single principal villain",
 "character_bible":"Repeat each registry entry's complete visual_bible verbatim under its exact name",
 "art_direction":"Consistent palette, lighting and world atmosphere",
 "outline":[{{"number":1,"beat":"..."}}]
}}
Exactly 50 outline entries. Keep metadata concise.
"""
        plan = extract_json(cf_text(outline_prompt, max_tokens=5000), "book metadata and 50-scene outline")
        if len(plan.get("outline", [])) != 50:
            raise ValueError(f"Expected 50 outline entries but received {len(plan.get('outline', []))}.")
        registry = plan.get("character_registry", [])
        if not isinstance(registry, list) or not registry:
            raise ValueError("Story plan is missing its character_registry.")
        names = [str(item.get("name", "")).strip() for item in registry if isinstance(item, dict)]
        if len(names) != len(registry) or any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Character registry has missing or duplicate character names.")
        villain_name = str(plan.get("principal_villain", "")).strip()
        villain_entries = [item for item in registry if item.get("importance") == "villain"]
        if not villain_name or villain_name not in names or len(villain_entries) != 1 or villain_entries[0]["name"] != villain_name:
            raise ValueError("Every book must define exactly one registered principal villain.")
        if not any(item.get("importance") == "protagonist" for item in registry):
            raise ValueError("Story must define at least one main protagonist.")
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
                if len(cached_batch.get("scenes", [])) == last - first + 1:
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
Each scene page text should be about {scene_words} words. Make visual prompts cinematic,
specific and varied. Do not repeat the same camera angle or static talking pose.
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
            if batch_path:
                batch_path.write_text(
                    json.dumps({"scenes": batch_scenes}, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )

        for offset, scene in enumerate(batch_scenes):
            number = first + offset
            scene["number"] = number
            if not scene.get("text") or not scene.get("visual_prompt"):
                raise ValueError(f"Scene {number} is missing text or a visual prompt.")
            for key in ("shot_type", "camera_angle", "composition", "action_pose", "gaze", "expression", "lighting"):
                scene.setdefault(key, "")
            scene.setdefault("characters_present", [])
            known_names = {item["name"] for item in plan.get("character_registry", [])}
            visible_names = scene.get("characters_present", [])
            if not isinstance(visible_names, list):
                raise ValueError(f"Scene {number} characters_present must be a list.")
            unknown_names = [name for name in visible_names if name not in known_names]
            if unknown_names:
                raise ValueError(f"Scene {number} uses unregistered character names: {unknown_names}")
            scenes.append(scene)

    plan.pop("outline", None)
    plan["scenes"] = scenes
    plan["art_direction"] = f"{STYLE_LOCK}\n{plan.get('art_direction', '')}"
    return plan

def cf_image(prompt, dest, width=1024, height=1024, reference_images=None):
    if not CF_ACCOUNT or not CF_TOKEN:
        raise RuntimeError("Cloudflare credentials missing. Edit the .env file first.")

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{IMAGE_MODEL}"
    reference_images = reference_images or []
    if len(reference_images) >= 2:
        reference_guidance = (
            "Use reference image 0 for the exact illustration rendering style and colour treatment. "
            "Use reference image 1 only as the identity guide for the named characters; "
            "include only characters explicitly present in this scene. "
            "Preserve stable faces, hair, eyes, proportions and clothing. "
        )
    elif len(reference_images) == 1:
        reference_guidance = (
            "Use reference image 0 as the exact illustration style and colour reference. "
            "Follow the character descriptions in the prompt; do not invent a character identity from the style reference. "
        )
    else:
        reference_guidance = (
            "No reference images are supplied for this generation. Follow the visual style and character descriptions in the prompt exactly. "
        )

    full_prompt = (
        prompt
        + "\n" + reference_guidance
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


def scene_reference_images(story, scene, outdir, style_ref):
    """Return style reference plus up to three scene-specific identity portraits."""
    registry = story.get("character_registry", [])
    by_name = {item.get("name"): item for item in registry}
    refs = [style_ref]
    for name in scene.get("characters_present", []):
        item = by_name.get(name)
        if not item:
            continue
        path = character_reference_path(outdir, name)
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
        f"{name}: {registry[name].get('visual_bible', '')}"
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
        "Match each recurring character's face, hair, eye colour, skin tone, proportions, "
        "identifying marks, clothing and accessories exactly. Do not merge identities or "
        "substitute one character for another. Any costume change must be explicitly stated "
        "in the scene's story context. "
        f"{direction} Scene {scene.get('number')}: {scene.get('visual_prompt', '')}. "
        f"Style lock: {STYLE_LOCK}. "
        "Create one cinematic movie frame with clear foreground/background depth, "
        "natural anatomy, purposeful character blocking and a visually distinct camera composition. "
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

    # Generate one dedicated identity portrait per selected recurring character.
    # These are reused in every scene where that character appears.
    registry = story.get("character_registry", [])
    for item in registry:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        portrait_path = character_reference_path(outdir, name)
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

