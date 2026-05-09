#!/usr/bin/env bash
# Stamps .claude/.pre_commit_approved with a JSON marker bound to the current
# staged diff. Called by /simplify and /review skills at the END of their
# review pass — never invoked directly by the agent (manual stamping is a
# process violation per 00_rule.md §Python > 'Agent quality-gate honesty
# rules' and docs/00_journal.md 2026-05-09 Process row).
#
# Schema: {"diff_sha": "<sha256 of git diff --cached>", "ts": <epoch>, "by": "<skill-name>"}
#
# Usage: bash .claude/hooks/stamp_pre_commit_approved.sh <skill-name>
#   <skill-name> ∈ {simplify, review} — recorded for audit only.
set -euo pipefail

SKILL="${1:-unknown}"
ROOT="$(git rev-parse --show-toplevel)"
SHA="$(git diff --cached | sha256sum | cut -d' ' -f1)"
TS="$(date +%s)"

printf '{"diff_sha": "%s", "ts": %s, "by": "%s"}\n' "$SHA" "$TS" "$SKILL" \
    > "$ROOT/.claude/.pre_commit_approved"

printf 'pre-commit marker stamped by %s (diff_sha=%s..., ts=%s)\n' \
    "$SKILL" "${SHA:0:12}" "$TS" >&2
