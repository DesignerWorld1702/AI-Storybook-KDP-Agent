import os, json, re, time, base64, io, textwrap
from datetime import datetime
from pathlib import Path
import requests
import streamlit as st
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
IMAGE_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell").strip()

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
    return text.strip()

def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The text model did not return valid JSON. Try again or use a different text model.")
    return json.loads(text[start:end+1])

STYLE_LOCK = (
    "Unified 2D cinematic anime realism: hand-painted feature-film illustration, "
    "semi-realistic adult anatomy and expressive faces, natural proportions, "
    "consistent fine linework, painterly cel shading, detailed believable materials "
    "and environments, controlled cinematic colour grading. Never 3D CGI, chibi, "
    "childish cartoon, photorealism, or a mixture of unrelated art styles."
)

def make_story(genre, style, audience, title_hint, character_notes, scene_words):
    prompt = f"""
Create an original illustrated feature-film story with exactly 50 sequential scenes.
Story direction: mystery, suspense, thriller, adventure and restrained fantasy.
Genre emphasis: {genre}
Audience: family / all ages.
Visual style (LOCKED): {STYLE_LOCK}
Title hint: {title_hint or "invent a memorable title"}.
Character notes / reusable character pool: {character_notes or "Create 2-3 original young-adult human protagonists with distinctive, repeatable visual traits."}

CAST RULES:
- The main protagonists must be humans in their 20s, not children or teenagers.
- Humans drive the plot and remain the central focus.
- Use only the characters required by this particular story. Do not force every character from a reusable pool into every book.
- Fantasy creatures are rare supporting characters, not the main cast; include one only when the plot genuinely needs it.
- Describe each selected character with stable face, hair, eye, body, clothing and distinguishing details.
- Stories may be standalone and may introduce new characters.

CINEMATIC DIRECTION:
Every scene must have intentional film direction and visual variety. Vary wide establishing shots, medium shots, close-ups, extreme close-ups, low/high angles, overhead views, side profiles, over-the-shoulder views, rear views and dynamic action compositions. Characters must not simply face forward and talk in every scene. Specify physical action, pose, gaze direction, expression, camera placement, foreground/background depth, lighting and atmosphere. Keep rendering style and character identity consistent while varying camera composition.

Each scene has page text of about {scene_words} words, suitable for an illustrated book.
Avoid copyrighted characters, existing franchises, and imitation of living artists.
Return ONLY valid JSON, no markdown fences, with this exact shape:
{{
 "title":"...",
 "subtitle":"...",
 "author":"Your Author Name",
 "description":"A compelling 100-180 word book description",
 "keywords":["seven distinct short search phrases"],
 "categories":["suggested broad category 1","suggested broad category 2"],
 "audience":"Family / all ages",
 "character_bible":"Detailed visual bibles for only the characters selected for this story",
 "art_direction":"The locked style plus this book's consistent palette, lighting and world atmosphere",
 "scenes":[
   {{
    "number":1,
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
Requirements: exactly 50 scenes; no extra keys; coherent beginning-middle-end; scene text must not contain page numbers; no text, lettering, logos or watermarks in images. The author field should be "Your Author Name" unless supplied by the user.
"""
    data = extract_json(cf_text(prompt, max_tokens=11000))
    scenes = data.get("scenes", [])
    if len(scenes) != 50:
        raise ValueError(f"Expected 50 scenes but received {len(scenes)}.")
    for i, scene in enumerate(scenes, 1):
        scene["number"] = i
        if not scene.get("text") or not scene.get("visual_prompt"):
            raise ValueError(f"Scene {i} is missing text or a visual prompt.")
        for key in ("shot_type", "camera_angle", "composition", "action_pose", "gaze", "expression", "lighting"):
            scene.setdefault(key, "")
        scene.setdefault("characters_present", [])
    data["art_direction"] = f"{STYLE_LOCK}\n{data.get('art_direction', '')}"
    return data


def cf_image(prompt, dest, width=1024, height=1024, reference_images=None):
    if not CF_ACCOUNT or not CF_TOKEN:
        raise RuntimeError("Cloudflare credentials missing. Edit the .env file first.")

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{IMAGE_MODEL}"
    full_prompt = (
        prompt
        + "\nUse reference image 0 for the exact illustration rendering style and colour treatment. "
        + "Use reference image 1 only as the identity guide for the named characters; "
        + "include only characters explicitly present in this scene. "
        + "Preserve stable faces, hair, eyes, proportions and clothing. "
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
    reference_images = reference_images or []
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


def build_scene_prompt(story, scene, style):
    cast = ", ".join(scene.get("characters_present", [])) or "only the characters required by this scene"
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
        f"Character bible: {story.get('character_bible', '')}. "
        f"Characters visible in this scene: {cast}. "
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
        c.setFont("Helvetica", text_size)
        text_lines = wrap_text(scene["text"], "Helvetica", text_size, max_width, c)
        available_lines = max(1, int((y - 0.18*inch) / (text_size*1.35)))
        if len(text_lines) > available_lines:
            # Fit the text panel conservatively without silently shrinking below 8pt.
            while len(text_lines) > available_lines and text_size > 8:
                text_size -= 0.5
                c.setFont("Helvetica", text_size)
                text_lines = wrap_text(scene["text"], "Helvetica", text_size, max_width, c)
                available_lines = max(1, int((y - 0.18*inch) / (text_size*1.35)))
        for line in text_lines[:available_lines]:
            c.drawString(margin, y, line); y -= text_size*1.35
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

st.set_page_config(page_title="AI Storybook + KDP Assistant", page_icon="📚", layout="wide")
st.title("📚 AI Storybook + Amazon KDP Assistant")
st.caption("Generate a 50-scene illustrated storybook and prepare print/eBook publishing files. Final KDP review and publication stay under your control.")

with st.sidebar:
    st.header("Connection")
    if CF_ACCOUNT and CF_TOKEN:
        st.success("Cloudflare credentials found in .env")
    else:
        st.warning("Cloudflare credentials missing")
    st.write("Text model:", TEXT_MODEL)
    st.write("Image model:", IMAGE_MODEL)
    st.caption("Change models in .env if these aren't available for your account.")

with st.form("story_form"):
    c1,c2 = st.columns(2)
    with c1:
        genre = st.selectbox("Story genre", ["Fantasy adventure","Children's bedtime story","Mystery and suspense","Romance and emotional story","Science fiction","Fable / animal story"])
        style = STYLE_LOCK
        st.caption("Locked illustration style: unified 2D cinematic anime realism across every page.")
        audience = "Family / all ages"
        st.caption("Audience: all ages. Main human protagonists are in their 20s.")
        trim = st.selectbox("Paperback trim size", list(TRIM_SIZES.keys()), index=0)
    with c2:
        title_hint = st.text_input("Title idea (optional)", placeholder="e.g., The Lantern in the Moonwood")
        author = st.text_input("Author name to print", value="Your Author Name")
        character_notes = st.text_area("Character / visual continuity notes (optional)", placeholder="Main character's age, hair, clothes, colors, companions...")
        scene_words = st.slider("Words per scene", min_value=20, max_value=90, value=45, step=5)
        text_size = st.slider("PDF story text size", min_value=8, max_value=16, value=11)
    resume_existing = st.checkbox("Resume the latest incomplete story generation", value=False)
    submitted = st.form_submit_button("✨ Generate story + 50 scenes", type="primary", use_container_width=True)

if submitted:
    if not CF_ACCOUNT or not CF_TOKEN:
        st.error("Add CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to .env, then restart the app.")
    else:
        try:
            if resume_existing:
                candidates = sorted(
                    [p for p in OUTROOT.iterdir() if p.is_dir() and (p / "story.json").exists()],
                    key=lambda p: p.name,
                )
                if not candidates:
                    raise RuntimeError("No saved story was found to resume. Start a new story first.")
                outdir = candidates[-1]
                story = json.loads((outdir / "story.json").read_text(encoding="utf-8"))
                st.info(f"Resuming saved story: {outdir.name}")
            else:
                run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                outdir = OUTROOT / run_id
                outdir.mkdir(parents=True, exist_ok=True)
                story = make_story(genre, style, audience, title_hint, character_notes, scene_words)
                story["author"] = author.strip() or "Your Author Name"
                story["genre"] = genre
                story["illustration_style"] = style
                story["trim_size"] = trim
                (outdir / "story.json").write_text(
                    json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8"
                )

            scenes_dir = outdir / "scenes"
            scenes_dir.mkdir(parents=True, exist_ok=True)
            st.session_state["last_outdir"] = str(outdir)

            with st.status("Preparing consistent visual references...", expanded=True) as status:
                references = create_reference_images(story, outdir)
                status.update(label="Reference images ready. Generating missing scenes...", state="running")
                progress = st.progress(0.0)
                note = st.empty()
                completed = 0
                for index, scene in enumerate(story["scenes"], 1):
                    path = scenes_dir / f"scene_{index:03d}.png"
                    if path.exists() and path.stat().st_size > 0:
                        completed += 1
                        progress.progress(completed / 50)
                        continue
                    note.write(f"Generating scene {index}/50: {scene.get('heading', '')}")
                    prompt = build_scene_prompt(story, scene, style)
                    cf_image(prompt, path, reference_images=references)
                    completed += 1
                    progress.progress(completed / 50)
                    time.sleep(0.15)

                status.update(label="All 50 illustrations ready. Building publishing files...", state="running")
                build_pdf(story, scenes_dir, outdir / "storybook_interior.pdf", trim, False, text_size)
                make_kindle_html(story, scenes_dir, outdir / "kindle_story.html")
                make_listing(story, outdir)
                write_checklist(outdir)
                create_contact_sheet(scenes_dir, outdir / "scene_contact_sheet.jpg")
                status.update(label="Storybook files created!", state="complete")

            st.success(f"Finished! Files are in: {outdir}")
            st.session_state["story"] = story
            st.session_state["last_outdir"] = str(outdir)
        except Exception as exc:
            st.error(f"Generation stopped: {exc}")
            st.info(
                "Completed scene images and reference sheets remain saved. "
                "Choose 'Resume the latest incomplete story generation' and submit again "
                "to continue without regenerating existing scene images."
            )

if "last_outdir" in st.session_state:
    outdir = Path(st.session_state["last_outdir"])
    if outdir.exists():
        st.subheader("Latest output")
        st.code(str(outdir))
        for name, label, mime in [
            ("storybook_interior.pdf","Download print interior PDF","application/pdf"),
            ("kindle_story.html","Download Kindle HTML source","text/html"),
            ("story.json","Download story JSON","application/json"),
            ("kdp_listing_draft.txt","Download KDP listing draft","text/plain"),
            ("kdp_listing_draft.json","Download KDP listing JSON","application/json"),
            ("publishing_checklist.txt","Download publishing checklist","text/plain"),
            ("scene_contact_sheet.jpg","Download 50-scene contact sheet","image/jpeg"),
        ]:
            p = outdir/name
            if p.exists():
                st.download_button(label, data=p.read_bytes(), file_name=name, mime=mime, key=f"dl_{name}")
        st.markdown("---")
        st.subheader("Amazon KDP handoff")
        st.write("Open KDP in your browser, sign in yourself, then use the generated listing draft and checklist. This app does not collect your password or click the final publish button.")
        st.link_button("Open Amazon KDP", "https://kdp.amazon.com/")
        st.warning("Before uploading: use KDP's current cover calculator/template for a separate full-wrap cover. The PDF here is the interior only. Kindle eBook requires its own conversion and preview check.")
