# AI Storybook + Amazon KDP Publishing Assistant

A local Windows-first starter app that creates an original 50-scene illustrated storybook, generates scene images through Cloudflare Workers AI, places the story text at the bottom of each page, and exports a PDF plus metadata/checklists for Amazon KDP.

## What it does
- Creates a 50-scene story with a concise, plot-driven cast and a consequential antagonist where required by the selected story format.
- Builds a canonical character registry for every selected character, including supporting characters and the villain.
- Generates dedicated identity portraits for recurring characters and reuses relevant portraits as scene-specific image references.
- Repeats each visible character's exact visual bible in scene prompts and validates scene character names against the registry.
- Generates one illustration per scene using a Cloudflare Workers AI image model (configurable).
- Builds a square/portrait/6x9 PDF with illustration above a readable text panel.
- Runs a strict whole-book editorial review with a 7/10 minimum, plus deterministic checks for 50 sequential scenes, 40–55 words per page, exact registered character names, and one registered principal villain.
- Validates all 50 scene image files for readability, requested resolution, blank outputs, and near-duplicate images before page composition.
- Exports scene text, metadata JSON, a KDP listing draft, quality reports, and a publishing checklist.
- Includes a KDP browser handoff helper: opens KDP in your browser for you to sign in yourself. This project does **not** store your Amazon password or automate final publication. KDP's interface and requirements can change; review every field and preview in KDP before submitting.

## Important publishing notes
- You need to verify current KDP trim sizes, bleed, cover templates, image requirements, and accepted upload formats in KDP's own help pages before publishing.
- This starter exports an interior PDF. A print cover is a separate full-wrap file whose dimensions depend on trim size, page count, paper type, and bleed; use KDP's cover calculator/template rather than treating the interior PDF as a cover.
- For Kindle eBook, a print PDF is not a suitable universal eBook manuscript. The agent exports a separate fixed-layout EPUB 3 picture-book file plus its HTML source. The EPUB receives ZIP, XML, page-count, and image-count integrity checks; still preview it in Kindle Previewer and confirm current KDP device compatibility before upload.
- Amazon KDP requires disclosure of AI-generated content when applicable. Review current KDP policy and disclose accurately.
- The app does not upload/publish without your explicit review and final action in KDP.

## Permanent character identity library

- Permanent visual models live under `character_library/models/<visual_id>/` with an `identity.json` record and an optional local `reference.png`.
- The immutable identity record describes face, facial proportions, skin tone, hair, eyes, body proportions and permanent marks. Story names, personalities, occupations, relationships, roles, costumes and accessories are created anew for each book.
- The AI selects only the reusable visual models that genuinely fit the plot (including none when appropriate); the library is not a fixed ensemble and characters are never forced into a story. New names, personalities, relationships, occupations, roles and costumes are invented for each book, while selected characters retain their physical identity.
- When a saved reference image is available locally, the illustration pipeline uses it as a physical-identity reference and explicitly tells the image model to follow the current book's costume instead of copying the reference outfit.
- Story-specific cast names are recorded under `character_library/story_cast/` so future books can avoid reusing previous names.
- Image references remain on the local computer unless the owner intentionally adds them to a private or public repository. Never commit `.env` credentials.

## Illustration quality and resolution

- The daily runner now selects a high-resolution scene image size (default maximum side: 1536 px) that matches the printable art-panel aspect ratio for the selected trim size. This is sharper than the earlier 1024 px default and avoids unnecessary cropping.
- Change `STORY_IMAGE_MAX_SIDE` in the local `.env` only if needed; it must be a multiple of 16 from 1024 to 1920. Larger images use more inference resources and may affect quota or cost.
- Scene prompts explicitly request crisp focus, defined facial features, hair strands, fabric/material textures, clean edges, and reduced blur.
- Each scene reference portrait is identified by character name in the prompt, rather than treating several portraits as one generic identity sheet.
- FLUX.2 Klein 4B is a speed-oriented distilled model with a fixed four-step process on Workers AI; increasing output dimensions cannot guarantee perfect detail. The project does not pretend to guarantee zero blur. Inspect the contact sheet and regenerate weak scenes before publishing.
- A 512 × 512 test image is only a low-resolution connectivity test; it is not representative of the daily runner's final scene dimensions.

## Scheduling status — intentionally paused

Automatic daily generation is disabled while the storybook agent is being finalized. The GitHub Actions generation workflow is manual-dispatch only; it has no cron schedule. The Windows scheduler installer also requires an explicit `-EnableScheduling` switch and will otherwise make no changes.

If an older Windows scheduled task is already installed on your PC, run `disable_scheduler.ps1` once in PowerShell to remove it. This repository change cannot remove a task from your computer remotely.

After the agent passes the complete quality and output validation process, scheduling can be enabled deliberately with:

```powershell
.\\install_scheduler.ps1 -EnableScheduling
```

The daily runner stores progress in `state/scheduler_state.json` and writes books under `output/YYYYMMDD_daily_story/`. Do not enable scheduling until the agent has been tested end to end and the owner approves restarting automation.

## KDP workflow boundary

The runner automatically prepares the interior PDF, Kindle HTML source, listing drafts, and checklist. It does not sign into Amazon, bypass account verification, or press the final publish button. KDP has no general public self-service publishing API for this project to rely on, so actual account submission and publication remain subject to KDP's supported interface and account controls. The EPUB receives structural integrity validation, but must still be previewed in Kindle Previewer. The print interior is not a full-wrap cover.

## Requirements
- Windows 10/11, Python 3.11 or newer.
- Cloudflare account with Workers AI enabled and an account ID + API token.
- Internet connection.
- For the text-generation model, select a model available in your Cloudflare account. Model availability/pricing can change.

## Quick start (Windows)
1. Install Python 3.11+ from https://www.python.org/downloads/windows/ and tick **Add Python to PATH**.
2. Extract this ZIP to a folder.
3. Open Command Prompt in that folder and run:
   ```bat
   setup.bat
   ```
4. Open `.env` in Notepad and enter your Cloudflare account ID and API token. Do not share this file.
5. Run:
   ```bat
   run.bat
   ```
6. In the browser app, enter story settings and click **Generate story + scenes**.
7. Download the PDF and listing files from the app's output folder.
8. Open KDP using the app's **Open KDP** button, sign in yourself, enter/review details, upload the appropriate files, preview, and publish only after your own approval.

## Cloudflare credentials
Create a token with the minimum permissions needed for Workers AI usage. Keep credentials in `.env`, never commit it or share screenshots that reveal it. The app sends requests directly to Cloudflare from your PC.

`.env` example:
```env
CLOUDFLARE_ACCOUNT_ID=replace_me
CLOUDFLARE_API_TOKEN=replace_me
CLOUDFLARE_TEXT_MODEL=@cf/meta/llama-3.1-8b-instruct
CLOUDFLARE_IMAGE_MODEL=@cf/black-forest-labs/flux-2-klein-4b
```
If a model is unavailable, change it to a model currently listed in your Cloudflare Workers AI dashboard/docs.

## Outputs
Each run creates a timestamped folder inside `output/` with:
- `storybook_interior.pdf` — interior pages, including 50 scene pages.
- `kindle_story.html` — HTML source for the illustrated eBook.
- `kindle_story.epub` — fixed-layout EPUB 3 picture-book package with 50 integrated illustrated pages.
- `story.json` — structured story and scenes.
- `kdp_listing_draft.json` and `kdp_listing_draft.txt` — metadata draft.
- `scenes/scene_001.png` ... `scene_050.png`.
- `scene_contact_sheet.jpg` — quick visual review.
- `quality_review.json` — editorial review scores and issues.
- `image_quality_report.json` — technical image integrity report.
- `publishing_checklist.txt`.

## Limitations
- Generating 50 images plus visual-review calls may exceed free Cloudflare Workers AI quota or have model-specific limits. The runner preserves completed scenes and resumes an incomplete book from its saved output/artifact when rerun; quota errors stop the run without marking the book complete.
- Character continuity is treated as a required quality benchmark: recurring protagonists, supporting characters, creatures and the principal villain receive canonical visual bibles; recurring characters also receive identity portraits reused in scenes where they appear. The villain's costume and styling are designed for each new book, then held consistent within that book.
- Image models can still produce occasional visual drift. The reference portraits and prompt checks reduce this risk but cannot guarantee pixel-perfect identity; review the contact sheet and regenerate any inconsistent scene before publishing.
- The included KDP handoff is deliberately user-in-the-loop, not a hidden or unattended publishing bot. There is no claim of a supported public KDP publishing API.


## Automated visual quality gate

Before page layout and package verification, the runner now reviews every generated scene
with Cloudflare Workers AI Moondream 3.1. It compares the illustration against the written
scene direction and, where applicable, side-by-side character identity reference portraits.
The review checks scene match, visible cast, identity resemblance, costume continuity,
obvious anatomy defects, blur, and major composition problems.

A scene scoring below 7/10, failing the scene-match check, or containing a major visual
defect is regenerated up to two times and reviewed again. If it still fails, the run stops
and the package cannot receive the publication verification marker. The workflow also
requires a passing report for all 50 scenes before it marks the Pakistan-date book complete.

The report is saved as `visual_quality_report.json`. The existing
`image_quality_report.json` combines technical image checks with the visual-review
summary. This is AI-assisted quality control, not a guarantee of perfect human-level
art direction; a final author approval is still required before KDP publication.

The visual reviewer uses the Cloudflare Workers AI model
`@cf/moondream/moondream3.1-9B-A2B` by default. It shares the configured Cloudflare
Workers AI account and token with image generation. Vision calls consume Workers AI
usage separately from image generation, and availability/allowances depend on the
Cloudflare account. If the vision model is unavailable or its response cannot be
validated, the run fails closed rather than silently skipping visual review.
