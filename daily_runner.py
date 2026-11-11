"""Daily, resumable storybook generation runner for Windows Task Scheduler."""
import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import storybook_engine as engine

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TZ = ZoneInfo("Asia/Karachi")
OUTROOT = ROOT / "output"
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG_DIR / "daily_runner.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)])
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "scheduler_state.json"
CONFIG_FILE = ROOT / "config" / "automation.json"
DEFAULT_CONFIG = {
    "schedule_time": "20:00",
    "timezone": "Asia/Karachi",
    "missed_run_policy": "catchup",
    "author": "Fahad Asif",
    "genre_cycle": ["Fantasy adventure", "Mystery and suspense", "Fantasy adventure", "Science fiction"],
    "trim_size": "8.5 × 8.5 in — square picture book",
    "scene_words": 45,
    "pdf_text_size": 11,
    "catchup_pause_seconds": 120,
    "max_books_per_run": 1,
    "first_run_date": date.today().isoformat()
}

def load_config():
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged

def load_state(config):
    if not STATE_FILE.exists():
        state = {"last_completed_date": (date.fromisoformat(config["first_run_date"]) - timedelta(days=1)).isoformat()}
        save_state(state)
        return state
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))

def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def valid_image(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return path.stat().st_size > 1024
    except Exception:
        return False

def build_one_book(book_date, config):
    run_id = book_date.strftime("%Y%m%d") + "_daily_story"
    outdir = OUTROOT / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    status_path = outdir / "job_status.json"
    status = {"scheduled_date": book_date.isoformat(), "status": "running", "updated_at": datetime.now(TZ).isoformat()}
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    try:
        story_path = outdir / "story.json"
        if story_path.exists():
            story = json.loads(story_path.read_text(encoding="utf-8"))
        else:
            genres = config["genre_cycle"]
            genre = genres[(book_date - date.fromisoformat(config["first_run_date"])).days % len(genres)]
            story = engine.make_story(
                genre, engine.STYLE_LOCK, "Family / all ages",
                "", engine.DEFAULT_CHARACTER_POOL, int(config["scene_words"]), workdir=outdir
            )
            story["author"] = config["author"]
            story["genre"] = genre
            story["illustration_style"] = engine.STYLE_LOCK
            story["trim_size"] = config["trim_size"]
            story["pdf_text_size"] = int(config["pdf_text_size"])
            story["scheduled_date"] = book_date.isoformat()
            story_path.write_text(json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")

        if story.get("story_schema_version") != 2:
            raise RuntimeError(
                "This saved story uses an older story schema and has been left untouched. "
                "Create a fresh book date with the updated generator; existing story files and images are preserved."
            )
        if not story.get("character_registry") or not story.get("principal_villain"):
            raise RuntimeError(
                "This saved story is missing its character-continuity registry. "
                "The existing files were preserved."
            )
        references = engine.create_reference_images(story, outdir)
        scene_width, scene_height = engine.scene_image_dimensions(story.get("trim_size", config["trim_size"]))
        logging.info("Scene illustration resolution: %s x %s", scene_width, scene_height)
        scenes_dir = outdir / "scenes"
        scenes_dir.mkdir(exist_ok=True)
        for index, scene in enumerate(story["scenes"], 1):
            image_path = scenes_dir / f"scene_{index:03d}.png"
            if valid_image(image_path):
                continue
            if image_path.exists():
                image_path.unlink()
            scene_refs = engine.scene_reference_images(
                story, scene, outdir, references[0]
            )
            engine.cf_image(
                engine.build_scene_prompt(story, scene, engine.STYLE_LOCK),
                image_path, width=scene_width, height=scene_height,
                reference_images=scene_refs
            )
            if not valid_image(image_path):
                raise RuntimeError(f"Scene {index} image did not pass file validation.")
            time.sleep(0.2)

        engine.build_pdf(story, scenes_dir, outdir / "storybook_interior.pdf",
                         story["trim_size"], False, story["pdf_text_size"])
        engine.make_kindle_html(story, scenes_dir, outdir / "kindle_story.html")
        engine.make_listing(story, outdir)
        engine.write_checklist(outdir)
        engine.create_contact_sheet(scenes_dir, outdir / "scene_contact_sheet.jpg")
        status.update({"status": "completed", "updated_at": datetime.now(TZ).isoformat(),
                       "completed_scenes": 50})
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        return True
    except Exception as exc:
        status.update({"status": "failed", "updated_at": datetime.now(TZ).isoformat(),
                       "error": str(exc), "traceback": traceback.format_exc()[-8000:]})
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        logging.exception("Book generation failed for %s: %s", book_date, exc)
        return False

def run_due_jobs(force=False):
    config = load_config()
    state = load_state(config)
    now = datetime.now(TZ)
    hour, minute = map(int, config["schedule_time"].split(":"))
    today_due = now.hour > hour or (now.hour == hour and now.minute >= minute)
    last_done = date.fromisoformat(state["last_completed_date"])
    today = now.date()
    due_through = today if (force or today_due) else today - timedelta(days=1)
    next_date = last_done + timedelta(days=1)
    if next_date < date.fromisoformat(config["first_run_date"]):
        next_date = date.fromisoformat(config["first_run_date"])
    if next_date > due_through:
        logging.info("No books due. Next scheduled run: %s %s.", config["schedule_time"], config["timezone"])
        return 0
    completed = 0
    while next_date <= due_through:
        logging.info("Starting scheduled book for %s", next_date)
        if not build_one_book(next_date, config):
            logging.warning("Stopping catch-up queue. This date will be retried on the next task run.")
            return 1
        state["last_completed_date"] = next_date.isoformat()
        save_state(state)
        completed += 1
        next_date += timedelta(days=1)
        if next_date <= due_through and completed >= max(1, int(config.get("max_books_per_run", 1))):
            logging.info("More missed dates remain in the catch-up queue; the next task trigger will process the next one.")
            break
        if next_date <= due_through:
            time.sleep(int(config["catchup_pause_seconds"]))
    logging.info("Completed %s scheduled book(s).", completed)
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-now", action="store_true", help="Generate the next book immediately.")
    args = parser.parse_args()
    sys.exit(run_due_jobs(force=args.run_now))
