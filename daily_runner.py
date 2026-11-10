"""Daily, resumable storybook generation runner for Windows Task Scheduler."""
import argparse
import json
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

        references = engine.create_reference_images(story, outdir)
        scenes_dir = outdir / "scenes"
        scenes_dir.mkdir(exist_ok=True)
        for index, scene in enumerate(story["scenes"], 1):
            image_path = scenes_dir / f"scene_{index:03d}.png"
            if valid_image(image_path):
                continue
            if image_path.exists():
                image_path.unlink()
            engine.cf_image(
                engine.build_scene_prompt(story, scene, engine.STYLE_LOCK),
                image_path, reference_images=references
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
        print(f"[FAILED] {book_date}: {exc}", flush=True)
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
        print(f"No books due. Next scheduled run: {config['schedule_time']} {config['timezone']}.", flush=True)
        return 0
    completed = 0
    while next_date <= due_through:
        print(f"Starting scheduled book for {next_date}...", flush=True)
        if not build_one_book(next_date, config):
            print("Stopping catch-up queue. This date will be retried on the next task run.", flush=True)
            return 1
        state["last_completed_date"] = next_date.isoformat()
        save_state(state)
        completed += 1
        next_date += timedelta(days=1)
        if next_date <= due_through:
            time.sleep(int(config["catchup_pause_seconds"]))
    print(f"Completed {completed} scheduled book(s).", flush=True)
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-now", action="store_true", help="Generate the next book immediately.")
    args = parser.parse_args()
    sys.exit(run_due_jobs(force=args.run_now))
