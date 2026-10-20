"""Tests for install.py and install.sh: installs, rendering, safe overwrite, uninstall."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.py"
INSTALL_SH = ROOT / "install.sh"


class InstallScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saythen-install-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        self.env["SAYTHEN_PYTHON"] = sys.executable
        self.env.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args):
        return subprocess.run(
            [sys.executable, str(INSTALL), *args],
            capture_output=True,
            encoding="utf-8",
            env=self.env,
            timeout=60,
        )

    def assertInstalled(self, target):
        base = target.parent.parent
        self.assertTrue((base / "skills" / "saythen-model" / "SKILL.md").is_file())
        self.assertTrue((base / "agents" / "saythen-drafter.md").is_file())
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertTrue((target / "rules.md").is_file())
        gate = target / "scripts" / "saythen_gate.py"
        self.assertTrue(gate.is_file())
        if os.name != "nt":
            self.assertTrue(os.access(gate, os.X_OK))
        for skill in (target, base / "skills" / "saythen-model"):
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("{{", text)

    def test_user_install_and_uninstall(self):
        target = self.home / ".claude" / "skills" / "saythen"
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalled(target)
        proc = self.run_install()  # reinstall over an existing SayThen install
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_install("--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(target.exists())
        self.assertFalse((self.home / ".claude" / "skills" / "saythen-model").exists())
        self.assertFalse((self.home / ".claude" / "agents" / "saythen-drafter.md").exists())
        self.assertTrue((self.home / ".claude" / "skills").is_dir())

    def test_respects_claude_config_dir(self):
        config = self.tmp / "custom-config"
        self.env["CLAUDE_CONFIG_DIR"] = str(config)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalled(config / "skills" / "saythen")

    def test_project_install_and_uninstall(self):
        project = self.tmp / "my project"
        project.mkdir()
        proc = self.run_install("--project", str(project))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalled(project / ".claude" / "skills" / "saythen")
        proc = self.run_install("--uninstall", "--project", str(project))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((project / ".claude" / "skills" / "saythen").exists())

    def test_refuses_to_replace_unrelated_skill(self):
        target = self.home / ".claude" / "skills" / "saythen"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: something-else\n---\n")
        for args in ((), ("--uninstall",)):
            proc = self.run_install(*args)
            self.assertEqual(proc.returncode, 73)
            self.assertIn("not a SayThen install", proc.stderr)
            self.assertEqual((target / "SKILL.md").read_text(), "---\nname: something-else\n---\n")

    def test_refuses_to_replace_unrelated_agent(self):
        agent = self.home / ".claude" / "agents" / "saythen-drafter.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("---\nname: mine\n---\n")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 73)
        self.assertEqual(agent.read_text(), "---\nname: mine\n---\n")
        self.assertFalse((self.home / ".claude" / "skills" / "saythen").exists())

    def test_python_command_is_filled_in(self):
        self.env["SAYTHEN_PYTHON"] = "%s -X utf8" % sys.executable
        self.assertEqual(self.run_install().returncode, 0)
        text = (self.home / ".claude" / "skills" / "saythen" / "SKILL.md").read_text("utf-8")
        self.assertIn(
            '!`%s -X utf8 "${CLAUDE_SKILL_DIR}/scripts/saythen_gate.py" begin' % sys.executable,
            text,
        )
        line = [x for x in text.splitlines() if x.strip().startswith("command: ")][0]
        self.assertEqual(json.loads(line.split("command: ", 1)[1]), sys.executable)
        tools = "Bash(%s -X utf8" % sys.executable
        self.assertIn(tools, text)
        self.assertEqual("PowerShell(" in text, os.name == "nt")

    def test_missing_python_is_reported(self):
        self.env["SAYTHEN_PYTHON"] = "no-such-python-here"
        self.env["PATH"] = str(self.tmp)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 69)
        self.assertIn("Python 3.9", proc.stderr)

    @unittest.skipIf(os.name == "nt", "install.sh is for macOS and Linux")
    def test_install_sh_runs_install_py(self):
        proc = subprocess.run(
            ["sh", str(INSTALL_SH)], capture_output=True, encoding="utf-8", env=self.env, timeout=60
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalled(self.home / ".claude" / "skills" / "saythen")

    def test_bad_arguments(self):
        self.assertEqual(self.run_install("--bogus").returncode, 64)
        self.assertEqual(self.run_install("--project").returncode, 64)
        self.assertEqual(self.run_install("--project", str(self.tmp / "missing")).returncode, 66)


if __name__ == "__main__":
    unittest.main()
