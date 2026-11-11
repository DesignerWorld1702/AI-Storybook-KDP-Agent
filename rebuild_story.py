import json
import shutil
from pathlib import Path
from datetime import datetime

ROOT = Path("output/20261110_daily_story")
PLAN = ROOT / "story_plan.json"
STORY = ROOT / "story.json"

if not PLAN.exists() or not STORY.exists():
    raise FileNotFoundError("Existing story plan or story.json is missing.")

# Preserve backups before changing anything.
for path in (PLAN, STORY):
    backup = path.with_name(path.name + ".pre_rebuild_backup")
    shutil.copy2(path, backup)
    print("Backup created:", backup)

plan = json.loads(PLAN.read_text(encoding="utf-8"))
story = json.loads(STORY.read_text(encoding="utf-8"))

registry = plan["character_registry"]
villain = plan["principal_villain"]

required = {
    "Adam", "Zayn", "Rayan", "Noah", "Aira",
    "Zara", "Hania", "Meher", "Lumi", villain
}
registered = {item["name"] for item in registry}

if not required.issubset(registered):
    raise ValueError("A required character is missing from the registry.")

# Preserve the exact existing character visual identities.
registry_json = json.dumps(registry, ensure_ascii=False, indent=2)
bible = plan.get("character_bible", "")

prompt = f"""
You are rewriting a cinematic fantasy mystery adventure for a 50-page
illustrated family storybook.

EXISTING CHARACTER REGISTRY (DO NOT CHANGE OR REDESIGN):
{registry_json}

PRINCIPAL VILLAIN: {villain}

EXISTING CHARACTER BIBLE:
{bible}

WORLD:
The Celestial Spires are ancient structures built around a mysterious
artifact capable of altering reality. The artifact is not simply a weapon:
it responds to memory, truth, intention and sacrifice.

CENTRAL TWIST:
Kaelin Darkshadow is not merely searching for the prophecy. He was once
chosen as its guardian, but an earlier attempt to protect the artifact
cost him someone he loved. He now believes controlling reality is the
only way to undo that loss. The prophecy describes his history and warns
that his attempt to rewrite the past will destroy the boundary between
memory and reality.

Lumi, the wise unicorn, witnessed the original guardians' oath. Lumi
knows that the prophecy has been deliberately misinterpreted. Lumi must
help the protagonists understand its real meaning and play an essential
role in the climax.

STORY REQUIREMENTS:
- Adam and Zayn are the two central protagonists.
- Their friendship, contrasting approaches and disagreement must matter.
- Supporting characters must have distinct contributions and relationships.
- Rayan and Meher contribute exploration and survival expertise.
- Noah and Hania investigate evidence and interpret clues.
- Aira and Zara contribute leadership, deduction and decisive action.
- Lumi appears at important turning points, not only at the entrance.
- Kaelin remains the one principal villain. Do not introduce another
  principal villain or reveal a different mastermind.
- The villain must be intelligent, emotionally complex and dangerous.
- Give the artifact clear limitations and consequences.
- Include a meaningful midpoint revelation.
- Include a serious setback before the climax.
- The final confrontation must require teamwork and a difficult choice.
- Resolve Kaelin's fate and the prophecy clearly.
- End the story once. Do not add a sequel teaser or repeated epilogues.

EXACT 50-SCENE STRUCTURE:
1-8: Mystery begins, prophecy, first clues, organization and Lumi.
9-16: Expedition, puzzles, relationships and first confrontation.
17-24: Artifact discovery, rules, Kaelin's connection and disagreement.
25-32: Midpoint twist, betrayal or setback, consequences and regrouping.
33-42: Final investigation, dangerous journey, sacrifice and preparation.
43-50: Climax, final confrontation, prophecy resolution and emotional ending.

OUTLINE QUALITY:
- Exactly 50 sequential scenes, numbered 1 through 50.
- Every scene advances the same continuous story.
- Each beat must describe a concrete event, not a vague reflection.
- Avoid repeating headings, events, discoveries or emotional conclusions.
- Do not defeat the villain before the final confrontation.
- Do not resolve the central conflict before scenes 43-50.
- Keep the chronology consistent.
- Use characters only when they contribute to that scene.
- The final scene must be the actual ending of this story.

Return ONLY valid JSON with this shape:
{{
  "outline": [
    {{"number": 1, "beat": "One or two sentences describing a concrete event."}}
  ]
}}
"""

import storybook_engine as engine

print("Generating a new 50-scene outline...")
result = engine.extract_json(
    engine.cf_text(prompt, max_tokens=5000),
    "revised 50-scene story outline"
)

outline = result.get("outline", [])

if len(outline) != 50:
    raise ValueError(f"Expected 50 outline scenes, received {len(outline)}.")

for index, item in enumerate(outline, 1):
    if item.get("number") != index or not item.get("beat", "").strip():
        raise ValueError(f"Invalid outline entry at scene {index}.")

# Replace only the outline. Preserve all original character information.
plan["outline"] = outline
PLAN.write_text(
    json.dumps(plan, indent=2, ensure_ascii=False),
    encoding="utf-8"
)

# Remove old generated story and scene batches.
# Keep character reference images and their directory untouched.
for path in ROOT.glob("scene_batch_*.json"):
    path.unlink()

if STORY.exists():
    STORY.unlink()

print("SUCCESS: New outline saved.")
print("Scenes:", len(outline))
print("Characters preserved:", len(registry))
print("Principal villain:", villain)
print("Old scene batches removed.")
print("Character reference images preserved.")
print("Next step: regenerate all 50 scenes.")