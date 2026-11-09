# AI Storybook + Amazon KDP Publishing Assistant

A local Windows-first starter app that creates an original 50-scene illustrated storybook, generates scene images through Cloudflare Workers AI, places the story text at the bottom of each page, and exports a PDF plus metadata/checklists for Amazon KDP.

## What it does
- Creates a story and 50 short scene captions using a Cloudflare Workers AI text model (configurable).
- Generates one illustration per scene using a Cloudflare Workers AI image model (configurable).
- Builds a square/portrait/6x9 PDF with illustration above a readable text panel.
- Exports scene text, metadata JSON, a KDP listing draft, and a publishing checklist.
- Includes a KDP browser handoff helper: opens KDP in your browser for you to sign in yourself. This project does **not** store your Amazon password or automate final publication. KDP's interface and requirements can change; review every field and preview in KDP before submitting.

## Important publishing notes
- You need to verify current KDP trim sizes, bleed, cover templates, image requirements, and accepted upload formats in KDP's own help pages before publishing.
- This starter exports an interior PDF. A print cover is a separate full-wrap file whose dimensions depend on trim size, page count, paper type, and bleed; use KDP's cover calculator/template rather than treating the interior PDF as a cover.
- For Kindle eBook, a print PDF is not a suitable universal eBook manuscript. The app exports a separate HTML story manuscript that can be converted/checked in Kindle Create or another KDP-supported workflow.
- Amazon KDP requires disclosure of AI-generated content when applicable. Review current KDP policy and disclose accurately.
- The app does not upload/publish without your explicit review and final action in KDP.

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
CLOUDFLARE_IMAGE_MODEL=@cf/black-forest-labs/flux-1-schnell
```
If a model is unavailable, change it to a model currently listed in your Cloudflare Workers AI dashboard/docs.

## Outputs
Each run creates a timestamped folder inside `output/` with:
- `storybook_interior.pdf` — interior pages, including 50 scene pages.
- `kindle_story.html` — reflowable HTML source for eBook conversion/review.
- `story.json` — structured story and scenes.
- `kdp_listing_draft.json` and `kdp_listing_draft.txt` — metadata draft.
- `scenes/scene_001.png` ... `scene_050.png`.
- `scene_contact_sheet.jpg` — quick visual review.
- `publishing_checklist.txt`.

## Limitations
- Generating 50 images may exceed free Cloudflare Workers AI quota or have model-specific limits. This app stops on errors and preserves completed scenes so you can resume by rerunning with the same output folder if you implement resume support; the initial version restarts a run.
- AI-generated characters can drift visually between scenes. Character descriptions are repeated in prompts to improve consistency, but this is not guaranteed.
- The included KDP handoff is deliberately user-in-the-loop, not a hidden or unattended publishing bot. There is no claim of a supported public KDP publishing API.
