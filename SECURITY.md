# Security policy

## What SayThen promises

While a SayThen proposal is pending in a Claude Code session, the skill's
`PreToolUse` hook denies every tool call except `Read`, `Glob`, `Grep`,
`ToolSearch`, single read-only shell commands from a fixed list (`ls`, `find`,
`grep`, `rg`, `cat`, `head`, `tail`, `wc`, `tree`, `pwd`, with no shell
operators, substitutions, or write flags), the SayThen approval dialog, and,
when a drafting model is set with `/saythen-model`, a foreground call to the
`saythen-drafter` subagent on that model. That subagent's own tools are
limited to `Read`, `Glob`, and `Grep`.
The session unlocks only when Claude Code's own dialog result shows that the user chose **Approve** for the
exact proposal that the gate checked when the dialog opened, and Claude
receives exactly the text shown as that proposal. The gate fails
closed: if it cannot read its state or its input, it blocks the tool call.

SayThen never grants permissions. It does not return `allow` decisions, so
Claude Code's permission prompts, permission rules, sandbox, and auto mode
checks apply as usual.

## What SayThen does not promise

- It does not control the text Claude writes, only tool calls.
- It does not verify that the translated proposal shown to the user matches
  the English version, or that the localized option labels are accurate.
- After approval, it does not verify that Claude's actions stay within the
  approved instruction.
- It does not protect against your own hooks, settings, or other extensions,
  for example a hook that answers `AskUserQuestion` automatically.
- It does not make prompt injection impossible. It limits what can happen
  before you approve.

See the Limitations section of the [README](README.md) for details.

## Data

SayThen sends nothing over the network. The gate stores one JSON file per
session with a status, timestamps, and SHA-256 hashes. It never stores the
request or the proposal text. Files older than seven days are deleted the next
time `/saythen` runs.

## Reporting a vulnerability

Please report ways to run a tool, approve a proposal, or change an approved
instruction without the user's explicit approval, as well as any data leak.

Use GitHub's private vulnerability reporting for this repository (Security
tab, then "Report a vulnerability"). Do not open a public issue for a security
problem. Include the Claude Code version, the operating system, and the steps
to reproduce.

## Supported versions

Only the latest release receives security fixes.
