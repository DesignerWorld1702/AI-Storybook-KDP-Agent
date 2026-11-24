import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("storybook_engine", Path(__file__).parents[1] / "storybook_engine.py")
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


def test_outline_review_findings_become_scene_specific_repair_instructions():
    report = {
        "critical_issues": [
            {"number": 20, "reason": "kidnapping motive is unexplained", "evidence_quote": "Her sister is taken."},
            {"number": 34, "reason": "injury has no plot consequence", "evidence_quote": "Jim is wounded."},
            {"number": 46, "reason": "return is not a distinct event", "evidence_quote": "She returns home."},
        ]
    }
    instructions = engine.outline_issue_repair_instructions(report)
    assert "SCENE 20" in instructions and "kidnapping motive is unexplained" in instructions
    assert "Original beat (quote): Her sister is taken." in instructions
    assert "SCENE 34" in instructions and "injury has no plot consequence" in instructions
    assert "SCENE 46" in instructions and "return is not a distinct event" in instructions


def test_outline_repair_instructions_empty_when_no_issues():
    assert engine.outline_issue_repair_instructions({"critical_issues": []}) == "No reviewer issue-specific instructions were supplied."
