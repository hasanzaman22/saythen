---
name: saythen-model
description: Choose the model SayThen uses to draft and translate proposals, without changing the session model.
argument-hint: "[haiku|sonnet|opus|fable|default]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_SKILL_DIR}/../saythen/scripts/saythen_gate.py" model *)
---

# SayThen model

Current setting:
!`python3 "${CLAUDE_SKILL_DIR}/../saythen/scripts/saythen_gate.py" model`

The user typed:

<model_choice>
$ARGUMENTS
</model_choice>

SayThen can draft its proposals (understanding, translating, and improving the
request) on a different model from the one this session uses. The chosen
model runs only inside the read-only `saythen-drafter` subagent. The session
model, the approval dialog, and the approved task are not affected.

- If `<model_choice>` is empty, open a picker, like Claude Code's `/model`:
  call AskUserQuestion with exactly one question and no `answers` or
  `annotations`:
  - `header`: `Model`
  - `question`: "Which model should SayThen use to draft proposals?" in the
    user's language
  - `multiSelect`: false
  - `options`, in this order, each description in the user's language:
    1. `Default`: the session model drafts proposals itself
    2. `Haiku`: fastest and lowest cost
    3. `Sonnet`: balanced speed and quality
    4. `Opus`: most capable
  - Add ` (current)` to the label of the current setting from the line above,
    for example `Haiku (current)`. If the current setting is `fable`, which
    has no option of its own, say so in the question text. The user can type
    `fable` in the dialog's own text field to choose Fable.

  Map the answer to a word: Default to `default`, Haiku to `haiku`, Sonnet to
  `sonnet`, Opus to `opus`, and a typed `fable` to `fable`. If the user
  typed anything else or closed the picker, change nothing and say so.
- If `<model_choice>` is exactly one of `haiku`, `sonnet`, `opus`, `fable`, or
  `default`, use that word and skip the picker.
- If `<model_choice>` is anything else, do not run anything. Tell the user the
  valid choices.

With a word chosen, run this command once, with that word in place of
`<name>`, and nothing else:

    python3 "${CLAUDE_SKILL_DIR}/../saythen/scripts/saythen_gate.py" model <name>

Then tell the user the new setting in one sentence, and that it applies from
their next `/saythen` request and does not change the session model. If the
command is denied because a SayThen proposal is still pending, tell the user
to finish or cancel it (`/saythen cancel`) and run `/saythen-model` again.

If AskUserQuestion is not available (for example in `claude -p` runs), show
the current setting and the choices instead of the picker.

Reply in the language the user writes in.
