#!/usr/bin/env bash
# PostToolUse hook on Bash: when pytest/make test exits non-zero, append a
# stub line to .claude/pending_journal.md so the Stop hook forces /journal-add.
set -uo pipefail

INPUT="$(cat)"
# Emit STATUS on the first line, full CMD on the second, so a CMD with
# embedded spaces is preserved intact (read with two separate calls).
PARSED="$(printf '%s' "$INPUT" | python3 -c '
import sys, json
d = json.load(sys.stdin)
cmd = d.get("tool_input", {}).get("command", "")
tr = d.get("tool_response", {}) or {}
status = tr.get("exit_code", tr.get("returncode", 0))
print(status)
print(cmd.replace("\n", " ")[:500])
' 2>/dev/null || printf '0\n\n')"
STATUS="$(printf '%s' "$PARSED" | sed -n '1p')"
CMD="$(printf '%s' "$PARSED" | sed -n '2p')"

if [[ "$STATUS" == "0" ]]; then exit 0; fi
if ! printf '%s' "$CMD" | grep -qE 'pytest|make[[:space:]]+test'; then exit 0; fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
PENDING="$ROOT/.claude/pending_journal.md"
mkdir -p "$(dirname "$PENDING")"
printf '%s\tcmd=%s\texit=%s\n' "$(date -Iseconds)" "$CMD" "$STATUS" >> "$PENDING"
exit 0
