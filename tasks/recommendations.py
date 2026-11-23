import json
import re


_BLOCK = re.compile(
    r"(?:\A|\n)FLAMETHROWER_GATE_RECOMMENDATION\r?\n(.*?)\r?\nEND_FLAMETHROWER_GATE_RECOMMENDATION\s*\Z",
    re.DOTALL,
)
_ARCHITECT_BLOCK = re.compile(
    r"(?:\A|\n)FLAMETHROWER_ARCHITECT_PLAN\r?\n(.*?)\r?\nEND_FLAMETHROWER_ARCHITECT_PLAN\s*\Z",
    re.DOTALL,
)
_QUESTION_BLOCK = re.compile(
    r"(?:\A|\n)FLAMETHROWER_QUESTION\r?\n(.*?)\r?\nEND_FLAMETHROWER_QUESTION\s*\Z",
    re.DOTALL,
)
_DEVELOPER_COMPLETE = "FLAMETHROWER_DEVELOPER_COMPLETE\nEND_FLAMETHROWER_DEVELOPER_COMPLETE"


def unique_marker_lines(text, start, end):
    lines = text.splitlines()
    return lines.count(start) == 1 and lines.count(end) == 1


def question_text(text):
    match = _QUESTION_BLOCK.search(text)
    if not match or not unique_marker_lines(text, "FLAMETHROWER_QUESTION", "END_FLAMETHROWER_QUESTION"):
        return ""
    return match.group(1).strip()


def developer_complete(text):
    normalized = text.replace("\r\n", "\n").strip()
    return (
        normalized.endswith(_DEVELOPER_COMPLETE)
        and unique_marker_lines(normalized, "FLAMETHROWER_DEVELOPER_COMPLETE", "END_FLAMETHROWER_DEVELOPER_COMPLETE")
        and (normalized == _DEVELOPER_COMPLETE or normalized[-len(_DEVELOPER_COMPLETE) - 1] == "\n")
    )


def architect_plan(text):
    match = _ARCHITECT_BLOCK.search(text)
    if (
        not match or not unique_marker_lines(text, "FLAMETHROWER_ARCHITECT_PLAN", "END_FLAMETHROWER_ARCHITECT_PLAN")
    ):
        return ""
    return match.group(1).strip()


def final_agent_report(events):
    chunks = []
    for event in reversed(events):
        if event.kind != "message":
            break
        chunks.append(event.content)
    text = "".join(reversed(chunks)).strip()
    match = _BLOCK.search(text)
    recommendation = None
    if match and unique_marker_lines(text, "FLAMETHROWER_GATE_RECOMMENDATION", "END_FLAMETHROWER_GATE_RECOMMENDATION"):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            fields = ("result", "recommendation", "summary", "evidence", "untested", "reason")
            if all(isinstance(data.get(key), str) and data[key].strip() for key in fields):
                allowed = {
                    "passed": ("advance", "hold"),
                    "failed": ("rework", "hold"),
                    "unverified": ("rework", "hold"),
                }
                if data["recommendation"] in allowed.get(data["result"], ()):
                    recommendation = {key: data[key].strip() for key in fields}
    return {"text": text, "recommendation": recommendation}


def recommendation_evidence(run, recommendation):
    return (
        f"Agent recommendation from {run.get_stage_display()} run #{run.pk} (not independently verified):\n"
        f"Summary: {recommendation['summary']}\n"
        f"Evidence: {recommendation['evidence']}\n"
        f"Untested: {recommendation['untested']}\n"
        f"Reason: {recommendation['reason']}"
    )
