#!/usr/bin/env bash
# SessionStart hook: surface prior lessons and next TDD task so the agent
# "thinks before working" instead of repeating mistakes captured in 00_journal.md.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
JOURNAL="$ROOT/docs/00_journal.md"
PLAN="$ROOT/docs/00_plan.md"

emit() { printf '%s\n' "$*"; }

emit "## Error-Learning Loop — Session Context"
emit ""

if [[ -f "$JOURNAL" ]]; then
    emit "### 00_journal.md — Domain TOC (read before planning)"
    awk '/^## Domains/{flag=1; next} /^---/{if(flag){exit}} flag' "$JOURNAL" \
        | sed '/^$/d'
    emit ""
    emit "### Recent guidelines (last 8 entries across domains)"
    grep -E '^\| 20[0-9]{2}-' "$JOURNAL" | tail -n 8 || true
    emit ""
fi

if [[ -f "$PLAN" ]]; then
    emit "### 00_plan.md — Next unmarked TDD task"
    grep -nE '^\|.*\| \[ \] \|' "$PLAN" | head -n 1 || emit "(no unmarked tasks)"
    emit ""
fi

emit "Reminder: Red → Green → Refactor. Commit prefix [BEHAVIORAL] or [STRUCTURAL]. Run full pre-commit gate (docker → format → lint → test → bandit)."
