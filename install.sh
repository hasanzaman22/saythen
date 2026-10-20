#!/bin/sh
# Install or remove SayThen for Claude Code.
#
#   ./install.sh                     install for your user (~/.claude)
#   ./install.sh --project DIR       install into DIR/.claude
#   ./install.sh --uninstall         remove the user install
#   ./install.sh --uninstall --project DIR
#
# SayThen is the saythen and saythen-model skills plus the saythen-drafter
# subagent. The script only copies or removes those files. It does not edit
# settings.json or any other Claude Code configuration.

set -eu

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

action=install
project=""
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) action=uninstall ;;
        --project)
            [ $# -ge 2 ] || { echo "error: --project needs a directory" >&2; exit 64; }
            project=$2
            shift
            ;;
        --user) project="" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit 64 ;;
    esac
    shift
done

repo=$(cd "$(dirname "$0")" && pwd)
if [ -n "$project" ]; then
    [ -d "$project" ] || { echo "error: project directory not found: $project" >&2; exit 66; }
    base=$(cd "$project" && pwd)/.claude
else
    base=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
fi
target=$base/skills/saythen
model_target=$base/skills/saythen-model
agent_target=$base/agents/saythen-drafter.md

# Each existing target must be ours before it is replaced or removed.
is_ours() {
    case "$1" in
        "$target") [ -f "$1/SKILL.md" ] && [ -f "$1/scripts/saythen_gate.py" ] \
            && grep -q '^name: saythen$' "$1/SKILL.md" ;;
        "$model_target") [ -f "$1/SKILL.md" ] && grep -q '^name: saythen-model$' "$1/SKILL.md" ;;
        "$agent_target") [ -f "$1" ] && grep -q '^name: saythen-drafter$' "$1" ;;
    esac
}

for path in "$target" "$model_target" "$agent_target"; do
    if [ -e "$path" ] && ! is_ours "$path"; then
        echo "error: $path exists and is not a SayThen install; leaving it untouched." >&2
        exit 73
    fi
done

if [ "$action" = uninstall ]; then
    removed=""
    for path in "$target" "$model_target" "$agent_target"; do
        if [ -e "$path" ]; then
            rm -rf "$path"
            echo "Removed $path"
            removed=yes
        fi
    done
    [ -n "$removed" ] || echo "SayThen is not installed in $base"
    state=${SAYTHEN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/saythen}
    echo "Session state and the model setting, if any, are in $state. Remove that directory to delete them."
    exit 0
fi

command -v python3 >/dev/null 2>&1 || {
    echo "error: python3 is required by the SayThen approval gate" >&2
    exit 69
}
[ -f "$repo/skills/saythen/SKILL.md" ] || { echo "error: run this script from the SayThen repository" >&2; exit 66; }

mkdir -p "$base/skills" "$base/agents"
rm -rf "$target" "$model_target"
cp -R "$repo/skills/saythen" "$target"
cp -R "$repo/skills/saythen-model" "$model_target"
cp "$repo/agents/saythen-drafter.md" "$agent_target"
chmod +x "$target/scripts/saythen_gate.py"
echo "Installed SayThen to $base (skills/saythen, skills/saythen-model, agents/saythen-drafter.md)"
echo "Start Claude Code and type: /saythen <request in any language>"
echo "Optional: /saythen-model haiku drafts proposals on Haiku without changing the session model."
