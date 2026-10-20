# Contributing to SayThen

Thanks for helping. SayThen is small on purpose: one skill, one rules file,
one gate script, and tests. Changes that keep it that way are the easiest to
review.

## Set up

```sh
git clone https://github.com/hasanzaman22/saythen.git
cd saythen
./install.sh --project /path/to/a/test/project   # try your changes in a scratch project
```

You need Python 3.9 or later. There are no dependencies to install.

## Before you open a pull request

```sh
python3 -m unittest discover -s tests -v
python3 -m pip install ruff && ruff check .        # optional locally, runs in CI
```

Run the model-dependent checks when your change can affect them:

- Changes to `rules.md`: run `python3 tests/eval_scenarios.py` and include the
  summary line in your pull request. Run it with the model you changed the rules
  for, and say which one.
- Changes to `SKILL.md` or `saythen_gate.py`: run
  `python3 tests/live/interactive_gate_check.py` and include the results.

Both scripts use your Claude usage. Say in the pull request if you could not
run them.

## What to change where

- `skills/saythen/rules.md`: how requests are interpreted and written. Keep it
  free of anything specific to Claude Code so that other integrations can reuse
  it. When you add a rule, add a scenario to `tests/scenarios.json` that would
  fail without it.
- `skills/saythen/SKILL.md`: the Claude Code workflow and hook registration.
- `skills/saythen/scripts/saythen_gate.py`: the approval gate. Every change
  needs unit tests in `tests/test_gate.py`. The gate must keep failing closed:
  when it cannot decide, it blocks.

## Guidelines

- Do not add dependencies, services, or network calls.
- Do not log or store the user's request or the proposal text.
- Keep the gate's guarantees and the README's description of them in sync. If
  you change what the gate enforces, update the Limitations section.
- Do not claim support for an agent CLI, platform, or Claude Code version that
  you have not tested.
- Write commit messages that say what changed and why. No particular format
  is required.

## Reporting bugs

Open an issue with the Claude Code version (`claude --version`), your
operating system, the `/saythen` request (remove anything private), what you
expected, and what happened. For security problems, follow
[SECURITY.md](SECURITY.md) instead.
