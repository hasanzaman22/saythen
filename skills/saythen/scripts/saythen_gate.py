#!/usr/bin/env python3
"""SayThen approval gate for Claude Code.

The gate keeps one small JSON state file per Claude Code session. While a
SayThen proposal is pending, the PreToolUse hook denies every tool call except
read-only lookups and the approval dialog itself. The session unlocks only when
the PostToolUse hook sees that the user picked "Approve" in the native
AskUserQuestion dialog that displayed the proposal.

The dialog is one AskUserQuestion call; the SayThen approval question is
last. When the user did not write in English, the approval question's options
are: approve and send the English version, approve and send the user's
language version, Edit, Cancel; a reply language question (ending with
Cancel) comes before it. Options are identified by position, so their labels
can be in the user's language.

Claude Code's preview box cannot scroll. A version that is too long for it
has no preview on its Approve option; instead a Read question before the
approval question holds it as pages, one per option, which the user turns
with the arrow keys. The pages joined by newlines are the version that runs.
Read questions come in this order: the user's language, then English.

The user can pick a separate model for drafting with /saythen-model. Drafting
then runs in the read-only saythen-drafter subagent on that model, and the
session model stays as it is. The gate lets only that one subagent call
through while a proposal is being drafted.

Subcommands:
  begin <session_id>    Lock the session. Run by the skill before Claude sees it.
  rules                 Print the transformation rules, unless drafting is delegated.
  model [<name>]        Print or set the drafting model (haiku, sonnet, opus,
                        fable, or default for the session model).
  pre-tool-use          PreToolUse hook (JSON on stdin).
  post-tool-use         PostToolUse hook (JSON on stdin).
  user-prompt-submit    UserPromptSubmit hook (JSON on stdin).
  status <session_id>   Print the session state (for debugging and tests).

Only the Python standard library is used. The state file stores hashes and
status, never the user's request or the proposal text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

STATE_VERSION = 1
DIALOG_HEADER = "SayThen"
# Meaning of the approval question's options, by position. Labels may be in
# the user's language. A proposal in another language has two Approve options:
# send the English version (first) or the user's language version.
OPTION_ROLES = ("Approve", "Edit", "Regenerate", "Cancel")
TRANSLATED_ROLES = ("Approve English", "Approve translation", "Edit", "Cancel")
# The reply-language question offers two or three languages followed by Cancel.
MAX_LANGUAGE_OPTIONS = 4
MAX_PAGES = 4

# Drafting models. These are the aliases the Agent tool accepts. "default"
# means the session model drafts the proposal itself.
DRAFTER_MODELS = ("haiku", "sonnet", "opus", "fable")
DEFAULT_MODEL = "default"
DRAFTER_AGENT = "saythen-drafter"
AGENT_TOOLS = frozenset({"Agent", "Task"})
MODEL_FILE = "model.conf"
MAX_PROPOSAL_CHARS = 20000
STALE_SECONDS = 7 * 24 * 3600

# Tools that only read local data. They stay available while a proposal is
# pending so Claude can look at the project before it writes the instruction.
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "ToolSearch"})

# Shell commands allowed while a proposal is pending. Claude Code on macOS and
# Linux explores projects with Bash because Glob and Grep are not in its
# default tool set. Only a single command from this list passes, with no shell
# operators, substitutions, or variables, and without the flags that make it
# write files or run other programs. git is excluded because repository
# configuration can make it run arbitrary programs.
READ_ONLY_COMMANDS = {
    "ls": frozenset(),
    "pwd": frozenset(),
    "cat": frozenset(),
    "head": frozenset(),
    "tail": frozenset(),
    "wc": frozenset(),
    "grep": frozenset(),
    "find": frozenset(
        {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
    ),
    "rg": frozenset({"--pre", "--pre-glob"}),
    "tree": frozenset({"-o"}),
}
SHELL_SPECIAL_CHARS = frozenset("`$\\\n\r")
SHELL_OPERATOR_CHARS = frozenset("();<>|&")

DRAFTING = "drafting"
AWAITING = "awaiting_approval"
APPROVED = "approved"
CANCELLED = "cancelled"
LOCKED_STATUSES = frozenset({DRAFTING, AWAITING, CANCELLED})

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CANCEL_PROMPTS = frozenset({"/saythen cancel", "/saythen --cancel"})

LOCKED_REASON = (
    "SayThen: a proposal is waiting for the user's approval, so no actions are "
    "allowed yet. Only Read, Glob, Grep, single read-only shell commands (ls, "
    "find, grep, rg, cat, head, tail, wc, tree, pwd), and the approval dialog "
    "are available. Call AskUserQuestion once, as the SayThen skill describes: "
    'the last question has the header "SayThen" and four options (Approve, '
    "Edit, Regenerate, Cancel; or, for a user who did not write in English: "
    "approve English, approve their language, Edit, Cancel, with a reply "
    "language question before it). Each Approve option previews its full "
    "version, or has no preview and a Read question with its pages."
)
CANCELLED_REASON = (
    "SayThen: the user cancelled this proposal. Do not perform the request. "
    "Confirm the cancellation in one short sentence and end your turn."
)


class GateError(Exception):
    """Invalid input that the gate refuses to act on."""


# --------------------------------------------------------------------------
# State storage
# --------------------------------------------------------------------------


def state_dir() -> Path:
    override = os.environ.get("SAYTHEN_STATE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        return Path(base) / "saythen"
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "saythen"


def validate_session_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise GateError("invalid or missing session id")
    return session_id


def state_path(session_id: str) -> Path:
    return state_dir() / (validate_session_id(session_id) + ".json")


def load_state(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the state dict, None when absent, or raise GateError if unreadable."""
    path = state_path(session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GateError("cannot read state file: %s" % exc) from exc
    try:
        state = json.loads(raw)
    except ValueError as exc:
        raise GateError("state file is corrupt") from exc
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise GateError("state file has an unknown format")
    return state


def save_state(session_id: str, state: Dict[str, Any]) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    state["version"] = STATE_VERSION
    state["updated_at"] = time.time()
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(state_path(session_id)))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_state(session_id: str) -> None:
    try:
        state_path(session_id).unlink()
    except FileNotFoundError:
        pass


def remove_stale_states(now: float) -> None:
    directory = state_dir()
    if not directory.is_dir():
        return
    for entry in directory.glob("*.json"):
        try:
            if now - entry.stat().st_mtime > STALE_SECONDS:
                entry.unlink()
        except OSError:
            pass


def read_drafter_model() -> Optional[str]:
    """Return the configured drafting model, or None for the session model."""
    try:
        value = (state_dir() / MODEL_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GateError("cannot read the model setting: %s" % exc) from exc
    if value in DRAFTER_MODELS:
        return value
    if value == DEFAULT_MODEL:
        return None
    raise GateError("the model setting is invalid; run /saythen-model default")


def write_drafter_model(value: str) -> None:
    if value not in DRAFTER_MODELS + (DEFAULT_MODEL,):
        raise GateError(
            "unknown model %s; use one of: %s"
            % (json.dumps(value), ", ".join(DRAFTER_MODELS + (DEFAULT_MODEL,)))
        )
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / MODEL_FILE
    if value == DEFAULT_MODEL:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_text(value + "\n", encoding="utf-8")


def describe_drafter_model(value: Optional[str]) -> str:
    if value is None:
        return "SayThen model: default (the session model drafts proposals)"
    return "SayThen model: %s (only for drafting; the session model is unchanged)" % value


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Hook output helpers
# --------------------------------------------------------------------------


def deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def post_context(text: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }


def prompt_context(text: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }


# --------------------------------------------------------------------------
# Approval dialog parsing
# --------------------------------------------------------------------------


def find_saythen_question(tool_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return None
    for question in questions:
        if isinstance(question, dict) and question.get("header") == DIALOG_HEADER:
            return question
    return None


def check_option_labels(options: Any, count: Optional[int] = None) -> list:
    if not isinstance(options, list) or not all(isinstance(o, dict) for o in options):
        raise GateError("every question needs a list of options")
    labels = [o.get("label") for o in options]
    if count is not None and len(labels) != count:
        raise GateError("the SayThen question needs exactly %d options" % count)
    if not all(isinstance(label, str) and label.strip() for label in labels):
        raise GateError("every option needs a label")
    if len(set(label.strip() for label in labels)) != len(labels):
        raise GateError("option labels must be different from each other")
    return labels


def check_preview(preview: Any, what: str) -> str:
    if not isinstance(preview, str) or not preview.strip():
        raise GateError("%s must carry its full text as the preview" % what)
    if len(preview) > MAX_PROPOSAL_CHARS:
        raise GateError("%s is longer than %d characters; shorten it" % (what, MAX_PROPOSAL_CHARS))
    return preview


def check_single_select(question: Any) -> Dict[str, Any]:
    if not isinstance(question, dict):
        raise GateError("every question must be an object")
    if question.get("multiSelect"):
        raise GateError("SayThen questions must not be multi-select")
    if not isinstance(question.get("question"), str) or not question["question"].strip():
        raise GateError("every SayThen question needs question text")
    return question


def check_reply_question(question: Any, texts: set) -> Dict[str, Any]:
    """Validate the reply-language question: languages first, then a Cancel option."""
    question = check_single_select(question)
    if question.get("header") == DIALOG_HEADER:
        raise GateError('only the first question may have the header "SayThen"')
    if question["question"] in texts:
        raise GateError("every question needs different question text")
    texts.add(question["question"])
    options = question.get("options")
    labels = check_option_labels(options)
    if not 3 <= len(labels) <= MAX_LANGUAGE_OPTIONS:
        raise GateError(
            "the reply language question needs 2 to %d languages and then Cancel"
            % (MAX_LANGUAGE_OPTIONS - 1)
        )
    if any(option.get("preview") for option in options):
        raise GateError("the reply language options must not have previews")
    choices = [[label, None] for label in labels[:-1]]
    return {"question": question["question"], "choices": choices, "cancel": labels[-1]}


def check_read_question(question: Any, texts: set) -> Dict[str, Any]:
    """Validate a Read question: one version split into pages, one per option."""
    question = check_single_select(question)
    if question.get("header") == DIALOG_HEADER:
        raise GateError('only the approval question may have the header "SayThen"')
    if question["question"] in texts:
        raise GateError("every question needs different question text")
    texts.add(question["question"])
    options = question.get("options")
    labels = check_option_labels(options)
    if not 2 <= len(labels) <= MAX_PAGES:
        raise GateError("a Read question needs 2 to %d pages" % MAX_PAGES)
    pages = [check_preview(option.get("preview"), "every page") for option in options]
    return {"question": question["question"], "labels": labels, "text": "\n".join(pages)}


def has_previews(question: Any) -> bool:
    options = question.get("options") if isinstance(question, dict) else None
    return isinstance(options, list) and any(
        isinstance(o, dict) and o.get("preview") for o in options
    )


def extract_dialog(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a SayThen approval dialog and return what it shows.

    The result holds the approval question's text, labels, and option roles,
    the previews of its options, the versions that can run ("versions", by
    Approve option), the Read questions ("reads"), and the reply languages
    ("reply").
    """
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise GateError("the SayThen dialog must contain one to four questions")
    approval = check_single_select(questions[-1])
    if approval.get("header") != DIALOG_HEADER:
        raise GateError('the last question is the approval question, with the header "SayThen"')
    options = approval.get("options")
    labels = check_option_labels(options, len(OPTION_ROLES))
    previews = [o.get("preview") or None for o in options]
    texts = {approval["question"]}
    before = list(questions[:-1])
    reply = None
    if before and not has_previews(before[-1]):
        reply = check_reply_question(before.pop(), texts)
    roles = TRANSLATED_ROLES if reply else OPTION_ROLES
    # Approve options: English first, then the user's language. Read questions
    # come in the opposite order: the user's language first.
    approve_slots = [0, 1] if reply else [0]
    for index, preview in enumerate(previews):
        if index in approve_slots:
            if preview is not None:
                check_preview(preview, "an Approve option")
        elif preview is not None:
            raise GateError("only the Approve options may have a preview")
    paged = [i for i in reversed(approve_slots) if previews[i] is None]
    if len(before) != len(paged):
        raise GateError(
            "every Approve option needs either its full text as the preview or one Read "
            "question with its pages, and nothing else may come before the approval question"
        )
    reads = [check_read_question(question, texts) for question in before]
    versions = list(previews[: len(approve_slots)])
    for index, read in zip(paged, reads):
        versions[index] = read["text"]
    if reply and {label.strip() for label in labels} & set(OPTION_ROLES):
        raise GateError(
            "the user did not write in English, so write the option labels in their language too"
        )
    return {
        "question": approval["question"],
        "labels": labels,
        "roles": list(roles),
        "previews": previews,
        "versions": versions,
        "reads": reads,
        "reply": reply,
    }


def dialog_sha256(dialog: Dict[str, Any]) -> str:
    return sha256(json.dumps(dialog, sort_keys=True, ensure_ascii=False))


def find_answer(payload: Dict[str, Any], question_text: str) -> Optional[Dict[str, Any]]:
    """Return {"answer", "notes", "preview"} from the dialog result, if present.

    Claude Code records the user's choice in an `answers` map keyed by question
    text, plus per-question `annotations` that hold the preview of the chosen
    option and any free-text notes. tool_response is checked first, then the
    post-dialog tool_input.
    """
    for source_key in ("tool_response", "tool_input"):
        source = payload.get(source_key)
        if not isinstance(source, dict):
            continue
        answers = source.get("answers")
        if not isinstance(answers, dict) or question_text not in answers:
            continue
        answer = answers[question_text]
        if not isinstance(answer, str):
            continue
        notes = ""
        preview = None
        annotations = source.get("annotations")
        entry = annotations.get(question_text) if isinstance(annotations, dict) else None
        if isinstance(entry, dict):
            if isinstance(entry.get("notes"), str):
                notes = entry["notes"].strip()
            if isinstance(entry.get("preview"), str):
                preview = entry["preview"]
        return {"answer": answer.strip(), "notes": notes, "preview": preview}
    return None


# --------------------------------------------------------------------------
# Event handlers
# --------------------------------------------------------------------------


def is_read_only_command(command: Any) -> bool:
    """True only for one simple command from READ_ONLY_COMMANDS.

    The check is deliberately strict. Anything it cannot parse with certainty,
    such as pipes, redirects, command substitution, or variables, is rejected.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    if any(char in SHELL_SPECIAL_CHARS for char in command):
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens:
        return False
    for token in tokens:
        if token and all(char in SHELL_OPERATOR_CHARS for char in token):
            return False
    program, args = tokens[0], tokens[1:]
    if program not in READ_ONLY_COMMANDS:
        return False
    blocked = READ_ONLY_COMMANDS[program]
    for arg in args:
        if arg in blocked or any(arg.startswith(flag + "=") for flag in blocked):
            return False
        if program == "tree" and arg.startswith("-o"):
            return False
    return True


def is_drafter_call(state: Dict[str, Any], tool_input: Dict[str, Any]) -> bool:
    """True for the one subagent call that drafts the proposal on the chosen model."""
    model = state.get("drafter_model")
    return (
        state.get("status") == DRAFTING
        and model in DRAFTER_MODELS
        and tool_input.get("subagent_type") == DRAFTER_AGENT
        and tool_input.get("model") == model
        and not tool_input.get("run_in_background")
        and not tool_input.get("isolation")
    )


def handle_begin(session_id: str) -> str:
    now = time.time()
    remove_stale_states(now)
    drafter = read_drafter_model()
    save_state(
        session_id,
        {
            "status": DRAFTING,
            "created_at": now,
            "round": 0,
            "proposal_sha256": None,
            "ask_tool_use_id": None,
            "approved_sha256": None,
            "drafter_model": drafter,
        },
    )
    return (
        "SAYTHEN_GATE=locked (only read-only tools and the SayThen dialog until "
        "approval)\nSAYTHEN_DRAFTER=%s" % (drafter or "session")
    )


def handle_rules(rules_path: Path) -> str:
    if read_drafter_model() is not None:
        return "(The %s subagent reads the rules itself.)" % DRAFTER_AGENT
    return rules_path.read_text(encoding="utf-8")


def handle_pre_tool_use(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    session_id = validate_session_id(payload.get("session_id"))
    state = load_state(session_id)
    if state is None:
        return None
    status = state.get("status")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    if status == APPROVED:
        if tool_name == "AskUserQuestion" and find_saythen_question(tool_input):
            return deny(
                "SayThen: this proposal was already approved and runs once. Carry "
                "out the approved instruction; do not ask for approval again. For a "
                "new request the user must run /saythen again."
            )
        return None
    if status not in LOCKED_STATUSES:
        raise GateError("unknown gate status")
    if status == CANCELLED:
        return deny(CANCELLED_REASON)

    if tool_name in READ_ONLY_TOOLS:
        return None
    if tool_name == "Bash" and is_read_only_command(tool_input.get("command")):
        return None
    if tool_name in AGENT_TOOLS and is_drafter_call(state, tool_input):
        return None
    if tool_name != "AskUserQuestion":
        return deny(LOCKED_REASON)

    # Only the user may answer the dialog. Input that already carries answers
    # would let the model approve its own proposal.
    if "answers" in tool_input or "annotations" in tool_input:
        return deny("SayThen: AskUserQuestion input must not include answers or annotations.")
    if find_saythen_question(tool_input) is None:
        # A clarifying question. Allowed; it can never approve anything.
        return None
    try:
        dialog = extract_dialog(tool_input)
    except GateError as exc:
        return deny("SayThen: invalid approval dialog: %s. %s" % (exc, LOCKED_REASON))
    state["status"] = AWAITING
    state["round"] = int(state.get("round") or 0) + 1
    state["proposal_sha256"] = dialog_sha256(dialog)
    state["ask_tool_use_id"] = payload.get("tool_use_id")
    save_state(session_id, state)
    return None


def handle_post_tool_use(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if payload.get("tool_name") != "AskUserQuestion":
        return None
    session_id = validate_session_id(payload.get("session_id"))
    state = load_state(session_id)
    if state is None:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or find_saythen_question(tool_input) is None:
        return None
    if state.get("status") != AWAITING:
        return None
    if payload.get("tool_use_id") != state.get("ask_tool_use_id"):
        return None

    try:
        dialog = extract_dialog(tool_input)
    except GateError:
        dialog = None

    def back_to_drafting(message: str) -> Dict[str, Any]:
        state["status"] = DRAFTING
        state["proposal_sha256"] = None
        state["ask_tool_use_id"] = None
        save_state(session_id, state)
        return post_context("SayThen: no approval was given. " + message)

    def feedback(text: str) -> Dict[str, Any]:
        return back_to_drafting(
            "The user typed this instead of choosing an option: %s\nTreat it as "
            "feedback (or as the new proposal if it is one), revise, and ask again."
            % json.dumps(text, ensure_ascii=False)
        )

    def note_as_edit(notes: str) -> Dict[str, Any]:
        return back_to_drafting(
            "The user added this note, so treat it as an edit: %s\nRevise and ask "
            "again." % json.dumps(notes, ensure_ascii=False)
        )

    if dialog is None or dialog_sha256(dialog) != state.get("proposal_sha256"):
        return back_to_drafting(
            "The dialog content changed after it was checked, so it cannot be "
            "approved. Present the proposal again."
        )
    reply = find_answer(payload, dialog["question"])
    if reply is None:
        return back_to_drafting("The user's choice could not be read. Present the proposal again.")

    labels = [label.strip() for label in dialog["labels"]]
    answer, notes = reply["answer"], reply["notes"]
    index = labels.index(answer) if answer in labels else None
    role = dialog["roles"][index] if index is not None else None
    # The reply language left unanswered is the first option, the user's language.
    language = None
    if dialog["reply"]:
        first = {"answer": dialog["reply"]["choices"][0][0].strip(), "notes": ""}
        language = find_answer(payload, dialog["reply"]["question"]) or first
        if language["answer"] == dialog["reply"]["cancel"].strip():
            role = "Cancel"
    # Choosing a page only turns pages. Typed text or a note there is feedback.
    if role != "Cancel":
        for read in dialog["reads"]:
            page = find_answer(payload, read["question"])
            if page is not None and page["answer"] not in [x.strip() for x in read["labels"]]:
                return feedback(page["answer"])
            if page is not None and page["notes"]:
                return feedback(page["notes"])
    if role is not None and role.startswith("Approve"):
        if reply["preview"] not in (None, dialog["previews"][index]):
            return back_to_drafting(
                "The preview the user saw does not match the proposal. Present the proposal again."
            )
        if notes:
            return note_as_edit(notes)
        reply_language = ""
        if language is not None:
            names = [label.strip() for label, _ in dialog["reply"]["choices"]]
            if language["answer"] not in names:
                return feedback(language["answer"])
            if language["notes"]:
                return note_as_edit(language["notes"])
            reply_language = (
                "\nWrite your replies to the user in this language: %s. Text that the "
                "instruction asks for in a specific language keeps that language, and "
                "code follows the project's conventions."
                % json.dumps(
                    dialog["reply"]["choices"][names.index(language["answer"])][0],
                    ensure_ascii=False,
                )
            )
        instruction = dialog["versions"][index]
        state["status"] = APPROVED
        state["approved_sha256"] = sha256(instruction)
        state["ask_tool_use_id"] = None
        save_state(session_id, state)
        return post_context(
            "SayThen: approved. Tools are unlocked. Carry out the instruction below "
            "exactly once, now, as the user's request. Do not change its scope or "
            "run /saythen again; normal permission prompts apply.%s\n"
            "<approved_instruction>\n%s\n</approved_instruction>" % (reply_language, instruction)
        )
    if role == "Cancel":
        state["status"] = CANCELLED
        state["proposal_sha256"] = None
        state["ask_tool_use_id"] = None
        save_state(session_id, state)
        return post_context(
            "SayThen: cancelled. Do not perform the request. Say so in one short sentence and stop."
        )
    if role == "Regenerate":
        return back_to_drafting(
            "The user wants a different version with the same intent. Ask again."
        )
    if role == "Edit":
        return back_to_drafting(
            "The user wants to edit it. Ask, in their language, what to change, then end "
            "your turn. When they reply, revise and ask again."
        )
    return feedback(answer)


def handle_user_prompt_submit(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    session_id = validate_session_id(payload.get("session_id"))
    prompt = payload.get("prompt")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    if prompt.lower() in CANCEL_PROMPTS:
        clear_state(session_id)
        return {
            "decision": "block",
            "reason": "SayThen: cancelled. Nothing was executed.",
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit"},
        }
    try:
        state = load_state(session_id)
    except GateError:
        # An unreadable state file must not lock the user out. The next
        # /saythen run rewrites it; anything else starts from a clean slate.
        clear_state(session_id)
        return None
    if state is None:
        return None
    if state.get("status") in (APPROVED, CANCELLED):
        clear_state(session_id)
        return None
    if prompt.startswith("/saythen"):
        return None
    return prompt_context(
        "SayThen: a proposal still awaits approval; tools stay locked. This message "
        "is not approval: treat it as a change, revise, and ask again with the "
        "SayThen dialog (or the user can type /saythen cancel)."
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def read_payload() -> Dict[str, Any]:
    # Hook input is UTF-8 JSON on every OS, whatever the console code page.
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError as exc:
        raise GateError("hook input is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GateError("hook input must be a JSON object")
    return payload


def emit(result: Optional[Dict[str, Any]]) -> None:
    if result is not None:
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.stdout.write("\n")


def use_utf8_output() -> None:
    """Write UTF-8 even where the console code page is not (Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv: list) -> int:
    use_utf8_output()
    if len(argv) < 2:
        sys.stderr.write(__doc__ or "")
        return 64
    command = argv[1]
    try:
        if command == "begin":
            if len(argv) != 3:
                raise GateError("usage: saythen_gate.py begin <session_id>")
            print(handle_begin(validate_session_id(argv[2])))
            return 0
        if command == "rules":
            if len(argv) != 2:
                raise GateError("usage: saythen_gate.py rules")
            print(handle_rules(Path(__file__).resolve().parent.parent / "rules.md"))
            return 0
        if command == "model":
            if len(argv) > 3:
                raise GateError("usage: saythen_gate.py model [<name>]")
            if len(argv) == 3:
                write_drafter_model(argv[2].strip().lower())
            print(describe_drafter_model(read_drafter_model()))
            return 0
        if command == "status":
            if len(argv) != 3:
                raise GateError("usage: saythen_gate.py status <session_id>")
            print(json.dumps(load_state(validate_session_id(argv[2]))))
            return 0
        handlers = {
            "pre-tool-use": handle_pre_tool_use,
            "post-tool-use": handle_post_tool_use,
            "user-prompt-submit": handle_user_prompt_submit,
        }
        if command not in handlers:
            raise GateError("unknown command: %s" % command)
        emit(handlers[command](read_payload()))
        return 0
    except GateError as exc:
        # Exit code 2 blocks the tool call or prompt and shows the reason. The
        # gate fails closed: when it cannot decide, nothing runs.
        sys.stderr.write("SayThen gate: %s. Type /saythen cancel to reset.\n" % exc)
        return 2
    except Exception as exc:  # noqa: BLE001 - any crash must still fail closed
        sys.stderr.write(
            "SayThen gate: internal error (%s). Type /saythen cancel to reset.\n"
            % type(exc).__name__
        )
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
