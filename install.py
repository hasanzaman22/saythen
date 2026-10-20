#!/usr/bin/env python3
"""Install or remove SayThen for Claude Code on macOS, Linux, or Windows.

    python3 install.py                     install for your user (~/.claude)
    python3 install.py --project DIR       install into DIR/.claude
    python3 install.py --uninstall         remove the user install
    python3 install.py --uninstall --project DIR

On Windows, run it with `py install.py` or `python install.py`.

SayThen is the saythen and saythen-model skills plus the saythen-drafter
subagent. The installer copies them and fills in the Python command and the
hook commands for this machine. It does not edit settings.json or any other
Claude Code configuration.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
GATE = "scripts/saythen_gate.py"
VERSION_CHECK = "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"

# Hook events, the matcher each one uses, and whether a hook failure blocks.
HOOK_EVENTS = (
    ("PreToolUse", "*", "pre-tool-use", True),
    ("PostToolUse", "AskUserQuestion", "post-tool-use", False),
    ("UserPromptSubmit", None, "user-prompt-submit", False),
)


class InstallError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def find_python() -> List[str]:
    """Return the command that starts Python 3.9+ from a shell, as a list of words.

    It must be a bare command name rather than a path, so that the same text
    runs in bash and in PowerShell. Set SAYTHEN_PYTHON to override it.
    """
    override = os.environ.get("SAYTHEN_PYTHON", "").split()
    candidates = [override] if override else []
    if WINDOWS:
        candidates += [["py", "-3"], ["python"], ["python3"]]
    else:
        candidates += [["python3"], ["python"]]
    for candidate in candidates:
        try:
            result = subprocess.run(
                candidate + ["-c", VERSION_CHECK], capture_output=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    raise InstallError(
        "Python 3.9 or later must be on PATH as %s"
        % ("py, python, or python3" if WINDOWS else "python3"),
        69,
    )


def yaml_string(value: str) -> str:
    """A double-quoted YAML string. JSON escaping is valid YAML."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def render_hooks(python: List[str], gate: str) -> str:
    lines = ["hooks:"]
    for event, matcher, subcommand, blocking in HOOK_EVENTS:
        lines.append("  %s:" % event)
        if matcher:
            lines.append("    - matcher: %s" % yaml_string(matcher))
            lines.append("      hooks:")
        else:
            lines.append("    - hooks:")
        args = python[1:] + [gate, subcommand]
        lines.append("        - type: command")
        lines.append("          command: %s" % yaml_string(python[0]))
        lines.append("          args: [%s]" % ", ".join(yaml_string(a) for a in args))
        if blocking:
            lines.append("          onFailure: block")
    return "\n".join(lines)


def render_allowed_tools(python: str, commands: List[str]) -> str:
    tools = ["Bash", "PowerShell"] if WINDOWS else ["Bash"]
    return " ".join("%s(%s %s)" % (tool, python, command) for tool in tools for command in commands)


def render(path: Path, python: List[str], gate: str) -> None:
    text = path.read_text(encoding="utf-8")
    command = " ".join(python)
    allowed = re.search(r"\{\{ALLOWED_TOOLS:(.*?)\}\}", text)
    if allowed:
        commands = [c.strip() for c in allowed.group(1).split("|")]
        text = text.replace(allowed.group(0), render_allowed_tools(command, commands))
    text = text.replace("{{HOOKS}}", render_hooks(python, gate))
    text = text.replace("{{PYTHON}}", command)
    path.write_text(text, encoding="utf-8", newline="\n")


def name_in(path: Path, name: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^name: %s$" % re.escape(name), text, re.MULTILINE) is not None


def targets(base: Path) -> dict:
    return {
        "skill": base / "skills" / "saythen",
        "model": base / "skills" / "saythen-model",
        "agent": base / "agents" / "saythen-drafter.md",
    }


def is_ours(kind: str, path: Path) -> bool:
    if kind == "skill":
        return (path / GATE).is_file() and name_in(path / "SKILL.md", "saythen")
    if kind == "model":
        return name_in(path / "SKILL.md", "saythen-model")
    return name_in(path, "saythen-drafter")


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def state_location() -> str:
    override = os.environ.get("SAYTHEN_STATE_DIR")
    if override:
        return override
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        return os.path.join(base, "saythen")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "saythen")


def run(argv: List[str]) -> int:
    action, project = "install", None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--uninstall":
            action = "uninstall"
        elif arg == "--project":
            if not args:
                raise InstallError("--project needs a directory", 64)
            project = args.pop(0)
        elif arg == "--user":
            project = None
        elif arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        else:
            raise InstallError("unknown option: %s" % arg, 64)

    if project is not None:
        if not Path(project).is_dir():
            raise InstallError("project directory not found: %s" % project, 66)
        base = Path(project).resolve() / ".claude"
        gate = "${CLAUDE_PROJECT_DIR}/.claude/skills/saythen/" + GATE
    else:
        config = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(config) if config else Path.home() / ".claude"
        base = base.resolve()
        gate = (base / "skills" / "saythen" / GATE).as_posix()
    paths = targets(base)

    for kind, path in paths.items():
        if path.exists() and not is_ours(kind, path):
            raise InstallError(
                "%s exists and is not a SayThen install; leaving it untouched" % path, 73
            )

    if action == "uninstall":
        removed = False
        for path in paths.values():
            if path.exists():
                remove(path)
                print("Removed %s" % path)
                removed = True
        if not removed:
            print("SayThen is not installed in %s" % base)
        print(
            "Session state and the model setting, if any, are in %s. Remove that "
            "directory to delete them." % state_location()
        )
        return 0

    if not (REPO / "skills" / "saythen" / "SKILL.md").is_file():
        raise InstallError("run this script from the SayThen repository", 66)
    python = find_python()
    for path in paths.values():
        if path.exists():
            remove(path)
    paths["agent"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / "skills" / "saythen", paths["skill"])
    shutil.copytree(REPO / "skills" / "saythen-model", paths["model"])
    shutil.copyfile(REPO / "agents" / "saythen-drafter.md", paths["agent"])
    for skill in (paths["skill"], paths["model"]):
        render(skill / "SKILL.md", python, gate)
    if not WINDOWS:
        os.chmod(paths["skill"] / GATE, 0o755)
    print(
        "Installed SayThen to %s (skills/saythen, skills/saythen-model, agents/saythen-drafter.md)"
        % base
    )
    print("Python command: %s" % " ".join(python))
    print("Start Claude Code and type: /saythen <request in any language>")
    print(
        "Optional: /saythen-model haiku drafts proposals on Haiku without changing "
        "the session model."
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except InstallError as exc:
        sys.stderr.write("error: %s\n" % exc)
        if exc.code == 64:
            sys.stderr.write(__doc__.strip() + "\n")
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
