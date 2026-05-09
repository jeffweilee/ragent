#!/usr/bin/env bash
# Stamp .claude/.pre_commit_approved with current timestamp.
# Wrapped as a script so it can be allowlisted as a single Bash invocation,
# avoiding shell-redirect quirks in Claude Code's permission matcher.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
date > "$ROOT/.claude/.pre_commit_approved"
