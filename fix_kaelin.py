from pathlib import Path

p = Path("storybook_engine.py")
s = p.read_text(encoding="utf-8")

old = '            scene["characters_present"] = visible_names'

new = '''            character_aliases = {"Kaelin": "Kaelin Darkshadow"}
            visible_names = [
                character_aliases.get(name, name) if isinstance(name, str) else name
                for name in visible_names
            ]
            scene["characters_present"] = visible_names'''

assert s.count(old) == 1, "Expected code line not found exactly once"

p.write_text(s.replace(old, new), encoding="utf-8")

print("SUCCESS: Kaelin alias fixed.")