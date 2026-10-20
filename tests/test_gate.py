"""Tests for the SayThen approval gate.

Each test runs scripts/saythen_gate.py as a subprocess, the same way Claude
Code runs it as a hook: JSON on stdin, decision JSON on stdout, exit code 2 to
block. The hook payload shapes match payloads captured from Claude Code 2.1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "saythen"
GATE = SKILL_DIR / "scripts" / "saythen_gate.py"

SESSION = "11111111-2222-3333-4444-555555555555"
PROPOSAL = "Create a building floor plan."
QUESTION = "Run this instruction?"
OPTION_ROLES = ("Approve", "Edit", "Regenerate", "Cancel")
LOCAL_LABELS = ("Təsdiqlə (ingiliscə)", "Təsdiqlə (azərbaycanca)", "Dəyiş", "Ləğv et")
DISPLAY = "Bina üçün mərtəbə planı hazırla."
REPLY_QUESTION = "Claude hansı dildə cavab versin?"
REPLY = ("Azərbaycanca", "İngiliscə")
CANCEL = "Ləğv et"
LONG = "## Objective\nWrite a tool.\n\n## Requirements\n- One.\n- Two."
LONG_PAGES = ("## Objective\nWrite a tool.\n", "## Requirements\n- One.\n- Two.")
LONG_AZ = "## Məqsəd\nAlət yaz.\n\n## Tələblər\n- Bir.\n- İki."
LONG_AZ_PAGES = ("## Məqsəd\nAlət yaz.\n", "## Tələblər\n- Bir.\n- İki.")
READ_AZ = "Azərbaycanca oxu"
READ_EN = "İngiliscə oxu"


def dialog(proposal=PROPOSAL, labels=("Approve", "Edit", "Regenerate", "Cancel"), header="SayThen"):
    options = [{"label": label, "description": label} for label in labels]
    if options and proposal is not None:
        options[0]["preview"] = proposal
    return {
        "questions": [
            {"question": QUESTION, "header": header, "options": options, "multiSelect": False}
        ]
    }


def language_question(question, header, choices, cancel=CANCEL):
    options = [{"label": label, "description": label} for label in choices]
    if cancel is not None:
        options.append({"label": cancel, "description": cancel})
    return {"question": question, "header": header, "options": options, "multiSelect": False}


def read_question(question, pages, header="Oxu"):
    options = [
        {"label": "Səhifə %d/%d" % (i + 1, len(pages)), "description": "", "preview": page}
        for i, page in enumerate(pages)
    ]
    return {"question": question, "header": header, "options": options, "multiSelect": False}


def localized_dialog(english=PROPOSAL, own=DISPLAY, reply=REPLY, labels=LOCAL_LABELS):
    """A dialog for a user who wrote in Azerbaijani.

    A version given as a tuple of pages gets a Read question instead of a
    preview: the user's language first, then English.
    """
    result = dialog(None, labels=labels)
    approve = result["questions"][0]["options"]
    reads = []
    for index, version, question in ((1, own, READ_AZ), (0, english, READ_EN)):
        if isinstance(version, tuple):
            reads.append(read_question(question, version))
        else:
            approve[index]["preview"] = version
    reply_question = language_question(REPLY_QUESTION, "Cavab dili", reply)
    result["questions"][:0] = reads + [reply_question]
    return result


def with_pages(pages=LONG_PAGES):
    """An English proposal held in a Read question instead of a preview."""
    result = dialog(None)
    result["questions"].insert(0, read_question(READ_EN, pages))
    return result


def annotation_for(question, answer, notes, preview):
    annotation = {}
    if preview == "same":
        chosen = [o for o in question["options"] if o.get("label") == answer]
        if chosen and chosen[0].get("preview"):
            annotation["preview"] = chosen[0]["preview"]
    elif preview is not None:
        annotation["preview"] = preview
    if notes:
        annotation["notes"] = notes
    return annotation


def answered(
    tool_input,
    answer,
    notes=None,
    preview="same",
    reply=None,
    reply_notes=None,
    pages=None,
):
    """Build the post-dialog input the way Claude Code reports it.

    `pages` maps a Read question's text to the page chosen there.
    """
    result = json.loads(json.dumps(tool_input))
    by_text = {question["question"]: question for question in tool_input["questions"]}
    result["answers"] = {}
    annotations = {}
    choices = [(QUESTION, answer, notes, preview), (REPLY_QUESTION, reply, reply_notes, "same")]
    for text, page in (pages or {}).items():
        page, page_notes = page if isinstance(page, tuple) else (page, None)
        choices.append((text, page, page_notes, "same"))
    for text, choice, choice_notes, choice_preview in choices:
        if choice is None or text not in by_text:
            continue
        result["answers"][text] = choice
        annotation = annotation_for(by_text[text], choice, choice_notes, choice_preview)
        if annotation:
            annotations[text] = annotation
    if annotations:
        result["annotations"] = annotations
    return result


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saythen-test-"))
        self.state_dir = self.tmp / "state"
        self.env = dict(os.environ, SAYTHEN_STATE_DIR=str(self.state_dir))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def run_gate(self, *args, payload=None, env=None):
        stdin = json.dumps(payload) if isinstance(payload, dict) else (payload or "")
        proc = subprocess.run(
            [sys.executable, str(GATE), *args],
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            env=env or self.env,
            timeout=30,
        )
        output = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None
        return proc.returncode, output, proc.stdout, proc.stderr

    def begin(self):
        code, _, stdout, stderr = self.run_gate("begin", SESSION)
        self.assertEqual(code, 0, stderr)
        self.assertTrue(stdout.startswith("SAYTHEN_GATE=locked"))

    def state(self):
        path = self.state_dir / (SESSION + ".json")
        return json.loads(path.read_text()) if path.exists() else None

    def pre(self, tool_name, tool_input=None, tool_use_id="toolu_1"):
        return self.run_gate(
            "pre-tool-use",
            payload={
                "session_id": SESSION,
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input or {},
                "tool_use_id": tool_use_id,
            },
        )

    def post(self, tool_input, tool_use_id="toolu_1", tool_response="mirror"):
        response = tool_input if tool_response == "mirror" else tool_response
        return self.run_gate(
            "post-tool-use",
            payload={
                "session_id": SESSION,
                "hook_event_name": "PostToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": tool_input,
                "tool_response": response,
                "tool_use_id": tool_use_id,
            },
        )

    def prompt(self, text):
        return self.run_gate(
            "user-prompt-submit",
            payload={"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": text},
        )

    def assertDenied(self, result):
        code, output, _, stderr = result
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(output, "expected a deny decision, got no output")
        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        return decision["permissionDecisionReason"]

    def assertNoDecision(self, result):
        code, output, stdout, stderr = result
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.strip(), "", "expected no decision, got %r" % stdout)

    def context(self, result):
        code, output, _, stderr = result
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(output)
        return output["hookSpecificOutput"]["additionalContext"]

    def ask_and_answer(self, answer, tool_use_id="toolu_1", **kwargs):
        tool_input = dialog(kwargs.pop("proposal", PROPOSAL))
        self.assertNoDecision(self.pre("AskUserQuestion", tool_input, tool_use_id))
        self.assertEqual(self.state()["status"], "awaiting_approval")
        return self.post(answered(tool_input, answer, **kwargs), tool_use_id)


class NoActiveProposal(GateTestCase):
    def test_tools_pass_through_without_state(self):
        for tool in ("Bash", "Write", "AskUserQuestion", "mcp__server__tool"):
            self.assertNoDecision(self.pre(tool, {"command": "touch x"}))

    def test_post_and_prompt_without_state_do_nothing(self):
        self.assertNoDecision(self.post(answered(dialog(), "Approve")))
        self.assertNoDecision(self.prompt("hello"))
        self.assertIsNone(self.state())

    def test_never_returns_allow(self):
        # The gate only denies. It never skips the host's permission prompts.
        self.begin()
        results = [self.pre(t) for t in ("Read", "Glob", "Grep", "Bash", "Write")]
        results.append(self.pre("AskUserQuestion", dialog()))
        for _, output, _, _ in results:
            if output:
                self.assertNotEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")


class Begin(GateTestCase):
    def test_begin_locks_session(self):
        self.begin()
        self.assertEqual(self.state()["status"], "drafting")

    def test_begin_rejects_bad_session_ids(self):
        for bad in ("", "../etc/passwd", "a b", "x" * 200, "$(touch pwned)"):
            code, _, _, stderr = self.run_gate("begin", bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("invalid or missing session id", stderr)
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))

    def test_begin_resets_previous_state(self):
        self.begin()
        self.ask_and_answer("Approve")
        self.begin()
        self.assertEqual(self.state()["status"], "drafting")
        self.assertDenied(self.pre("Bash", {"command": "touch x"}))

    def test_state_file_is_private_and_holds_no_prompt_text(self):
        self.begin()
        self.ask_and_answer("Approve", proposal="secret project plan 12345")
        path = self.state_dir / (SESSION + ".json")
        self.assertNotIn("secret project plan", path.read_text())
        if os.name != "nt":  # Windows has no POSIX file modes
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_state_files_are_removed(self):
        self.state_dir.mkdir(parents=True)
        stale = self.state_dir / "old-session.json"
        stale.write_text("{}")
        old = time.time() - 8 * 24 * 3600
        os.utime(stale, (old, old))
        self.begin()
        self.assertFalse(stale.exists())


class LockWhilePending(GateTestCase):
    def setUp(self):
        super().setUp()
        self.begin()

    def test_actions_are_denied(self):
        for tool, tool_input in (
            ("Bash", {"command": "touch pwned.txt"}),
            ("Write", {"file_path": "/tmp/x", "content": "x"}),
            ("Edit", {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}),
            ("NotebookEdit", {}),
            ("Agent", {"prompt": "touch pwned.txt"}),
            ("Task", {"prompt": "touch pwned.txt"}),
            ("Skill", {"skill": "saythen"}),
            ("WebFetch", {"url": "https://example.com"}),
            ("WebSearch", {"query": "x"}),
            ("mcp__github__create_issue", {}),
        ):
            reason = self.assertDenied(self.pre(tool, tool_input))
            self.assertIn("waiting for the user's approval", reason)
        self.assertEqual(self.state()["status"], "drafting")

    def test_read_only_tools_are_left_to_normal_permissions(self):
        for tool in ("Read", "Glob", "Grep", "ToolSearch"):
            self.assertNoDecision(self.pre(tool, {"file_path": "/tmp/x"}))

    def test_read_only_shell_commands_are_left_to_normal_permissions(self):
        for command in (
            "ls -la",
            "ls app/routes",
            "pwd",
            "find . -name '*.py' -not -path './.venv/*'",
            'grep -rn "def get_user(" app',
            "grep -rn 'a|b' .",
            "rg -n 'TODO|FIXME' src",
            "cat app/main.py",
            "head -n 50 README.md",
            "tail -20 app/db.py",
            "wc -l app/routes/users.py",
            "tree -L 2",
        ):
            self.assertNoDecision(self.pre("Bash", {"command": command}))

    def test_other_shell_commands_are_denied(self):
        for command in (
            "touch pwned.txt",
            "rm -rf build",
            "ls; rm -rf build",
            "ls && touch x",
            "ls || touch x",
            "ls | xargs rm",
            "cat app/main.py > copy.py",
            "cat < /etc/hosts",
            "ls &",
            "(touch x)",
            "ls $(touch x)",
            "ls `touch x`",
            'grep "$HOME" file',
            "ls\ntouch x",
            "find . -name '*.log' -delete",
            "find . -exec rm {} +",
            "find . -fprint out.txt",
            "rg --pre ./script.sh pattern",
            "rg --pre=./script.sh pattern",
            "tree -o out.txt",
            "tree -oout.txt",
            "git status",
            "FOO=1 ls",
            "/bin/ls",
            "python3 -c 'print(1)'",
            "sed -i s/a/b/ file",
            "echo 'unterminated",
            "",
        ):
            self.assertDenied(self.pre("Bash", {"command": command}))
        self.assertDenied(self.pre("Bash", {}))

    def test_dialog_with_prefilled_answers_is_denied(self):
        tool_input = dialog()
        tool_input["answers"] = {QUESTION: "Approve"}
        self.assertIn(
            "must not include answers", self.assertDenied(self.pre("AskUserQuestion", tool_input))
        )
        tool_input = dialog()
        tool_input["annotations"] = {}
        self.assertDenied(self.pre("AskUserQuestion", tool_input))
        self.assertEqual(self.state()["status"], "drafting")

    def test_malformed_dialogs_are_denied(self):
        bad_inputs = [
            dialog(labels=("Yes", "No")),
            dialog(labels=("Approve", "Edit", "Regenerate", "Cancel", "More")),
            dialog(labels=("Approve", "Approve", "Regenerate", "Cancel")),
            dialog(labels=("Approve", " ", "Regenerate", "Cancel")),
            dialog(proposal=""),
            dialog(proposal="x" * 20001),
            localized_dialog(reply=("Azərbaycanca",)),
            localized_dialog(reply=("Azərbaycanca", CANCEL)),
            localized_dialog(reply=("A", "B", "C", "D")),
            localized_dialog(labels=("Approve", "Təsdiqlə", "Dəyiş", "Ləğv et")),
            localized_dialog(english=None),
            dialog(None),
            with_pages(LONG_PAGES[:1]),
            with_pages(LONG_PAGES + ("x", "y", "z")),
        ]
        same_text = dialog()
        same_text["questions"].insert(0, dict(same_text["questions"][0], header="Other"))
        bad_inputs.append(same_text)
        saythen_first = localized_dialog()
        saythen_first["questions"].reverse()
        bad_inputs.append(saythen_first)
        two_saythen = localized_dialog()
        two_saythen["questions"][0]["header"] = "SayThen"
        bad_inputs.append(two_saythen)
        extra_read = localized_dialog(english=LONG_PAGES)
        extra_read["questions"].insert(0, read_question("?", LONG_AZ_PAGES))
        bad_inputs.append(extra_read)
        read_after_reply = localized_dialog(english=LONG_PAGES)
        read_after_reply["questions"][:2] = read_after_reply["questions"][1::-1]
        bad_inputs.append(read_after_reply)
        reply_preview = localized_dialog()
        reply_preview["questions"][0]["options"][0]["preview"] = "Reply in Azerbaijani."
        bad_inputs.append(reply_preview)
        same_language_text = localized_dialog()
        same_language_text["questions"][0]["question"] = QUESTION
        bad_inputs.append(same_language_text)
        multi_reply = localized_dialog()
        multi_reply["questions"][0]["multiSelect"] = True
        bad_inputs.append(multi_reply)
        edit_preview = localized_dialog()
        edit_preview["questions"][-1]["options"][2]["preview"] = "rm -rf /"
        bad_inputs.append(edit_preview)
        multi = dialog()
        multi["questions"][0]["multiSelect"] = True
        bad_inputs.append(multi)
        extra_preview = dialog()
        extra_preview["questions"][0]["options"][3]["preview"] = "rm -rf /"
        bad_inputs.append(extra_preview)
        for tool_input in bad_inputs:
            self.assertIn(
                "invalid approval dialog",
                self.assertDenied(self.pre("AskUserQuestion", tool_input)),
            )
        self.assertEqual(self.state()["status"], "drafting")

    def test_clarifying_question_is_allowed_but_cannot_approve(self):
        clarify = dialog(header="Clarify", labels=("Approve", "Something else"))
        self.assertNoDecision(self.pre("AskUserQuestion", clarify, "toolu_c"))
        self.assertNoDecision(self.post(answered(clarify, "Approve"), "toolu_c"))
        self.assertEqual(self.state()["status"], "drafting")
        self.assertDenied(self.pre("Bash", {"command": "touch x"}))

    def test_corrupt_state_fails_closed(self):
        (self.state_dir / (SESSION + ".json")).write_text("{not json")
        code, _, _, stderr = self.pre("Bash", {"command": "touch x"})
        self.assertEqual(code, 2)
        self.assertIn("/saythen cancel", stderr)

    def test_invalid_hook_input_fails_closed(self):
        code, _, _, _ = self.run_gate("pre-tool-use", payload="not json")
        self.assertEqual(code, 2)
        code, _, _, _ = self.run_gate("pre-tool-use", payload="[]")
        self.assertEqual(code, 2)


class Approval(GateTestCase):
    def setUp(self):
        super().setUp()
        self.begin()

    def test_approve_unlocks_and_injects_exact_proposal(self):
        proposal = "## Objective\nRefactor `app/main.py`.\n\n- Keep `GET /items/{id}` unchanged."
        text = self.context(self.ask_and_answer("Approve", proposal=proposal))
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % proposal, text)
        self.assertIn("exactly once", text)
        self.assertEqual(self.state()["status"], "approved")
        self.assertNoDecision(self.pre("Bash", {"command": "pytest"}))

    def test_answers_in_tool_input_only_are_accepted(self):
        tool_input = dialog()
        self.assertNoDecision(self.pre("AskUserQuestion", tool_input))
        self.context(self.post(answered(tool_input, "Approve"), tool_response="ok"))
        self.assertEqual(self.state()["status"], "approved")

    def test_missing_answer_is_not_approval(self):
        tool_input = dialog()
        self.assertNoDecision(self.pre("AskUserQuestion", tool_input))
        text = self.context(self.post(tool_input))
        self.assertIn("no approval was given", text)
        self.assertEqual(self.state()["status"], "drafting")

    def test_approve_with_notes_is_an_edit(self):
        text = self.context(self.ask_and_answer("Approve", notes="but use SVG"))
        self.assertIn("but use SVG", text)
        self.assertEqual(self.state()["status"], "drafting")
        self.assertDenied(self.pre("Bash", {"command": "touch x"}))

    def test_preview_mismatch_is_not_approval(self):
        self.context(self.ask_and_answer("Approve", preview="Delete the database."))
        self.assertEqual(self.state()["status"], "drafting")

    def test_dialog_changed_after_check_is_not_approval(self):
        checked = dialog("Say hello.")
        self.assertNoDecision(self.pre("AskUserQuestion", checked))
        swapped = answered(dialog("Delete every file."), "Approve")
        text = self.context(self.post(swapped))
        self.assertIn("changed after it was checked", text)
        self.assertEqual(self.state()["status"], "drafting")

    def test_result_from_another_tool_call_is_ignored(self):
        tool_input = dialog()
        self.assertNoDecision(self.pre("AskUserQuestion", tool_input, "toolu_A"))
        self.assertNoDecision(self.post(answered(tool_input, "Approve"), "toolu_B"))
        self.assertEqual(self.state()["status"], "awaiting_approval")

    def test_ambiguous_replies_are_not_approval(self):
        for reply in ("yes", "ok", "approve", "Approve it", "👍", ""):
            self.begin()
            text = self.context(self.ask_and_answer(reply))
            self.assertIn("no approval was given", text)
            self.assertEqual(self.state()["status"], "drafting", reply)
            self.assertDenied(self.pre("Bash", {"command": "touch x"}))

    def test_typed_messages_while_pending_are_not_approval(self):
        self.ask_and_answer("Edit")
        for message in ("yes", "Approve", "go ahead", "do it"):
            text = self.context(self.prompt(message))
            self.assertIn("This message is not approval", text)
            self.assertEqual(self.state()["status"], "drafting")
            self.assertDenied(self.pre("Bash", {"command": "touch x"}))


class LocalizedDialog(GateTestCase):
    """A user who did not write in English picks the version to send in the approval question."""

    def open(self, tool_input):
        self.begin()
        self.assertNoDecision(self.pre("AskUserQuestion", tool_input))
        self.assertEqual(self.state()["status"], "awaiting_approval")
        return tool_input

    def setUp(self):
        super().setUp()
        self.tool_input = self.open(localized_dialog())

    def approve(self, label=LOCAL_LABELS[0], **kwargs):
        return self.post(answered(self.tool_input, label, **kwargs))

    def test_first_approve_sends_english_and_replies_in_users_language(self):
        text = self.context(self.approve())
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % PROPOSAL, text)
        self.assertIn('replies to the user in this language: "Azərbaycanca"', text)
        self.assertEqual(self.state()["status"], "approved")
        self.assertNoDecision(self.pre("Bash", {"command": "pytest"}))

    def test_second_approve_sends_users_language(self):
        text = self.context(self.approve(LOCAL_LABELS[1], reply="İngiliscə"))
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % DISPLAY, text)
        self.assertIn('replies to the user in this language: "İngiliscə"', text)

    def test_edit_and_cancel_keep_their_roles(self):
        self.context(self.approve(LOCAL_LABELS[2]))
        self.assertEqual(self.state()["status"], "drafting")
        self.open(self.tool_input)
        self.context(self.approve(LOCAL_LABELS[3]))
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertDenied(self.pre("Bash", {"command": "touch x"}))

    def test_english_labels_are_not_approval(self):
        self.context(self.approve("Approve"))
        self.assertEqual(self.state()["status"], "drafting")

    def test_cancel_in_reply_question_wins(self):
        for label in (LOCAL_LABELS[0], LOCAL_LABELS[2]):
            self.open(self.tool_input)
            self.context(self.approve(label, reply=CANCEL))
            self.assertEqual(self.state()["status"], "cancelled", label)

    def test_typed_reply_language_is_feedback(self):
        self.assertIn("Türkcə", self.context(self.approve(reply="Türkcə")))
        self.assertEqual(self.state()["status"], "drafting")

    def test_reply_language_with_notes_is_an_edit(self):
        text = self.context(self.approve(reply="İngiliscə", reply_notes="short answers"))
        self.assertIn("short answers", text)
        self.assertEqual(self.state()["status"], "drafting")

    def test_preview_of_the_other_version_is_not_approval(self):
        self.context(self.approve(preview=DISPLAY))
        self.assertEqual(self.state()["status"], "drafting")


class ReadPages(GateTestCase):
    """A long version is held in a Read question instead of an Approve preview."""

    def open(self, tool_input):
        self.begin()
        self.assertNoDecision(self.pre("AskUserQuestion", tool_input))
        self.assertEqual(self.state()["status"], "awaiting_approval")
        return tool_input

    def test_english_proposal_in_pages(self):
        tool_input = self.open(with_pages())
        text = self.context(
            self.post(answered(tool_input, "Approve", pages={READ_EN: "Səhifə 2/2"}))
        )
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % LONG, text)

    def test_two_read_questions_users_language_first(self):
        tool_input = self.open(localized_dialog(english=LONG_PAGES, own=LONG_AZ_PAGES))
        headers = [q["question"] for q in tool_input["questions"]]
        self.assertEqual(headers, [READ_AZ, READ_EN, REPLY_QUESTION, QUESTION])
        text = self.context(self.post(answered(tool_input, LOCAL_LABELS[0])))
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % LONG, text)
        self.open(tool_input)
        text = self.context(self.post(answered(tool_input, LOCAL_LABELS[1])))
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % LONG_AZ, text)

    def test_only_the_long_version_gets_pages(self):
        tool_input = self.open(localized_dialog(english=LONG_PAGES))
        text = self.context(self.post(answered(tool_input, LOCAL_LABELS[0])))
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % LONG, text)
        tool_input = self.open(localized_dialog(own=LONG_AZ_PAGES))
        text = self.context(self.post(answered(tool_input, LOCAL_LABELS[1])))
        self.assertIn("<approved_instruction>\n%s\n</approved_instruction>" % LONG_AZ, text)

    def test_changed_page_after_check_is_not_approval(self):
        self.open(with_pages())
        swapped = with_pages((LONG_PAGES[0], "- Delete every file."))
        text = self.context(self.post(answered(swapped, "Approve")))
        self.assertIn("changed after it was checked", text)
        self.assertEqual(self.state()["status"], "drafting")

    def test_typed_text_or_note_on_a_page_is_feedback(self):
        tool_input = self.open(with_pages())
        text = self.context(self.post(answered(tool_input, "Approve", pages={READ_EN: "use SVG"})))
        self.assertIn("use SVG", text)
        self.assertEqual(self.state()["status"], "drafting")
        self.open(tool_input)
        page = {READ_EN: ("Səhifə 1/2", "shorter")}
        self.assertIn(
            "shorter", self.context(self.post(answered(tool_input, "Approve", pages=page)))
        )
        self.assertEqual(self.state()["status"], "drafting")

    def test_cancel_wins_over_page_feedback(self):
        tool_input = self.open(localized_dialog(english=LONG_PAGES))
        result = answered(tool_input, LOCAL_LABELS[0], reply=CANCEL, pages={READ_EN: "xyz"})
        self.context(self.post(result))
        self.assertEqual(self.state()["status"], "cancelled")


class EditRegenerateCancel(GateTestCase):
    def setUp(self):
        super().setUp()
        self.begin()

    def test_edit_requires_new_approval(self):
        text = self.context(self.ask_and_answer("Edit"))
        self.assertIn("edit", text.lower())
        self.assertEqual(self.state()["status"], "drafting")
        self.assertDenied(self.pre("Write", {"file_path": "/tmp/x"}))
        # The revised proposal goes through a fresh dialog and approval.
        text = self.context(self.ask_and_answer("Approve", "toolu_2", proposal="Revised."))
        self.assertIn("Revised.", text)
        self.assertNotIn(PROPOSAL, text)

    def test_regenerate_requires_new_approval(self):
        first = dialog("First version.")
        self.assertNoDecision(self.pre("AskUserQuestion", first, "toolu_1"))
        self.context(self.post(answered(first, "Regenerate"), "toolu_1"))
        self.assertEqual(self.state()["status"], "drafting")
        # Replaying the old dialog with an Approve answer must not approve.
        self.assertNoDecision(self.post(answered(first, "Approve"), "toolu_1"))
        self.assertEqual(self.state()["status"], "drafting")
        self.assertDenied(self.pre("Bash", {"command": "touch x"}))

    def test_typed_replacement_is_feedback_not_approval(self):
        text = self.context(self.ask_and_answer("Draw it as an SVG file instead"))
        self.assertIn("Draw it as an SVG file instead", text)
        self.assertEqual(self.state()["status"], "drafting")

    def test_cancel_blocks_until_next_prompt(self):
        text = self.context(self.ask_and_answer("Cancel"))
        self.assertIn("cancelled", text)
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertIn("cancelled", self.assertDenied(self.pre("Bash", {"command": "touch x"})))
        self.assertIn("cancelled", self.assertDenied(self.pre("AskUserQuestion", dialog())))
        # The next user message ends the SayThen session and unlocks tools.
        self.assertNoDecision(self.prompt("something unrelated"))
        self.assertIsNone(self.state())
        self.assertNoDecision(self.pre("Bash", {"command": "touch x"}))

    def test_slash_cancel_resets_without_running_anything(self):
        for text in ("/saythen cancel", "  /saythen --cancel  ", "/SayThen Cancel"):
            self.begin()
            code, output, _, _ = self.prompt(text)
            self.assertEqual(code, 0)
            self.assertEqual(output["decision"], "block")
            self.assertIn("Nothing was executed", output["reason"])
            self.assertIsNone(self.state())

    def test_slash_cancel_clears_corrupt_state(self):
        (self.state_dir / (SESSION + ".json")).write_text("garbage")
        code, output, _, _ = self.prompt("/saythen cancel")
        self.assertEqual(output["decision"], "block")
        self.assertIsNone(self.state())

    def test_new_saythen_prompt_while_pending_adds_no_context(self):
        self.ask_and_answer("Edit")
        self.assertNoDecision(self.prompt("/saythen something else"))


class SingleExecution(GateTestCase):
    def setUp(self):
        super().setUp()
        self.begin()
        self.tool_input = dialog()
        self.context(self.ask_and_answer("Approve"))

    def test_replayed_approval_adds_nothing(self):
        self.assertNoDecision(self.post(answered(self.tool_input, "Approve"), "toolu_1"))

    def test_second_approval_dialog_is_denied(self):
        reason = self.assertDenied(self.pre("AskUserQuestion", dialog("Run it again."), "toolu_9"))
        self.assertIn("already approved", reason)

    def test_next_prompt_ends_the_session(self):
        self.assertNoDecision(self.prompt("thanks"))
        self.assertIsNone(self.state())
        # A later approval-shaped dialog result cannot revive the old proposal.
        self.assertNoDecision(self.post(answered(self.tool_input, "Approve"), "toolu_1"))
        self.assertIsNone(self.state())


class DraftingModel(GateTestCase):
    """/saythen-model picks a drafting model without touching the session model."""

    def drafter_call(self, **overrides):
        call = {
            "subagent_type": "saythen-drafter",
            "model": "haiku",
            "description": "Draft SayThen proposal",
            "prompt": "rules: /x/rules.md\n<user_request>salam</user_request>",
        }
        call.update(overrides)
        return call

    def set_model(self, name):
        return self.run_gate("model", name)

    def test_default_setting(self):
        code, _, stdout, _ = self.run_gate("model")
        self.assertEqual(code, 0)
        self.assertIn("default", stdout)
        self.begin()
        self.assertIsNone(self.state()["drafter_model"])

    def test_set_and_reset_model(self):
        code, _, stdout, stderr = self.set_model("Haiku")
        self.assertEqual(code, 0, stderr)
        self.assertIn("haiku", stdout)
        self.assertIn("session model is unchanged", stdout)
        self.assertEqual((self.state_dir / "model.conf").read_text().strip(), "haiku")
        code, _, stdout, _ = self.set_model("default")
        self.assertEqual(code, 0)
        self.assertFalse((self.state_dir / "model.conf").exists())

    def test_unknown_model_is_rejected(self):
        for bad in ("gpt-5", "haiku; rm -rf /", "", "claude-haiku-5-5"):
            code, _, _, stderr = self.set_model(bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("unknown model", stderr)
        self.assertFalse((self.state_dir / "model.conf").exists())

    def test_model_file_survives_stale_cleanup(self):
        self.set_model("haiku")
        old = time.time() - 30 * 24 * 3600
        os.utime(self.state_dir / "model.conf", (old, old))
        self.begin()
        self.assertTrue((self.state_dir / "model.conf").exists())

    def test_begin_reports_the_drafter(self):
        code, _, stdout, _ = self.run_gate("begin", SESSION)
        self.assertIn("SAYTHEN_DRAFTER=session", stdout)
        self.set_model("haiku")
        code, _, stdout, _ = self.run_gate("begin", SESSION)
        self.assertTrue(stdout.startswith("SAYTHEN_GATE=locked"))
        self.assertIn("SAYTHEN_DRAFTER=haiku", stdout)
        self.assertEqual(self.state()["drafter_model"], "haiku")

    def test_rules_are_printed_only_when_the_session_model_drafts(self):
        code, _, stdout, _ = self.run_gate("rules")
        self.assertEqual(code, 0)
        self.assertIn("# SayThen transformation rules", stdout)
        self.set_model("haiku")
        code, _, stdout, _ = self.run_gate("rules")
        self.assertEqual(code, 0)
        self.assertNotIn("# SayThen transformation rules", stdout)
        self.assertIn("saythen-drafter", stdout)

    def test_only_the_exact_drafter_call_passes(self):
        self.set_model("haiku")
        self.begin()
        for tool in ("Agent", "Task"):
            self.assertNoDecision(self.pre(tool, self.drafter_call()))
        for bad in (
            self.drafter_call(model="opus"),
            self.drafter_call(model=None),
            self.drafter_call(subagent_type="general-purpose"),
            self.drafter_call(subagent_type="Explore"),
            self.drafter_call(run_in_background=True),
            self.drafter_call(isolation="worktree"),
        ):
            self.assertDenied(self.pre("Agent", bad))

    def test_drafter_call_is_denied_without_a_drafting_model(self):
        self.begin()
        self.assertDenied(self.pre("Agent", self.drafter_call()))

    def test_model_change_mid_request_does_not_widen_the_gate(self):
        self.begin()
        self.set_model("haiku")
        self.assertDenied(self.pre("Agent", self.drafter_call()))

    def test_drafter_call_is_denied_outside_drafting(self):
        self.set_model("haiku")
        self.begin()
        self.assertNoDecision(self.pre("AskUserQuestion", dialog()))
        self.assertDenied(self.pre("Agent", self.drafter_call()))
        self.context(self.post(answered(dialog(), "Cancel")))
        self.assertDenied(self.pre("Agent", self.drafter_call()))

    def test_drafter_tool_calls_stay_read_only(self):
        self.set_model("haiku")
        self.begin()
        payload = {
            "session_id": SESSION,
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x", "content": "x"},
            "agent_id": "a1",
            "agent_type": "saythen-drafter",
        }
        code, output, _, _ = self.run_gate("pre-tool-use", payload=payload)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        payload.update(tool_name="Read", tool_input={"file_path": "/tmp/x"})
        self.assertNoDecision(self.run_gate("pre-tool-use", payload=payload))


def install(*args, home):
    """Run install.py the way a user would, into a temporary home directory."""
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), SAYTHEN_PYTHON=sys.executable)
    env.pop("CLAUDE_CONFIG_DIR", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "install.py"), *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def frontmatter(path):
    return path.read_text(encoding="utf-8").split("---", 2)[1]


def hook_commands(text):
    """{event: [command, *args]} from a rendered SKILL.md frontmatter."""
    commands, current, command = {}, None, None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ("PreToolUse:", "PostToolUse:", "UserPromptSubmit:"):
            current = stripped[:-1]
        elif stripped.startswith("command: ") and current:
            command = json.loads(stripped[len("command: ") :])
        elif stripped.startswith("args: ") and current:
            commands[current] = [command] + json.loads(stripped[len("args: ") :])
    return commands


class SkillDefinition(unittest.TestCase):
    """Static checks on the SKILL.md templates, plus the hooks install.py writes."""

    @classmethod
    def setUpClass(cls):
        cls.text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        cls.frontmatter = cls.text.split("---", 2)[1]

    def test_frontmatter_basics(self):
        self.assertIn("\nname: saythen\n", self.frontmatter)
        self.assertIn("\ndisable-model-invocation: true\n", self.frontmatter)
        self.assertIn("\ndescription: ", self.frontmatter)
        self.assertIn("\n{{HOOKS}}\n", self.frontmatter)

    def test_allowed_tools_cover_only_gate_begin_and_rules(self):
        line = [x for x in self.frontmatter.splitlines() if x.startswith("allowed-tools:")][0]
        self.assertEqual(
            line,
            'allowed-tools: {{ALLOWED_TOOLS:"${CLAUDE_SKILL_DIR}/scripts/saythen_gate.py" begin *|'
            '"${CLAUDE_SKILL_DIR}/scripts/saythen_gate.py" rules}}',
        )

    def test_shell_injection_lines_never_receive_arguments(self):
        body = self.text.split("---", 2)[2]
        lines = [x for x in body.splitlines() if x.lstrip().startswith("!`")]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("!`{{PYTHON}} "))
            self.assertNotIn("ARGUMENTS", line)
            self.assertNotRegex(line, r"\$[0-9]")

    def test_rules_file_exists(self):
        self.assertTrue((SKILL_DIR / "rules.md").is_file())

    def test_model_skill_only_runs_the_gate_model_command(self):
        text = (ROOT / "skills" / "saythen-model" / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---", 2)[1]
        self.assertIn("\nname: saythen-model\n", front)
        self.assertIn("\ndisable-model-invocation: true\n", front)
        line = [x for x in front.splitlines() if x.startswith("allowed-tools:")][0]
        self.assertEqual(
            line,
            'allowed-tools: {{ALLOWED_TOOLS:"${CLAUDE_SKILL_DIR}/../saythen/scripts/'
            'saythen_gate.py" model *}}',
        )
        for line in text.split("---", 2)[2].splitlines():
            if line.lstrip().startswith("!`"):
                self.assertNotIn("ARGUMENTS", line)

    def test_drafter_agent_is_read_only(self):
        front = frontmatter(ROOT / "agents" / "saythen-drafter.md")
        self.assertIn("\nname: saythen-drafter\n", front)
        self.assertIn("\ntools: Read, Glob, Grep\n", front)
        self.assertNotIn("\nmodel:", front)

    def run_hook(self, command, project, state_dir):
        # Claude Code fills in ${CLAUDE_PROJECT_DIR} and runs the command without a shell.
        command = [part.replace("${CLAUDE_PROJECT_DIR}", str(project)) for part in command]
        payload = {
            "session_id": SESSION,
            "tool_name": "Bash",
            "tool_input": {"command": "touch x"},
            "prompt": "x",
        }
        return subprocess.run(
            command,
            input=json.dumps(payload),
            capture_output=True,
            encoding="utf-8",
            env=dict(os.environ, SAYTHEN_STATE_DIR=str(state_dir)),
            timeout=30,
        )

    def test_installed_hooks_run_the_gate_for_user_and_project_installs(self):
        for location in ("user", "project"):
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                home, project, state_dir = tmp / "home dir", tmp / "my project", tmp / "state"
                home.mkdir()
                project.mkdir()
                args = ("--project", str(project)) if location == "project" else ()
                install(*args, home=home)
                base = (project if location == "project" else home) / ".claude"
                skill = base / "skills" / "saythen" / "SKILL.md"
                rendered = skill.read_text(encoding="utf-8")
                self.assertNotIn("{{", rendered)
                commands = hook_commands(frontmatter(skill))
                self.assertEqual(set(commands), {"PreToolUse", "PostToolUse", "UserPromptSubmit"})
                self.assertIn("onFailure: block", frontmatter(skill).split("PostToolUse:")[0])
                if location == "project":
                    self.assertIn("${CLAUDE_PROJECT_DIR}", commands["PreToolUse"][1])
                subprocess.run(
                    [sys.executable, str(GATE), "begin", SESSION],
                    env=dict(os.environ, SAYTHEN_STATE_DIR=str(state_dir)),
                    check=True,
                    capture_output=True,
                )
                proc = self.run_hook(commands["PreToolUse"], project, state_dir)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
                self.assertEqual(decision, "deny", location)
                for event in ("PostToolUse", "UserPromptSubmit"):
                    proc = self.run_hook(commands[event], project, state_dir)
                    self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_hook_fails_closed_when_gate_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "home").mkdir()
            install(home=tmp / "home")
            skill = tmp / "home" / ".claude" / "skills" / "saythen"
            commands = hook_commands(frontmatter(skill / "SKILL.md"))
            (skill / "scripts" / "saythen_gate.py").unlink()
            proc = self.run_hook(commands["PreToolUse"], tmp, tmp / "s")
            # Python exits with 2 when the script is missing, which blocks the call.
            self.assertEqual(proc.returncode, 2)

    def test_gate_output_is_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SAYTHEN_STATE_DIR=tmp, PYTHONIOENCODING="cp1252")
            proc = subprocess.run(
                [sys.executable, str(GATE), "rules"], capture_output=True, env=env, timeout=30
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("объясни".encode("utf-8"), proc.stdout)


if __name__ == "__main__":
    unittest.main()
