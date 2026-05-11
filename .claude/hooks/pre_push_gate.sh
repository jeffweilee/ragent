#!/usr/bin/env bash
# PreToolUse hook on Bash: run the full test suite before any `git push`.
# Reads tool input JSON from stdin; exits 2 to block the push with a reason.
#
# Moved from pre_commit_gate.sh so commits stay fast: format+lint still run at
# commit time, but `make test-gate` (testcontainers MariaDB/ES/Redis/MinIO) runs
# here, immediately before code leaves the machine.
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

# Docker daemon must be live (testcontainers requirement).
if ! docker ps &>/dev/null; then
    block "Docker daemon not running — start it before push (00_rule.md §Docker).
  Agent SOP: run \`sudo dockerd --host=unix:///var/run/docker.sock &>/tmp/dockerd.log &\` then wait up to 30s. Do NOT declare 'docker unavailable' without having run that command."
fi

LOG_DIR="$(mktemp -d -t ragent-prepush-XXXXXX)"
trap 'rm -rf "$LOG_DIR"' EXIT

if ! make test-gate >"$LOG_DIR/test.log" 2>&1; then
    keep="$ROOT/.claude/logs"
    mkdir -p "$keep"
    cp "$LOG_DIR/test.log" "$keep/test.log" 2>/dev/null || true
    block "test failed — see .claude/logs/test.log"
fi

# Pytest must report 0 skipped @pytest.mark.docker tests.
if grep -qE 'docker.*skipped|skipped.*docker' "$LOG_DIR/test.log"; then
    block "@pytest.mark.docker tests were skipped — fix daemon and re-run."
fi

exit 0
