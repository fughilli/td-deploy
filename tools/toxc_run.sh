#!/usr/bin/env bash
# `bazel run //:toxc -- <input> [opts]` entrypoint.
# Runs the toxc pipeline from the source tree inside the pinned Nix env
# (python + Mesa GL). Bazel sets BUILD_WORKSPACE_DIRECTORY to the repo root and
# BUILD_WORKING_DIRECTORY to wherever the user invoked `bazel run`.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run this via 'bazel run //:toxc -- ...'}"
exec nix/dev.sh python3 -m cli --cwd "${BUILD_WORKING_DIRECTORY:-$PWD}" "$@"
