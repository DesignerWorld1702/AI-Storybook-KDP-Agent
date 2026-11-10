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

## Daily automatic generation (Windows)

The Streamlit app remains an optional manual dashboard. The independent `daily_runner.py` creates books without opening a browser.

1. Keep your Cloudflare credentials in the local `.env` file. Never upload it to GitHub.
2. Install the project dependencies with `python -m pip install -r requirements.txt`.
3. Run `python daily_runner.py --run-now` once to test the unattended pipeline immediately.
4. After the test succeeds, right-click `install_scheduler.ps1` and run it with PowerShell (or run it from an ordinary PowerShell window). It registers a Windows Task Scheduler task for 8:00 PM and Windows logon. If the PC was asleep, the task is configured to wake it when supported.
5. The runner stores progress in `state/scheduler_state.json`, writes each book under `output/YYYYMMDD_daily_story/`, and resumes from valid scene images after interruptions.
6. If the PC was off for scheduled days, the startup trigger processes overdue dates sequentially, one book per task trigger by default to limit quota spikes. A failed date stays at the head of the queue and is retried on the next task run. Change `max_books_per_run` in the automation config if you intentionally want a larger catch-up batch.

Configuration is created on first run at `config/automation.json`. You can edit the author, genre rotation, trim size, schedule, and catch-up pause there. The time zone is Asia/Karachi.

**Important:** The first automatic test should be run manually with `--run-now` before installing the schedule. A 50-image book may exceed a free Cloudflare quota or incur usage charges. Check the account's limits before leaving daily generation unattended.

## KDP workflow boundary

The runner automatically prepares the interior PDF, Kindle HTML source, listing drafts, and checklist. It does not sign into Amazon, bypass account verification, or press the final publish button. KDP has no general public self-service publishing API for this project to rely on, so actual account submission and publication remain subject to KDP's supported interface and account controls. The HTML output is a source manuscript, not a validated Kindle EPUB. The print interior is not a full-wrap cover.

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
