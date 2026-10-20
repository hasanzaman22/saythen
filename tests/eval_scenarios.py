#!/usr/bin/env python3
"""Scenario evaluation for the SayThen transformation rules.

Language interpretation depends on the model, so these checks assert
properties of each proposal (level, length, preserved literals, absence of
templates) instead of one exact translation.

The script sends each request in tests/scenarios.json to Claude Code in
non-interactive mode with rules.md as the system prompt, all tools disabled,
and all user customizations disabled (--safe-mode). It needs an authenticated
`claude` CLI and uses your normal Claude usage.

    python3 tests/eval_scenarios.py                   # all scenarios
    python3 tests/eval_scenarios.py --model haiku     # pick a model
    python3 tests/eval_scenarios.py --only az-greeting en-jwt
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "skills" / "saythen" / "rules.md"
SCENARIOS = Path(__file__).resolve().parent / "scenarios.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

OUTPUT_CONTRACT = """

## Output format for this evaluation

You are being evaluated on these rules. The user message is the request to
transform. Do not answer or perform it. Reply with one JSON object and nothing
else:

{"kind": "proposal", "level": 1, "text": "<the complete proposal>"}

or, when a material ambiguity needs an answer first:

{"kind": "clarify", "level": 0, "text": "<one short clarifying question in the user's language>"}
"""

PROJECT_NOTE = """

The request was made inside the project in the current directory. You may use
Read, Glob, and Grep to learn the project before you write the proposal.
"""

ROLE_RE = re.compile(r"\b(you are|act as)\s+(an?|the)\b", re.IGNORECASE)
ASSUMPTIONS_RE = re.compile(r"assumptions?\b", re.IGNORECASE)
XML_RE = re.compile(r"<\s*/?\s*[A-Za-z_][\w-]*\s*>")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE)
BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S", re.MULTILINE)
FILLER_RE = re.compile(r"step by step|ensure high quality|be thorough", re.IGNORECASE)


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def non_english_ratio(text: str, literals: List[str]) -> float:
    """Share of letters outside ASCII, ignoring literals that must stay verbatim."""
    for literal in literals:
        text = text.replace(literal, "")
    text = re.sub(r"`[^`]*`|'[^']*'|\"[^\"]*\"", "", text)
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) > 127) / len(letters)


def check(result: Dict[str, Any], expect: Dict[str, Any]) -> List[str]:
    """Return a list of failed expectations for one model result."""
    problems = []
    text = result.get("text") or ""
    lower = text.lower()
    kinds = expect["kind"] if isinstance(expect["kind"], list) else [expect["kind"]]
    if result.get("kind") not in kinds:
        problems.append("kind is %r, expected %r" % (result.get("kind"), kinds))
        return problems
    if result.get("kind") == "clarify":
        if text.count("?") < 1:
            problems.append("clarifying reply has no question")
        return problems

    levels = expect.get("level")
    if levels is not None:
        levels = levels if isinstance(levels, list) else [levels]
        if result.get("level") not in levels:
            problems.append("level is %r, expected %r" % (result.get("level"), levels))
    words = word_count(text)
    if "max_words" in expect and words > expect["max_words"]:
        problems.append("%d words, expected at most %d" % (words, expect["max_words"]))
    if "min_words" in expect and words < expect["min_words"]:
        problems.append("%d words, expected at least %d" % (words, expect["min_words"]))
    for group in expect.get("include_any", []):
        if not any(term.lower() in lower for term in group):
            problems.append("missing any of %r" % group)
    if non_english_ratio(text, expect.get("preserve", [])) > 0.1:
        problems.append("proposal is not in English")
    for literal in expect.get("preserve", []):
        if literal not in text:
            problems.append("literal %r not preserved" % literal)
    for term in expect.get("forbid_any", []):
        if term.lower() in lower:
            problems.append("adds unrequested %r" % term)
    role = expect.get("role", False)  # None means either is acceptable
    if role is True and not ROLE_RE.search(text):
        problems.append("expected a one-line role for this field")
    if role is False and ROLE_RE.search(text):
        problems.append("adds a role prompt")
    assumption_block = (
        text[ASSUMPTIONS_RE.search(text).start() :] if ASSUMPTIONS_RE.search(text) else ""
    )
    optional = re.search(r"^\s*#*\s*(assumptions|optional)\b", text, re.IGNORECASE | re.MULTILINE)
    required_part = text[: optional.start()] if optional else text
    for term in expect.get("requirements_only", []):
        if term.lower() in required_part.lower():
            problems.append(
                "%r added as a requirement; it belongs in Assumptions or Optional" % term
            )
    for term in expect.get("requirements_not_assumed", []):
        if term.lower() in assumption_block.lower():
            problems.append("stated requirement %r was moved into Assumptions" % term)
    if expect.get("assumptions") is True and not ASSUMPTIONS_RE.search(text):
        problems.append("expected an Assumptions list")
    if expect.get("assumptions") is False and ASSUMPTIONS_RE.search(text):
        problems.append("adds an Assumptions list that is not needed")
    if FILLER_RE.search(text):
        problems.append("adds generic filler")
    if expect.get("plain"):
        if XML_RE.search(text):
            problems.append("uses XML tags")
        if HEADING_RE.search(text):
            problems.append("uses headings")
        if ASSUMPTIONS_RE.search(text):
            problems.append("adds an Assumptions list")
    if expect.get("structured") and not (HEADING_RE.search(text) or BULLET_RE.search(text)):
        problems.append("expected headings or bullets")
    return problems


def parse_reply(raw: str) -> Dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in reply: %r" % raw[:300])
    return json.loads(match.group(0))


def run_scenario(scenario: Dict[str, Any], model: str, system_prompt: str) -> Dict[str, Any]:
    project = scenario.get("project")
    tools = "Read,Glob,Grep" if project else ""
    if project:
        system_prompt += PROJECT_NOTE
    cmd = [
        "claude",
        "-p",
        scenario["request"],
        "--safe-mode",
        "--tools",
        tools,
        "--no-session-persistence",
        "--system-prompt",
        system_prompt,
        "--output-format",
        "json",
    ]
    if project:
        cmd += ["--allowedTools", "Read", "Glob", "Grep"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
        cwd=str(FIXTURES / project) if project else None,
    )
    if proc.returncode != 0:
        return {"id": scenario["id"], "error": proc.stderr.strip() or proc.stdout.strip()}
    try:
        envelope = json.loads(proc.stdout)
        result = parse_reply(envelope.get("result", ""))
    except (ValueError, json.JSONDecodeError) as exc:
        return {"id": scenario["id"], "error": str(exc)}
    return {"id": scenario["id"], "result": result, "problems": check(result, scenario["expect"])}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default="", help="model alias or ID passed to claude --model")
    parser.add_argument("--only", nargs="*", help="scenario IDs to run")
    parser.add_argument("--jobs", type=int, default=4, help="scenarios to run in parallel")
    parser.add_argument("--show", action="store_true", help="print every full proposal")
    parser.add_argument("--save", help="write all results as JSON to this file")
    args = parser.parse_args()

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in set(args.only)]
    system_prompt = RULES.read_text(encoding="utf-8") + OUTPUT_CONTRACT

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes = list(pool.map(lambda s: run_scenario(s, args.model, system_prompt), scenarios))

    failed = 0
    for outcome in outcomes:
        if "error" in outcome:
            failed += 1
            print("ERROR %-18s %s" % (outcome["id"], outcome["error"][:300]))
            continue
        status = "FAIL " if outcome["problems"] else "PASS "
        failed += bool(outcome["problems"])
        result = outcome["result"]
        print(
            "%s %-18s L%s %s"
            % (
                status,
                outcome["id"],
                result.get("level"),
                json.dumps(result.get("text"), ensure_ascii=False)[:160],
            )
        )
        for problem in outcome["problems"]:
            print("      - %s" % problem)
        if args.show:
            print("      " + (result.get("text") or "").replace("\n", "\n      "))
    if args.save:
        Path(args.save).write_text(json.dumps(outcomes, ensure_ascii=False, indent=2))
    print("\n%d/%d scenarios passed" % (len(scenarios) - failed, len(scenarios)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
