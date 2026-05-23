#!/usr/bin/env bash
# PreToolUse hook on Bash: run unit tests before any `git push`.
# Reads tool input JSON from stdin; exits 2 to block the push with a reason.
#
# Default fast path: unit tests only — no docker, no testcontainers.
# Integration + e2e are opt-in via `RAGENT_PREPUSH_FULL=1 git push ...`, which
# restores the original behaviour (docker daemon check + `make test-gate`,
# which itself excludes tests/e2e; set RAGENT_PREPUSH_FULL=e2e to also include
# tests/e2e). Markdown-only diffs short-circuit before any of this.
set -uo pipefail

INPUT="$(cat)"
CMD="$(printf '%s' "$INPUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null || true)"

# Only intercept git push invocations.
if ! printf '%s' "$CMD" | grep -qE '(^|[[:space:];&|])git[[:space:]]+push([[:space:]]|$)'; then
    exit 0
fi

block() {
    printf 'Pre-push gate FAILED: %s\n' "$1" >&2
    exit 2
}

# Reject hook bypasses on push as well.
if printf '%s' "$CMD" | grep -qE '(^|[[:space:]])--no-verify([[:space:]]|$)'; then
    block "--no-verify is forbidden by 00_rule.md."
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
cd "$ROOT"

# High-risk full-review gate — if a pre-commit risk classification wrote
# .pending_full_review, require /simplify --mode full + /review --mode full
# before push is allowed.
#
# Stamp freshness: 30 minutes AND the stamp must be newer than the pending
# marker itself — this prevents a full-review run that predates the high-risk
# commit from satisfying the gate (fix for review finding P1: timing).
#
# Marker consumption: deferred until ALL pre-push checks pass via an EXIT
# trap. If tests fail and the push is blocked the marker remains, so the
# next retry still requires full review (fix for review finding P1: consume).
PENDING="$ROOT/.claude/.pending_full_review"
_CONSUME_PENDING=0
_consume_on_success() {
    local code=$?
    if [[ $_CONSUME_PENDING -eq 1 && $code -eq 0 ]]; then
        rm -f "$PENDING"
        printf 'Pre-push gate: .pending_full_review consumed — full review satisfied.\n' >&2
    fi
}
if [[ -s "$PENDING" ]]; then
    FULL_FRESHNESS=1800  # 30 minutes
    FULL_NOW=$(date +%s)
    FULL_CUTOFF=$(( FULL_NOW - FULL_FRESHNESS ))
    # The stamp must also be newer than the pending marker (commit time)
    # so a pre-commit full review cannot satisfy a post-commit push gate.
    PENDING_TS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ts",0))' "$PENDING" 2>/dev/null || echo 0)
    AUDIT="$ROOT/.claude/.stamp_audit.log"
    FULL_HITS=$(python3 - "${AUDIT:-/dev/null}" "$FULL_CUTOFF" "$PENDING_TS" <<'PY' 2>/dev/null
import json, sys
log, cutoff, pending_ts = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
hits = {"simplify": "no", "review": "no"}
try:
    with open(log) as f:
        for line in f:
            try:
                row = json.loads(line)
                ts = int(row.get("ts", 0))
                # Must be within freshness window AND after the pending marker
                if ts >= cutoff and ts > pending_ts:
                    by = row.get("by", "")
                    if by in ("simplify:full", "simplify"):
                        hits["simplify"] = "yes"
                    elif by in ("review:full", "review"):
                        hits["review"] = "yes"
            except Exception:
                continue
except FileNotFoundError:
    pass
print(hits["simplify"], hits["review"])
PY
) || FULL_HITS="no no"
    read -r SIM_FULL REV_FULL <<<"$FULL_HITS"
    if [[ "$SIM_FULL" != yes || "$REV_FULL" != yes ]]; then
        REASON=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("reason","?"))' "$PENDING" 2>/dev/null || echo "?")
        block "high-risk full-review gate: .pending_full_review exists (reason: ${REASON}).
  Before pushing, run BOTH skills AFTER your last high-risk commit:
    /simplify --mode full
    /review --mode full
  (stamps must be within 30 min and newer than the commit). Got simplify:full=${SIM_FULL} review:full=${REV_FULL}."
    fi
    # Mark for consumption — actual rm happens in the EXIT trap after all
    # remaining pre-push checks (markdown/tests) also pass.
    _CONSUME_PENDING=1
    printf 'Pre-push gate: full-review requirement satisfied — proceeding to test gate.\n' >&2
fi

# Determine the diff range being pushed. Prefer the upstream tracking ref;
# fall back to origin/<current-branch>, then origin/HEAD. If we can resolve
# a base AND every changed path is a markdown file, skip docker+test — the
# gate exists to catch code regressions, and .md-only pushes can't trip them.
BASE=""
if UP="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"; then
    BASE="$UP"
elif BR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" && git rev-parse --verify "origin/$BR" &>/dev/null; then
    BASE="origin/$BR"
elif git rev-parse --verify origin/HEAD &>/dev/null; then
    BASE="origin/HEAD"
fi

if [[ -n "$BASE" ]]; then
    CHANGED="$(git diff --name-only "$BASE"...HEAD 2>/dev/null || true)"
    if [[ -n "$CHANGED" ]] && ! printf '%s\n' "$CHANGED" | grep -qvE '\.md$'; then
        printf 'Pre-push gate: markdown-only diff vs %s — skipping docker + test-gate.\n' "$BASE" >&2
        exit 0
    fi
fi

LOG_DIR="$(mktemp -d -t ragent-prepush-XXXXXX)"
# Combine cleanup: remove temp dir AND conditionally consume pending marker.
trap '_consume_on_success; rm -rf "$LOG_DIR"' EXIT

FULL="${RAGENT_PREPUSH_FULL:-}"

if [[ -z "$FULL" ]]; then
    # Fast path: unit tests only. No docker, no testcontainers.
    if ! uv run pytest tests/unit >"$LOG_DIR/test.log" 2>&1; then
        keep="$ROOT/.claude/logs"
        mkdir -p "$keep"
        cp "$LOG_DIR/test.log" "$keep/test.log" 2>/dev/null || true
        block "unit tests failed — see .claude/logs/test.log
  Integration + e2e are opt-in: re-run with \`RAGENT_PREPUSH_FULL=1 git push ...\`
  (set RAGENT_PREPUSH_FULL=e2e to also include tests/e2e)."
    fi
    exit 0
fi

# Opt-in full path: requires docker daemon for testcontainers.
if ! docker ps &>/dev/null; then
    block "Docker daemon not running — start it before push (00_rule.md §Docker).
  Agent SOP: run \`sudo dockerd --host=unix:///var/run/docker.sock &>/tmp/dockerd.log &\` then wait up to 30s. Do NOT declare 'docker unavailable' without having run that command."
fi

if [[ "$FULL" == "e2e" ]]; then
    TARGET="test"
else
    TARGET="test-gate"
fi

if ! make "$TARGET" >"$LOG_DIR/test.log" 2>&1; then
    keep="$ROOT/.claude/logs"
    mkdir -p "$keep"
    cp "$LOG_DIR/test.log" "$keep/test.log" 2>/dev/null || true
    block "$TARGET failed — see .claude/logs/test.log"
fi

# Pytest must report 0 skipped @pytest.mark.docker tests when docker path runs.
if grep -qE 'docker.*skipped|skipped.*docker' "$LOG_DIR/test.log"; then
    block "@pytest.mark.docker tests were skipped — fix daemon and re-run."
fi

exit 0
