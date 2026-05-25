#!/usr/bin/env bash
# Stop hook: refuse to end the turn while pending journal entries exist.
# Closes the loop by forcing /journal-add after any test failure captured
# during the session (see post_test_capture.sh / pending_journal.md).
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
PENDING="$ROOT/.claude/pending_journal.md"

if [[ -s "$PENDING" ]]; then
    cat <<EOF >&2
Stop blocked: unresolved error-learning entries in $PENDING.

Run the /journal-add skill to file each entry under one of the fixed domains
(Architecture, SRE, QA, Security, Spec, Process) in docs/00_journal.md, then
truncate $PENDING. This is the closure step of the error-learning loop.
EOF
    exit 2
fi

exit 0
