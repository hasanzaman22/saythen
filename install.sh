#!/bin/sh
# Install or remove SayThen for Claude Code on macOS and Linux.
#
#   ./install.sh                     install for your user (~/.claude)
#   ./install.sh --project DIR       install into DIR/.claude
#   ./install.sh --uninstall         remove the user install
#   ./install.sh --uninstall --project DIR
#
# This runs install.py, which works on every OS. On Windows, run
# `py install.py` instead.

set -eu

command -v python3 >/dev/null 2>&1 || {
    echo "error: python3 is required by the SayThen approval gate" >&2
    exit 69
}
exec python3 "$(dirname "$0")/install.py" "$@"
