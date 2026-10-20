# Changelog

## 0.1.0 (unreleased)

First release.

- `/saythen` skill for Claude Code that turns a request in any language into a
  clear English instruction, sized to the task (conversation, moderate task,
  or complex task).
- Approval gate enforced by skill hooks: while a proposal is pending, every
  tool call except `Read`, `Glob`, `Grep`, `ToolSearch`, single read-only
  shell commands from a fixed list, and the approval dialog is denied. Only choosing **Approve** in the native dialog that shows
  the proposal unlocks the session. Edit, Regenerate, typed replies, and
  Cancel never approve.
- One approval dialog in the user's language. For a user who did not write
  in English, the approval question offers "approve and send English" and
  "approve and send your language", after a reply-language question; both
  questions can cancel. Long versions get Read tabs (the user's language,
  then English) with up to four pages each, because the preview box does not
  scroll. The gate reads options by position, so labels can be localized.
- The skill prompt, rules, and drafter prompt are kept short, and the
  proposal is not repeated in the chat, to save tokens.
- `/saythen cancel` discards a pending proposal.
- `/saythen-model` picks a separate model (`haiku`, `sonnet`, `opus`, `fable`)
  for drafting and translating proposals. Drafting then runs in the read-only
  `saythen-drafter` subagent; the session model is not changed.
- Portable transformation rules in `skills/saythen/rules.md`. Tasks in a
  professional field outside software get a one-line role, the field's normal
  standard, and an editable Assumptions list. Level follows the deliverable,
  so a short request for a program is treated as a multi-part task.
- `install.sh` for user and project installs, and for uninstalling.

### Verified for this release

Environment: Claude Code 2.1.303, macOS 26.5, Python 3.14 and 3.9.

- Unit tests (`python3 -m unittest discover -s tests`): 48 tests pass on
  Python 3.14 and 3.9.
- Scenario evaluation (`tests/eval_scenarios.py`, 58 scenarios covering
  conversation, facts, health questions, translation, coding, git and
  destructive commands, pasted errors and long logs, attachments,
  mixed-language and multi-task requests, professional fields such as
  architecture, law, medicine, teaching, finance, marketing, design, and
  travel, explicit output languages, prompt injection, and two requests that
  need a fixture project): Sonnet 58/58, Opus 57/58 (a poem proposal 12 words
  over a length limit that was then raised), Haiku 47/58. Haiku added
  unrequested details and assumptions, wrote one email in English instead of
  Azerbaijani, and asked one clarifying question in the wrong language.
  Sonnet or Opus is recommended. Each model ran once on the final rules.
- Non-interactive checks with `claude -p`: the gate state is created before
  the model runs; a model told to ignore SayThen had its `Bash` and `Write`
  calls denied by the hook while `Read` worked; `` !`command` `` text inside a
  `/saythen` request was not executed; `AskUserQuestion` is unavailable in
  `-p` runs, so nothing executed.
- Interactive checks (`tests/live/interactive_gate_check.py`, Sonnet, one full
  run): approve, cancel, regenerate, typed-yes, edit, slash-cancel, project,
  and clarify all passed. Only approved proposals changed files. In the
  project scenario, the proposal named `get_user` after Claude read the
  fixture with a read-only command, and the file was unchanged after Cancel.
  In the clarify scenario, a clarifying question kept the session locked and
  the file the request referred to survived until Cancel. Read-only commands
  during drafting went through Claude Code's normal permission prompt. An
  earlier run also showed Claude Code's own permission prompt for a Bash
  command after approval.
- `/saythen` appears in the session's slash commands after
  `./install.sh --project`. The user-level install path was tested with a
  temporary `HOME` only.
