#!/usr/bin/env bash
# PreToolUse hook on Bash: enforce 00_rule.md §Command before any `git commit`.
# Reads tool input JSON from stdin; exits 2 to block the commit with a reason.
set -uo pipefail

INPUT="$(cat)"
CMD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null || true)"

# Only intercept git commit invocations.
if ! printf '%s' "$CMD" | grep -qE '(^|[[:space:];&|])git[[:space:]]+commit([[:space:]]|$)'; then
    exit 0
fi

block() {
    # exit 2 → Claude Code surfaces stderr to the model as a blocking reason.
    printf 'Pre-commit gate FAILED: %s\n' "$1" >&2
    exit 2
}

# 1. Commit message must carry [BEHAVIORAL] or [STRUCTURAL] prefix.
#    Heuristic: the prefix tag must appear somewhere in the commit invocation
#    (covers both `-m "[STRUCTURAL] ..."` and heredoc `-m "$(cat <<'EOF' ...`).
if printf '%s' "$CMD" | grep -qE -- '-m[[:space:]]'; then
    if ! printf '%s' "$CMD" | grep -qE '\[(BEHAVIORAL|STRUCTURAL)\]'; then
        block "commit message missing [BEHAVIORAL] or [STRUCTURAL] prefix (Tidy First rule)."
    fi
fi

# 2. Reject explicit hook/test bypasses (only when used as flags, not when
#    appearing inside a commit message body).
GIT_FLAGS="$(printf '%s' "$CMD" | sed -E 's/-m[[:space:]]+("([^"]|\\")*"|'\''([^'\'']|\\'\'')*'\''|\$\([^)]*\))//g')"
if printf '%s' "$GIT_FLAGS" | grep -qE '(^|[[:space:]])(--no-verify|--no-gpg-sign)([[:space:]]|$)'; then
    block "--no-verify / --no-gpg-sign are forbidden by 00_rule.md."
fi
# Note: pytest-skip enforcement (`-m "not docker"`, `--deselect`) is verified
# below by parsing the actual `make test` output, not by string-matching the
# git-commit invocation (which has no pytest semantics).

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
cd "$ROOT"

# 3. Scope: the heavy quality gate runs when staged changes touch code
#    (src/, tests/, pyproject.toml) OR the contract docs (spec / plan), since
#    spec drift can change behaviour as much as code (e.g. §5.2 mapping JSON,
#    /readyz contract, env-var inventory). Pure .claude / journal / README
#    commits skip the gate but still pass the prefix and bypass-flag checks
#    above. (Strengthened 2026-05-09 after Gap D: docs-only commits previously
#    bypassed /simplify + /review entirely — see docs/00_journal.md Process.)
STAGED="$(git diff --cached --name-only 2>/dev/null || true)"
TRIGGERS_GATE=0
if printf '%s\n' "$STAGED" | grep -qE '^(src/|tests/|pyproject\.toml$|docs/00_(spec|plan)\.md$)'; then
    TRIGGERS_GATE=1
fi
# Code-only checks (docker test gate, format, lint) only fire on real code
# diffs; spec/plan-only commits get the simplify+review marker check.
CODE_GATE=0
if printf '%s\n' "$STAGED" | grep -qE '^(src/|tests/|pyproject\.toml$)'; then
    CODE_GATE=1
fi

# 4. Docs reminder — non-blocking. Whenever code changes, surface a nudge to
#    update plan.md / spec.md / README.md if the change warrants it.
if [[ $TRIGGERS_GATE -eq 1 ]]; then
    DOC_HITS="$(printf '%s\n' "$STAGED" | grep -E '^(docs/00_(plan|spec|journal)\.md|README\.md)$' || true)"
    if [[ -z "$DOC_HITS" ]]; then
        printf 'Pre-commit reminder: src/tests/pyproject changes staged but no docs/00_plan.md, docs/00_spec.md, docs/00_journal.md, or README.md update. Update them now if this change adds/alters behavior, contracts, env vars, or lessons learned.\n' >&2
    fi
fi

if [[ $TRIGGERS_GATE -eq 0 ]]; then
    exit 0
fi

# 5. Review & Simplify gate — both AI quality steps must have run and stamped
#    .claude/.pre_commit_approved against the *current* staged diff.
#    Marker schema (strengthened 2026-05-09 — see docs/00_journal.md Process
#    row, Gaps B & C): JSON `{"diff_sha": "<sha>", "ts": <epoch>}` where
#    `<sha>` = sha256 of `git diff --cached` output AT STAMP TIME. The gate
#    recomputes the staged diff sha now and rejects mismatch — so adding
#    new staged hunks after stamping invalidates the marker. Manual `date >`
#    stamping by the agent no longer satisfies the gate (the file would be
#    plain text, not JSON, and the diff_sha extraction below fails).
APPROVAL="$ROOT/.claude/.pre_commit_approved"
FRESHNESS=2700  # 45 minutes
if [[ ! -s "$APPROVAL" ]]; then
    block "pre-commit review gate: .claude/.pre_commit_approved missing or empty.
  Required steps before committing (see 00_rule.md §Python > 'Agent quality-gate honesty rules'):
    1. /simplify  — AI code quality review; stage any resulting fixes
    2. /review    — verify plan compliance, spec alignment, test coverage, code quality
    3. The second skill to finish writes JSON {\"diff_sha\": <sha256 git diff --cached>, \"ts\": <epoch>}
       to .claude/.pre_commit_approved. Manual 'date >' stamping is forbidden."
fi
MARKER_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("diff_sha",""))' "$APPROVAL" 2>/dev/null || true)
MARKER_TS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ts",""))' "$APPROVAL" 2>/dev/null || true)
if [[ -z "$MARKER_SHA" || ! "$MARKER_TS" =~ ^[0-9]+$ ]]; then
    block "pre-commit review gate: .claude/.pre_commit_approved is not a valid skill-emitted JSON marker (missing diff_sha or ts).
  Manual 'date >' stamping is forbidden — the marker MUST be JSON written by /simplify or /review at end-of-skill.
  Re-run /simplify and /review."
fi
APPROVAL_AGE=$(( $(date +%s) - MARKER_TS ))
if [[ $APPROVAL_AGE -gt $FRESHNESS ]]; then
    block "pre-commit review gate: marker is stale (${APPROVAL_AGE}s old, max ${FRESHNESS}s). Re-run /simplify and /review."
fi
CURRENT_SHA="$(git diff --cached | sha256sum | cut -d' ' -f1)"
if [[ "$MARKER_SHA" != "$CURRENT_SHA" ]]; then
    block "pre-commit review gate: staged diff changed since marker was stamped (marker_sha=${MARKER_SHA:0:12}…, current_sha=${CURRENT_SHA:0:12}…).
  /simplify and /review reviewed a different diff. Re-run both against the current staged set."
fi
# Consume the marker — every commit requires a fresh /simplify + /review cycle.
rm -f "$APPROVAL"

if [[ $CODE_GATE -eq 0 ]]; then
    # Spec/plan-only commit: the marker check above is sufficient — skip
    # docker / test / format / lint, since no executable code changed.
    exit 0
fi

# 6. Docker daemon must be live (testcontainers requirement).
if ! docker ps &>/dev/null; then
    block "Docker daemon not running — start it before commit (00_rule.md §Docker).
  Agent SOP: run \`sudo dockerd --host=unix:///var/run/docker.sock &>/tmp/dockerd.log &\` then wait up to 30s. Do NOT declare 'docker unavailable' without having run that command."
fi

# 7. Quality gate. Logs go to a per-run private dir to avoid /tmp symlink/
#    permission races with concurrent commits.
LOG_DIR="$(mktemp -d -t ragent-precommit-XXXXXX)"
trap 'rm -rf "$LOG_DIR"' EXIT
run_step() {
    local label="$1"; shift
    if ! "$@" >"$LOG_DIR/${label}.log" 2>&1; then
        # Preserve the failing log for the operator before trap cleans up.
        local keep="$ROOT/.claude/logs"
        mkdir -p "$keep"
        cp "$LOG_DIR/${label}.log" "$keep/${label}.log" 2>/dev/null || true
        block "$label failed — see .claude/logs/${label}.log"
    fi
}

run_step format make format
run_step lint   make lint
run_step test   make test-gate

# 8. Pytest must report 0 skipped @pytest.mark.docker tests.
if grep -qE 'docker.*skipped|skipped.*docker' "$LOG_DIR/test.log"; then
    block "@pytest.mark.docker tests were skipped — fix daemon and re-run."
fi

exit 0
