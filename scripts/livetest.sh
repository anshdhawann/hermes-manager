#!/bin/bash
# Live test of hermes-manager against real Hermes in a throwaway home. Never touches ~/.hermes.
# HERMES_AGENT_DIR: your hermes-agent checkout (default ~/.hermes/hermes-agent)
# HERMES_PYTHON:    its Python (default $HERMES_AGENT_DIR/venv/bin/python)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(dirname "$HERE")"
AGENT="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
PY="${HERMES_PYTHON:-$AGENT/venv/bin/python}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/home" "$T/hermes" "$T/locks" "$T/plugin"
# the plugin as committed, minus the repo-only files
git -C "$REPO" archive HEAD | tar -x -C "$T/plugin"
rm -rf "$T/plugin/tests" "$T/plugin/scripts" "$T/plugin/.gitignore" "$T/plugin/assets" "$T/plugin/LICENSE" "$T/plugin/NOTICE"
cd "$T"
env HOME="$T/home" HERMES_HOME="$T/hermes" HERMES_GATEWAY_LOCK_DIR="$T/locks" \
    PYTHONPATH="$AGENT" "$PY" "$HERE/livetest.py" "$T/plugin" 2>&1 | tee "$HERE/livetest.log"
exit "${PIPESTATUS[0]}"
