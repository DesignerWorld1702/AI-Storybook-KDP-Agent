from pathlib import Path

p = Path("storybook_engine.py")
s = p.read_text(encoding="utf-8")

old = '''            unknown_names = [name for name in visible_names if name not in known_names]
            if unknown_names:
                raise ValueError(f"Scene {number} uses unregistered character names: {unknown_names}")'''

new = '''            # Generic lowercase group descriptions (e.g. minions or guards)
            # are not individual registered character identities.
            generic_labels = [
                name for name in visible_names
                if isinstance(name, str)
                and name not in known_names
                and name[:1].islower()
            ]
            visible_names = [
                name for name in visible_names
                if name not in generic_labels
            ]
            scene["characters_present"] = visible_names

            unknown_names = [
                name for name in visible_names
                if name not in known_names
            ]
            if unknown_names:
                raise ValueError(
                    f"Scene {number} uses unregistered character names: {unknown_names}"
                )'''

assert s.count(old) == 1, "Could not find the exact character validation block."

Path("storybook_engine.py.backup2").write_text(s, encoding="utf-8")
p.write_text(s.replace(old, new), encoding="utf-8")

print("SUCCESS: Generic group labels are now handled separately.")
print("Backup: storybook_engine.py.backup2")