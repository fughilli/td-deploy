#!/usr/bin/env bash
# (Re)start the container-side live MJPEG stream against a project.
# Container-render + Mac-view model: run this in the container; view on the Mac at
#   http://toxc.$CLAUDE_SERVICE_INSTANCE.claude.localhost/
# Usage: tools/stream.sh [project.tox|.toe|.json] [extra cli args...]
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
TOX="${1:-/workspace/ascii_project.toe}"
shift || true
pkill -f -- '--stream' 2>/dev/null || true
sleep 1
nix/dev.sh python3 -m cli "$TOX" --stream --port 8788 "$@" >/tmp/toxc_stream.log 2>&1 &
sleep 20
if curl -s -m5 http://localhost:8788/stats >/dev/null 2>&1; then
  echo "stream up for $TOX -> port 8788 ($(curl -s http://localhost:8788/stats))"
else
  echo "stream FAILED to start; see /tmp/toxc_stream.log"; tail -20 /tmp/toxc_stream.log
fi
