#!/usr/bin/env bash
# PostToolUse hook on Bash: when pytest/make test exits non-zero, append a
# stub line to .claude/pending_journal.md so the Stop hook forces /journal-add.
set -uo pipefail

INPUT="$(cat)"
read -r CMD STATUS < <(printf '%s' "$INPUT" | python3 -c '
import sys, json
d = json.load(sys.stdin)
cmd = d.get("tool_input", {}).get("command", "")
status = d.get("tool_response", {}).get("exit_code", d.get("tool_response", {}).get("returncode", 0))
print(cmd.replace("\n"," ")[:200], status)
' 2>/dev/null || echo " 0")

if [[ "$STATUS" == "0" ]]; then exit 0; fi
if ! printf '%s' "$CMD" | grep -qE 'pytest|make[[:space:]]+test'; then exit 0; fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
PENDING="$ROOT/.claude/pending_journal.md"
mkdir -p "$(dirname "$PENDING")"
printf '%s\tcmd=%s\texit=%s\n' "$(date -Iseconds)" "$CMD" "$STATUS" >> "$PENDING"
exit 0
