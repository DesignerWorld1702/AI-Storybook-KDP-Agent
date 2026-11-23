"""AI-assisted visual quality control for generated storybook illustrations.

Uses Cloudflare Workers AI Moondream to compare each generated scene with its
written direction and the selected character reference portraits. Failed scenes
are regenerated a limited number of times; unresolved failures stop packaging.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

MODEL = os.getenv("CLOUDFLARE_VISION_MODEL", "@cf/moondream/moondream3.1-9B-A2B").strip()

def _is_quota_exhaustion_error(error: Exception | str) -> bool:
    """Recognize Cloudflare's exhausted daily free-allocation response."""
    message = str(error).casefold()
    return (
        "used up your daily free allocation" in message
        or "error code 4006" in message
        or '"code":4006' in message
        or '"code": 4006' in message
    )



def _data_uri(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((768, 768), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _comparison_board(scene_path: Path, reference_paths: list[Path]) -> str:
    """Place the generated scene beside its identity portraits for direct comparison."""
    scene = Image.open(scene_path).convert("RGB")
    scene.thumbnail((768, 768), Image.Resampling.LANCZOS)
    refs = []
    for path in reference_paths[:5]:
        if path and Path(path).is_file():
            ref = Image.open(path).convert("RGB")
            ref.thumbnail((240, 300), Image.Resampling.LANCZOS)
            refs.append((Path(path).stem, ref.copy()))
            ref.close()
    label_h = 34
    ref_w = 260
    board_w = 768 + (ref_w if refs else 0)
    board_h = max(768, len(refs) * 334)
    board = Image.new("RGB", (board_w, board_h), "white")
    board.paste(scene, (0, 0))
    draw = ImageDraw.Draw(board)
    for i, (name, ref) in enumerate(refs):
        x, y = 768, i * 334
        board.paste(ref, (x + 10, y + label_h))
        draw.text((x + 10, y + 8), name.replace("_", " ")[:32], fill="black")
    board.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    board.save(buffer, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _vision_review(account: str, token: str, image_uri: str, scene: dict, story: dict) -> dict:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}"
    cast = []
    registry = {str(x.get("name")): x for x in story.get("character_registry", []) if isinstance(x, dict)}
    for name in scene.get("characters_present", []):
        item = registry.get(name, {})
        cast.append({
            "name": name,
            "visual_id": item.get("visual_id", ""),
            "physical_identity": item.get("immutable_identity", {}),
            "story_costume": item.get("story_costume", ""),
            "role": item.get("role", "")
        })
    question = (
        "Act as a strict visual art director reviewing one finished storybook illustration. "
        "The left panel is the generated scene. The right panel, if present, contains character "
        "identity reference portraits. Compare carefully and judge only visible evidence. "
        "STRICT SCENE FIDELITY: compare image against exact page text. Check the literal location, "
        "who is present, each person's action, physical position (standing/sitting/lying/leaning), "
        "who interacts with whom, important props, and emotional beat. If text describes people "
        "talking in a room, the illustration must show that room, visible furniture, and people "
        "staged in the described positions, facing/listening to one another with natural conversational "
        "gestures—not generic posing or a mismatched setting. If one stands while another sits, "
        "verify that distinction. Dialogue bubbles are added during layout; check only whether the "
        "visual acting supports the specified speaker/listener exchange. Also check cast visibility "
        "when framing allows, likeness to reference portraits (face, hair, skin, body, marks), costume "
        "continuity, anatomy defects, merged bodies, extra limbs, malformed hands/faces, unwanted text, "
        "watermarks, blur and major composition issues. Return ONLY JSON keys: score (0-10), "
        "scene_match (true/false), character_identity_match (true/false), major_defects (array), "
        "minor_defects (array), observed_characters (array), repair_instruction (one concise instruction). "
        "Score below 7 or any major defect means fail. Be strict; do not pass to be encouraging.\\n\\n"
        f"BOOK: {story.get('title','')}\\nSCENE {scene.get('number')}: {scene.get('heading','')}\\n"
        f"PAGE TEXT: {scene.get('text','')}\\n"
        f"ILLUSTRATION DIRECTION: {scene.get('visual_prompt','')}\\n"
        f"COMPOSITION: {scene.get('composition','')}\\n"
        f"ACTION: {scene.get('action_pose','')}\\nGAZE: {scene.get('gaze','')}\\n"
        f"EXPRESSION: {scene.get('expression','')}\\n"
        f"DIALOGUE (bubbles rendered later): {json.dumps(scene.get('dialogue', []), ensure_ascii=False)}\\n"
        f"CAST AND LOCKED IDENTITIES: {json.dumps(cast, ensure_ascii=False)}"
    )
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"task": "query", "image": image_uri, "question": question,
              "reasoning": False, "max_tokens": 700},
        timeout=180
    )
    if not response.ok:
        raise RuntimeError(f"Cloudflare visual review failed ({response.status_code}): {response.text[:500]}")
    payload = response.json().get("result", {})
    answer = payload.get("answer") if isinstance(payload, dict) else None
    if not answer:
        raise RuntimeError("Vision model returned no review answer.")
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Vision model did not return the required JSON review.")
    report = json.loads(answer[start:end + 1])
    if not isinstance(report.get("score"), (int, float)) or isinstance(report.get("score"), bool):
        raise ValueError("Vision review has no numeric score.")
    if not isinstance(report.get("major_defects"), list) or not isinstance(report.get("minor_defects"), list):
        raise ValueError("Vision review has invalid defect lists.")
    report["passed"] = (
        float(report["score"]) >= 7
        and report.get("scene_match") is True
        and (not cast or report.get("character_identity_match") is True)
        and not report["major_defects"]
    )
    return report


def review_and_repair_scene_images(story: dict, scenes_dir: Path, workdir: Path,
                                   references: list[str], engine, max_regenerations: int = 2) -> dict:
    """Review all 50 scenes, regenerate failures, then require a clean second review."""
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not account or not token:
        raise RuntimeError("Cloudflare credentials are required for visual quality review.")
    scenes_dir, workdir = Path(scenes_dir), Path(workdir)
    width, height = engine.scene_image_dimensions(story.get("trim_size", "8.5 × 8.5 in — square picture book"))
    reports = []
    for index, scene in enumerate(story.get("scenes", []), 1):
        path = scenes_dir / f"scene_{index:03d}.png"
        last = None
        for attempt in range(max_regenerations + 1):
            scene_refs = engine.scene_reference_images(story, scene, workdir, references[0])
            identity_refs = [Path(p) for p in scene_refs[1:]]
            expected_cast = scene.get("characters_present", [])
            if expected_cast and not identity_refs:
                raise RuntimeError(
                    f"Scene {index} lists recurring characters but has no identity reference portraits."
                )
            board = _comparison_board(path, identity_refs)
            try:
                last = _vision_review(account, token, board, scene, story)
            except Exception as exc:
                if _is_quota_exhaustion_error(exc):
                    raise RuntimeError(
                        "Cloudflare daily Workers AI allocation is exhausted; stopping visual review "
                        "without wasting retries. Resume after the account allocation resets."
                    ) from exc
                if attempt == max_regenerations:
                    raise RuntimeError(f"Visual review could not complete for scene {index}: {exc}") from exc
                logging.warning("Visual review error on scene %s; retrying review: %s", index, exc)
                time.sleep(2)
                continue
            last["scene_number"] = index
            last["attempt"] = attempt + 1
            if last["passed"]:
                break
            if attempt == max_regenerations:
                break
            logging.warning("Scene %s failed visual review (%s/10); regenerating: %s",
                            index, last["score"], last.get("major_defects"))
            path.unlink(missing_ok=True)
            retry_scene = dict(scene)
            repair_notes = str(last.get("repair_instruction", "")).strip()
            defects = "; ".join(str(x) for x in last.get("major_defects", []) if str(x).strip())
            retry_scene["visual_prompt"] = (
                str(scene.get("visual_prompt", "")).strip()
                + "\\nVISUAL REVIEW REPAIR REQUIREMENTS (must correct on this regeneration): "
                + repair_notes + ("; " + defects if defects else "")
            )
            engine.cf_image(
                engine.build_scene_prompt(story, retry_scene, engine.STYLE_LOCK),
                path, width=width, height=height, reference_images=scene_refs
            )
            if not path.is_file():
                raise RuntimeError(f"Regenerated scene {index} is not a valid image.")
            time.sleep(0.5)
        reports.append(last or {"scene_number": index, "passed": False, "score": 0,
                                "major_defects": ["No visual review result."]})
        if not reports[-1].get("passed"):
            (workdir / "visual_quality_report.json").write_text(
                json.dumps({"passed": False, "scenes": reports}, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            raise RuntimeError(
                f"Visual quality gate failed at scene {index}; automated regeneration limit reached."
            )
    report = {
        "passed": len(reports) == 50 and all(x.get("passed") for x in reports),
        "reviewed_scenes": len(reports),
        "model": MODEL,
        "method": "Cloudflare Workers AI image-to-text visual review with side-by-side character reference portraits",
        "average_score": round(sum(float(x["score"]) for x in reports) / max(1, len(reports)), 2),
        "character_consistency_verified": all(x.get("character_identity_match") is True for x in reports),
        "scene_accuracy_verified": all(x.get("scene_match") is True for x in reports),
        "scenes": reports,
        "limitations": "AI visual review is an automated quality filter, not a guarantee of perfect human-level art direction."
    }
    (workdir / "visual_quality_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if not report["passed"]:
        raise RuntimeError("Visual quality gate did not pass all 50 scenes.")
    return report
