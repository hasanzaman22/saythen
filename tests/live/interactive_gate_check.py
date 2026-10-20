#!/usr/bin/env python3
"""Drive real interactive Claude Code sessions to check the SayThen gate.

This is a manual check, not part of the automated suite. It starts `claude` in
a pseudo-terminal inside a temporary project with SayThen installed, types
/saythen requests, and answers the approval dialog with key presses. Results
are read from the gate's state file and from the files the agent creates.

Requirements: macOS or Linux, an authenticated `claude` CLI, Python 3.9+.
Each scenario runs a short session on your account and takes about a minute.

    python3 tests/live/interactive_gate_check.py
    python3 tests/live/interactive_gate_check.py --model sonnet --only approve cancel

It relies on the terminal UI layout of Claude Code (the workspace trust dialog
and the order of the dialog options), so a UI change can break the driver
without SayThen being broken. Use --keep to inspect the transcript of the pty
in the temporary directory after a failure.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SKILL_DIR = ROOT / "skills" / "saythen"
DOWN, ENTER, ESC = "\x1b[B", "\r", "\x1b"
OPTION_INDEX = {"Approve": 0, "Edit": 1, "Regenerate": 2, "Cancel": 3}
ANSI_RE = re.compile(rb"\x1b\[[0-9;?<>=]*[a-zA-Z~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


class DriverError(Exception):
    """The terminal driver lost track of the UI; not a SayThen failure."""


class Session:
    def __init__(self, workdir: Path, model: str, fixture: str = ""):
        self.project = workdir / "project"
        if fixture:
            shutil.copytree(FIXTURES / fixture, self.project)
        self.state_dir = workdir / "state"
        skills = self.project / ".claude" / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SKILL_DIR, skills / "saythen")
        self.log = open(workdir / "pty.log", "wb")
        self.buffer = b""
        self.host_prompts = 0
        env = dict(os.environ, SAYTHEN_STATE_DIR=str(self.state_dir), TERM="xterm-256color")
        args = ["claude", "--permission-mode", "acceptEdits"]
        if model:
            args += ["--model", model]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            os.chdir(self.project)
            os.execvpe("claude", args, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return
                self.buffer += data
                self.log.write(data)
                self.log.flush()

    def screen(self) -> str:
        return ANSI_RE.sub(b"", self.buffer).decode("utf-8", "replace")

    def keys(self, text: str, pause: float = 0.4) -> None:
        os.write(self.fd, text.encode("utf-8"))
        self.pump(pause)

    def type_line(self, text: str, submit: bool = True) -> None:
        for char in text:
            os.write(self.fd, char.encode("utf-8"))
            self.pump(0.05)
        self.pump(1.0)
        if submit:
            self.keys(ENTER, 2.0)

    def state(self):
        files = list(self.state_dir.glob("*.json")) if self.state_dir.is_dir() else []
        return json.loads(files[0].read_text()) if files else None

    def wait_for(self, predicate, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            self.pump(0.5)
        return predicate()

    def wait_for_dialog(self, round_number: int, timeout: float = 150) -> bool:
        def ready():
            state = self.state() or {}
            return (
                state.get("status") == "awaiting_approval" and state.get("round", 0) >= round_number
            )

        # While Claude drafts, the gate lets only read-only commands reach Claude
        # Code's permission check, so any prompt shown here is for one of them.
        # Answer it the way a user would.
        mark = len(self.buffer)

        def ready_or_prompt():
            nonlocal mark
            if "doyouwanttoproceed?" in self.flat_since(mark):
                self.host_prompts += 1
                self.keys(ENTER, 2.0)
                mark = len(self.buffer)
            return ready()

        found = self.wait_for(ready_or_prompt, timeout)
        self.pump(2.0)  # let the dialog finish rendering
        return found

    def choose(self, label: str) -> None:
        for _ in range(OPTION_INDEX[label]):
            self.keys(DOWN)
        self.keys(ENTER, 2.0)

    def flat_since(self, offset: int = 0) -> str:
        text = ANSI_RE.sub(b"", self.buffer[offset:]).decode("utf-8", "replace")
        return text.lower().replace(" ", "")

    def start(self) -> None:
        self.wait_for(lambda: "trustthisfolder" in self.flat_since() or "❯" in self.screen(), 30)
        self.pump(2.0)
        mark = len(self.buffer)
        if "trustthisfolder" in self.flat_since():
            self.keys(DOWN)
            self.keys(ENTER, 2.0)
        # The empty input box shows a 'Try "..."' placeholder once it is ready.
        if not self.wait_for(lambda: 'try"' in self.flat_since(mark), 45):
            raise DriverError("the input box never became ready")
        self.pump(3.0)

    def saythen(self, request: str) -> None:
        mark = len(self.buffer)
        self.type_line("/saythen", submit=False)
        if "/saythen" not in self.flat_since(mark):
            raise DriverError("typed /saythen did not reach the input box")
        self.type_line(" " + request)
        if not self.wait_for(lambda: self.state() is not None, 8):
            self.keys(ENTER, 2.0)  # the first Enter can pick an autocomplete entry
        if not self.wait_for(lambda: self.state() is not None, 15):
            raise DriverError("/saythen did not start (no gate state)")

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
            time.sleep(1)
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        self.log.close()


def exists(session: Session, name: str) -> bool:
    return (session.project / name).exists()


def wait_for_file(s: Session, name: str, timeout: float = 120) -> bool:
    """Wait for the agent to create a file, answering host permission prompts.

    After approval, Claude Code's own permission prompts still appear (for
    example for some Bash commands). The driver answers "Yes" to them, the way
    the user would, and counts them in Session.host_prompts.
    """
    end = time.time() + timeout
    mark = len(s.buffer)
    while time.time() < end:
        if exists(s, name):
            return True
        s.pump(1.0)
        if "doyouwanttoproceed?" in s.flat_since(mark):
            s.host_prompts += 1
            s.keys(ENTER, 2.0)
            mark = len(s.buffer)
    return exists(s, name)


# Each scenario returns a list of failure messages (empty list means pass).


def scenario_approve(s: Session):
    s.saythen("approved.txt adlı fayl yarat, içində salam yazılsın")
    if not s.wait_for_dialog(1):
        return ["approval dialog never appeared"]
    problems = ["file created before approval"] if exists(s, "approved.txt") else []
    s.choose("Approve")
    if not wait_for_file(s, "approved.txt"):
        problems.append("approved instruction did not run")
    if (s.state() or {}).get("status") != "approved":
        problems.append("state is not approved: %r" % s.state())
    return problems


def scenario_cancel(s: Session):
    s.saythen("create a file named cancelled.txt containing hello")
    if not s.wait_for_dialog(1):
        return ["approval dialog never appeared"]
    s.choose("Cancel")
    s.pump(25)
    problems = []
    if exists(s, "cancelled.txt"):
        problems.append("cancelled request ran")
    if (s.state() or {}).get("status") != "cancelled":
        problems.append("state is not cancelled: %r" % s.state())
    return problems


def scenario_regenerate(s: Session):
    s.saythen("create a file named regen.txt containing hello")
    if not s.wait_for_dialog(1):
        return ["approval dialog never appeared"]
    s.choose("Regenerate")
    problems = []
    if not s.wait_for_dialog(2):
        problems.append("no fresh dialog after Regenerate")
    if exists(s, "regen.txt"):
        problems.append("request ran without fresh approval")
    s.choose("Cancel")
    s.pump(15)
    if exists(s, "regen.txt"):
        problems.append("request ran after Cancel")
    return problems


def scenario_typed_yes(s: Session):
    s.saythen("create a file named typed.txt containing hello")
    if not s.wait_for_dialog(1):
        return ["approval dialog never appeared"]
    s.keys(ESC, 4.0)  # dismiss the dialog without choosing
    s.type_line("yes")
    s.wait_for_dialog(2, timeout=90)
    problems = []
    if exists(s, "typed.txt"):
        problems.append("typed 'yes' executed the request")
    if (s.state() or {}).get("status") == "approved":
        problems.append("typed 'yes' approved the proposal")
    if (s.state() or {}).get("status") == "awaiting_approval":
        s.choose("Cancel")
        s.pump(10)
    return problems


def scenario_edit(s: Session):
    s.saythen("create a file named first.txt containing hello")
    if not s.wait_for_dialog(1):
        return ["approval dialog never appeared"]
    s.choose("Edit")
    s.pump(20)
    s.type_line("use the file name edited.txt instead")
    problems = []
    if not s.wait_for_dialog(2):
        return ["no fresh dialog after the edit"]
    if exists(s, "first.txt") or exists(s, "edited.txt"):
        problems.append("request ran before the edited proposal was approved")
    s.choose("Approve")
    if not wait_for_file(s, "edited.txt"):
        problems.append("edited instruction did not run")
    if exists(s, "first.txt"):
        problems.append("the original, unapproved instruction ran")
    return problems


def scenario_slash_cancel(s: Session):
    s.saythen("create a file named slash.txt containing hello")
    if not s.wait_for_dialog(1):
        return ["approval dialog never appeared"]
    s.keys(ESC, 4.0)
    s.type_line("/saythen cancel")
    s.pump(8)
    problems = []
    if s.state() is not None:
        problems.append("state still present: %r" % s.state())
    if exists(s, "slash.txt"):
        problems.append("request ran")
    return problems


def scenario_project(s: Session):
    s.saythen("users endpointində error handling-i yaxşılaşdır, public API dəyişməsin")
    if not s.wait_for_dialog(1, timeout=240):
        return ["approval dialog never appeared"]
    problems = []
    if "get_user" not in s.flat_since():
        problems.append("proposal does not name get_user from the project")
    original = (FIXTURES / "fastapi_shop" / "app" / "routes" / "users.py").read_text()
    s.choose("Cancel")
    s.pump(15)
    if (s.project / "app" / "routes" / "users.py").read_text() != original:
        problems.append("project file changed without approval")
    return problems


def scenario_clarify(s: Session):
    (s.project / "notes.txt").write_text("keep me\n")
    s.saythen("onu sil")
    s.pump(45)
    problems = []
    if (s.state() or {}).get("status") != "drafting":
        problems.append("expected a clarifying question, state is %r" % s.state())
    s.type_line("notes.txt faylını")
    if not s.wait_for_dialog(1):
        problems.append("no approval dialog after the answer")
    if not exists(s, "notes.txt"):
        problems.append("file deleted before approval")
    if (s.state() or {}).get("status") == "awaiting_approval":
        s.choose("Cancel")
        s.pump(15)
    if not exists(s, "notes.txt"):
        problems.append("file deleted after Cancel")
    return problems


SCENARIOS = {
    "approve": scenario_approve,
    "cancel": scenario_cancel,
    "regenerate": scenario_regenerate,
    "typed-yes": scenario_typed_yes,
    "edit": scenario_edit,
    "slash-cancel": scenario_slash_cancel,
    "project": scenario_project,
    "clarify": scenario_clarify,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default="haiku", help="model for the sessions (default: haiku)")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS), help="scenarios to run")
    parser.add_argument("--keep", action="store_true", help="keep temporary directories")
    args = parser.parse_args()
    if shutil.which("claude") is None:
        print("error: the claude CLI is not on PATH", file=sys.stderr)
        return 2

    failures = 0
    for name in args.only or list(SCENARIOS):
        workdir = Path(tempfile.mkdtemp(prefix="saythen-live-%s-" % name))
        session = Session(workdir, args.model, "fastapi_shop" if name == "project" else "")
        try:
            session.start()
            problems = SCENARIOS[name](session)
        except DriverError as exc:
            problems = ["driver error (not a gate result): %s" % exc]
        except Exception as exc:  # noqa: BLE001 - report and continue
            problems = ["driver error (not a gate result): %r" % exc]
        finally:
            session.close()
        failures += bool(problems)
        note = ""
        if session.host_prompts:
            note = " (answered %d host permission prompt(s))" % session.host_prompts
        print("%s %s%s" % ("FAIL" if problems else "PASS", name, note))
        for problem in problems:
            print("     - %s" % problem)
        if args.keep or problems:
            print("     transcript: %s" % (workdir / "pty.log"))
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
