from pathlib import Path

p = Path("storybook_engine.py")
s = p.read_text(encoding="utf-8")

# 1. Back up the original file.
backup = Path("storybook_engine.py.backup")
backup.write_text(s, encoding="utf-8")

# 2. Only reuse cached batches containing valid story text.
old_cache = '''                if len(cached_batch.get("scenes", [])) == last - first + 1:
                    batch_scenes = cached_batch["scenes"]'''

new_cache = '''                cached_scenes = cached_batch.get("scenes", [])
                if (
                    len(cached_scenes) == last - first + 1
                    and all(
                        isinstance(scene, dict)
                        and isinstance(scene.get("text"), str)
                        and scene["text"].strip()
                        for scene in cached_scenes
                    )
                ):
                    batch_scenes = cached_scenes'''

assert s.count(old_cache) == 1, "Cached batch section not found exactly once."
s = s.replace(old_cache, new_cache)

# 3. Remove the premature cache write.
old_write = '''            if batch_path:
                batch_path.write_text(
                    json.dumps({"scenes": batch_scenes}, indent=2, ensure_ascii=False),
                    encoding="utf-8"
                )

        for offset, scene in enumerate(batch_scenes):'''

new_write = '''        for offset, scene in enumerate(batch_scenes):'''

assert s.count(old_write) == 1, "Premature cache write section not found."
s = s.replace(old_write, new_write)

# 4. Repair missing visual prompts from the existing scene information.
old_check = '''            if not scene.get("text") or not scene.get("visual_prompt"):
                raise ValueError(f"Scene {number} is missing text or a visual prompt.")'''

new_check = '''            if not isinstance(scene, dict):
                raise ValueError(f"Scene {number} must be a JSON object.")

            if not isinstance(scene.get("text"), str) or not scene["text"].strip():
                raise ValueError(f"Scene {number} is missing its story text.")

            if not isinstance(scene.get("visual_prompt"), str) or not scene["visual_prompt"].strip():
                scene["visual_prompt"] = (
                    f"Illustrate this story moment: {scene.get('heading', '')}. "
                    f"Story action: {scene['text']} "
                    f"Visible characters: {', '.join(scene.get('characters_present', []))}. "
                    f"Shot type: {scene.get('shot_type', 'cinematic medium shot')}. "
                    f"Camera angle: {scene.get('camera_angle', 'eye-level')}. "
                    f"Composition: {scene.get('composition', '')}. "
                    f"Action and pose: {scene.get('action_pose', '')}. "
                    f"Gaze: {scene.get('gaze', '')}. "
                    f"Expression: {scene.get('expression', '')}. "
                    f"Lighting: {scene.get('lighting', '')}. "
                    "Create a detailed cinematic anime-realism illustration. "
                    "Preserve the established character identities and story continuity."
                )'''

assert s.count(old_check) == 1, "Scene validation section not found."
s = s.replace(old_check, new_check)

# 5. Save the batch only after every scene has passed validation.
old_end = '''            scenes.append(scene)

    plan.pop("outline", None)'''

new_end = '''            scenes.append(scene)

        if batch_path:
            batch_path.write_text(
                json.dumps({"scenes": batch_scenes}, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

    plan.pop("outline", None)'''

assert s.count(old_end) == 1, "Scene batch ending section not found."
s = s.replace(old_end, new_end)

p.write_text(s, encoding="utf-8")
print("SUCCESS: Story repair and batch validation changes applied.")
print("Original backup: storybook_engine.py.backup")