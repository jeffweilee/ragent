#!/usr/bin/env bash
# Stamps .claude/.pre_commit_approved + appends to .claude/.stamp_audit.log,
# bound to the current staged diff. Called ONLY at the tail of /simplify or
# /review skill execution.
#
# Marker schema (validated by .claude/hooks/pre_commit_gate.sh):
#   {"diff_sha": "<sha256 of git diff --cached>", "ts": <epoch>, "by": "<skill-name>"}
#
# Audit log (append-only JSON-lines; gate cross-checks both skills ran):
#   {"ts":<epoch>,"by":"<skill-name>","diff_sha":"<sha>"}
#
# Hardening (added 2026-05-09 after Process row "Mandatory-step honesty
# (recurrence)" — agent bypassed /simplify+/review for 7 commits by calling
# this script directly without invoking the Skill tool):
#   - Refuses to stamp without RAGENT_SKILL_INVOCATION_TOKEN env var (set
#     ONLY from inside a /simplify or /review skill body).
#   - Validates skill name argument (simplify|review only).
#   - Audit log is append-only — gate verifies BOTH skills' entries exist
#     for the current diff_sha (a forged single stamp now fails the gate).
#
# Usage (from inside a Skill body only):
#   RAGENT_SKILL_INVOCATION_TOKEN=1 bash .claude/hooks/stamp_pre_commit_approved.sh review
set -euo pipefail

SKILL="${1:-}"
case "$SKILL" in
    simplify | review) ;;
    *)
        printf 'stamp script: invalid skill name %q (expected simplify|review)\n' "$SKILL" >&2
        exit 2 ;;
esac

# Self-attestation gate. The Skill tool's execution wrapper is expected
# to set this env var; agent calling the script directly without first
# running /simplify or /review is a process violation. The token's
# value is informational (any non-empty string accepted) — purpose is
# explicit declaration of intent, not cryptographic proof.
if [[ -z "${RAGENT_SKILL_INVOCATION_TOKEN:-}" ]]; then
    cat >&2 <<'EOF'
stamp script: RAGENT_SKILL_INVOCATION_TOKEN env var required.

  This token is set ONLY from inside a /simplify or /review skill
  invocation. Calling this script with the env var unset means the
  caller is bypassing the quality gates — a process violation per
  docs/00_journal.md Process row 2026-05-09 "Mandatory-step honesty
  (recurrence)".

  Correct usage (set inside a /simplify or /review skill body only):
    RAGENT_SKILL_INVOCATION_TOKEN=1 bash .claude/hooks/stamp_pre_commit_approved.sh review
EOF
    exit 2
fi

ROOT="$(git rev-parse --show-toplevel)"
SHA="$(git diff --cached | sha256sum | cut -d' ' -f1)"
TS="$(date +%s)"

mkdir -p "$ROOT/.claude"

# Marker file first (last write wins) — gate validates schema + freshness.
# If this write fails, set -e aborts before the audit-log append, avoiding
# a phantom audit entry the gate can't reconcile against any marker.
printf '{"diff_sha": "%s", "ts": %s, "by": "%s"}\n' "$SHA" "$TS" "$SKILL" \
    > "$ROOT/.claude/.pre_commit_approved"

# Append-only audit log — gate cross-checks both /simplify AND /review
# entries exist for the current diff_sha within the freshness window. The
# audit log is the actual unforgeability layer: a single bypassing caller
# can stamp the marker but cannot retroactively produce the other skill's
# entry, so the gate's two-entry requirement still rejects.
printf '{"ts":%s,"by":"%s","diff_sha":"%s"}\n' "$TS" "$SKILL" "$SHA" \
    >> "$ROOT/.claude/.stamp_audit.log"

printf 'pre-commit marker stamped by %s (diff_sha=%s..., ts=%s)\n' \
    "$SKILL" "${SHA:0:12}" "$TS" >&2
