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

# 4. Docs gate — mandatory for [BEHAVIORAL] commits (00_rule.md: "Always check
#    and update 00_spec.md, 00_plan.md, 00_journal.md before and after delivery").
#    Hard-blocks when a [BEHAVIORAL] commit touches code but stages none of the
#    three mandatory docs. [STRUCTURAL] commits get a non-blocking reminder only.
if [[ $TRIGGERS_GATE -eq 1 ]]; then
    DOC_HITS="$(printf '%s\n' "$STAGED" | grep -E '^docs/00_(plan|spec|journal)\.md$' || true)"
    IS_BEHAVIORAL=0
    if printf '%s' "$CMD" | grep -q '\[BEHAVIORAL\]'; then
        IS_BEHAVIORAL=1
    fi
    if [[ -z "$DOC_HITS" ]]; then
        if [[ $IS_BEHAVIORAL -eq 1 ]]; then
            block "mandatory docs missing: [BEHAVIORAL] commits MUST stage at least one of docs/00_spec.md, docs/00_plan.md, docs/00_journal.md (00_rule.md §Document).
  Stage the relevant doc updates alongside this commit before proceeding."
        else
            printf 'Pre-commit reminder: src/tests/pyproject changes staged but no docs/00_plan.md, docs/00_spec.md, docs/00_journal.md update. Update them now if this change adds/alters behavior, contracts, env vars, or lessons learned.\n' >&2
        fi
    fi

    # 4b. API.md gate — router/api.py changes alter HTTP contracts visible to
    #     callers; docs/API.md must stay in sync. Hard-block on [BEHAVIORAL];
    #     non-blocking reminder on [STRUCTURAL].
    #     Use --diff-filter=AM so a staged *deletion* of docs/API.md does not
    #     satisfy the requirement (deleted files appear in --name-only output).
    API_CODE_HITS="$(printf '%s\n' "$STAGED" | grep -E '^src/ragent/(routers/|api\.py$)' || true)"
    STAGED_API_DOC="$(git diff --cached --name-only --diff-filter=AM 2>/dev/null | grep -E '^docs/API\.md$' || true)"
    if [[ -n "$API_CODE_HITS" ]]; then
        if [[ -z "$STAGED_API_DOC" ]]; then
            if [[ $IS_BEHAVIORAL -eq 1 ]]; then
                block "docs/API.md missing: [BEHAVIORAL] commits that change src/ragent/routers/ or src/ragent/api.py MUST stage docs/API.md (HTTP contract may have changed).
  Review docs/API.md and stage it alongside this commit before proceeding."
            else
                printf 'Pre-commit reminder: API code changes (src/ragent/routers/ or src/ragent/api.py) staged but docs/API.md not updated. Update it if endpoints, request/response shapes, or supported MIME types changed.\n' >&2
            fi
        fi
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
FRESHNESS=3600  # 60 minutes
NOW=$(date +%s)
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
APPROVAL_AGE=$(( NOW - MARKER_TS ))
if [[ $APPROVAL_AGE -gt $FRESHNESS ]]; then
    block "pre-commit review gate: marker is stale (${APPROVAL_AGE}s old, max ${FRESHNESS}s). Re-run /simplify and /review."
fi
CURRENT_SHA="$(git diff --cached | sha256sum | cut -d' ' -f1)"
if [[ "$MARKER_SHA" != "$CURRENT_SHA" ]]; then
    block "pre-commit review gate: staged diff changed since marker was stamped (marker_sha=${MARKER_SHA:0:12}…, current_sha=${CURRENT_SHA:0:12}…).
  /simplify and /review reviewed a different diff. Re-run both against the current staged set."
fi

# Audit-log cross-check — see docs/00_journal.md 2026-05-09 Process row
# "Mandatory-step honesty (recurrence)". The marker file is last-write-wins;
# the audit log is append-only. Both /simplify AND /review must have logged
# an entry for the current diff_sha within the freshness window — otherwise
# one skill was skipped, or stale entries from a long-ago review of the same
# diff would falsely satisfy the gate.
AUDIT="$ROOT/.claude/.stamp_audit.log"
if [[ ! -f "$AUDIT" ]]; then
    block "pre-commit review gate: audit log .claude/.stamp_audit.log missing.
  Hardened stamp script appends every /simplify and /review run here.
  Re-run /simplify and /review (audit log is created on first stamp)."
fi
AUDIT_CUTOFF=$(( NOW - FRESHNESS ))
# Capture python output to a variable so an unhandled exception (non-zero
# exit) is observable via the assignment's exit status — process substitution
# would silently swallow it. Broad except in the loop also tolerates rows
# with a non-int `ts` (the previous narrow `except ValueError` only covered
# json.loads, leaving int() to crash the whole scan).
AUDIT_HITS=$(python3 - "$AUDIT" "$CURRENT_SHA" "$AUDIT_CUTOFF" <<'PY' 2>/dev/null
import json, sys
log, sha, cutoff = sys.argv[1], sys.argv[2], int(sys.argv[3])
hits = {"simplify": "no", "review": "no"}
with open(log) as f:
    for line in f:
        try:
            row = json.loads(line)
            if row.get("diff_sha") == sha and int(row.get("ts", 0)) >= cutoff:
                by = row.get("by", "")
                # Accept any mode variant: simplify, simplify:fast, simplify:full
                if by == "simplify" or by.startswith("simplify:"):
                    hits["simplify"] = "yes"
                elif by == "review" or by.startswith("review:"):
                    hits["review"] = "yes"
        except Exception:
            continue
print(hits["simplify"], hits["review"])
PY
) || AUDIT_HITS="no no"
read -r SIMPLIFY_HIT REVIEW_HIT <<<"$AUDIT_HITS"
if [[ "$SIMPLIFY_HIT" != yes || "$REVIEW_HIT" != yes ]]; then
    block "pre-commit review gate: audit log missing fresh /simplify or /review entry for current diff_sha=${CURRENT_SHA:0:12} (within ${FRESHNESS}s).
  Got simplify=${SIMPLIFY_HIT:-no} review=${REVIEW_HIT:-no}.
  BOTH skills must run against the staged diff. See docs/00_journal.md
  Process row 2026-05-09 'Mandatory-step honesty (recurrence)'."
fi

# 5b. Risk classification — high-risk commits can still be committed after the
#     fast skill stamps above, but a .pending_full_review marker is written so
#     the pre-push gate can require /simplify --mode full + /review --mode full
#     before the code leaves the machine.
#
#     Low-risk fast path: ALL conditions must hold —
#       (a) changed files <= 10
#       (b) total changed lines (insertions + deletions across all staged
#           files) <= 200; a sweeping refactor over many files is higher risk
#           even if individual hunks are small
#       (c) total deleted lines <= 50 (same cross-file total, not per-hunk —
#           large-scale deletions warrant closer scrutiny regardless of spread)
#       (d) no high-risk file touched: lockfiles, pyproject.toml, auth/security
#           patterns, bootstrap/ (lifecycle), routers/api.py (interface),
#           migrations/alembic/ (schema)
#     Trivially low-risk (skip remaining checks): only tests/ and/or .md files.
#
#     Note: the marker is written AFTER format+lint pass so a rejected commit
#     never leaves a stale .pending_full_review behind (fix for review #4).
_classify_risk() {
    local staged="$1"
    RISK_REASONS=""
    # Trivially safe: only tests + markdown
    if ! printf '%s\n' "$staged" | grep -qvE '(^tests/|\.md$)'; then
        return 0
    fi
    local hr=0
    # Lockfiles
    if printf '%s\n' "$staged" | grep -qE '(\.lock$)'; then
        RISK_REASONS+=" lockfile;"; hr=1; fi
    # Dependency manifest
    if printf '%s\n' "$staged" | grep -qE '^pyproject\.toml$'; then
        RISK_REASONS+=" dependency(pyproject.toml);"; hr=1; fi
    # Auth / Security — "auth"/"security"/"oauth"/"authentication"/
    # "authorization" as a whole path segment or underscore/dot-joined
    # token (matches src/ragent/auth/, .../security/, auth_mode.py,
    # test_oidc_auth.py, test_oauth.py, authentication/, etc). Anchored
    # on [/_.] or string boundaries so substring hits like AUTHORS.md or
    # author.py don't falsely escalate to the high-risk full-review path.
    if printf '%s\n' "$staged" | grep -qiE '(^|[/_.])(oauth|auth|security|authentication|authorization)([/_.]|$)'; then
        RISK_REASONS+=" auth/security;"; hr=1; fi
    # Lifecycle (composition root / bootstrap)
    if printf '%s\n' "$staged" | grep -qE '^src/ragent/bootstrap/'; then
        RISK_REASONS+=" lifecycle(bootstrap);"; hr=1; fi
    # Interface (routers + top-level api.py)
    if printf '%s\n' "$staged" | grep -qE '^src/ragent/(routers/|api\.py$)'; then
        RISK_REASONS+=" interface(routers/api);"; hr=1; fi
    # Schema (migrations + alembic versions)
    if printf '%s\n' "$staged" | grep -qE '^(migrations/|alembic/)'; then
        RISK_REASONS+=" schema(migrations/alembic);"; hr=1; fi
    # File count
    local fc
    fc=$(printf '%s\n' "$staged" | grep -c '.' 2>/dev/null || echo 0)
    if [[ $fc -gt 10 ]]; then
        RISK_REASONS+=" files>10($fc);"; hr=1; fi
    # Line counts — force POSIX locale so --shortstat output is always in
    # English regardless of the user's LC_* settings (locale-translated
    # "insertion"/"deletion" strings would cause the regex to return 0).
    local stat_summary ins del
    stat_summary=$(LC_ALL=C git diff --cached --shortstat 2>/dev/null || true)
    ins=$(printf '%s' "$stat_summary" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)
    del=$(printf '%s' "$stat_summary" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)
    local total=$(( ins + del ))
    if [[ $total -gt 200 ]]; then
        RISK_REASONS+=" lines>200($total);"; hr=1; fi
    if [[ $del -gt 50 ]]; then
        RISK_REASONS+=" deletions>50($del);"; hr=1; fi
    return $hr
}
# Run classification now so we know whether to write the pending marker,
# but defer the actual write until after format+lint succeed — a failing
# commit must not leave .pending_full_review in place (would block push
# even though no high-risk commit was ever created).
RISK_REASONS=""
_NEED_PENDING=0
if ! _classify_risk "$STAGED"; then
    _NEED_PENDING=1
    printf 'Pre-commit risk gate: HIGH-RISK commit — %s\n  Commit allowed; full review required before push.\n  Run /simplify --mode full and /review --mode full, then git push.\n' \
        "${RISK_REASONS%;}" >&2
fi

# Consume the marker — every commit requires a fresh /simplify + /review cycle.
rm -f "$APPROVAL"

if [[ $CODE_GATE -eq 0 ]]; then
    # Spec/plan-only commit: the marker check above is sufficient — skip
    # docker / test / format / lint, since no executable code changed.
    if [[ $_NEED_PENDING -eq 1 ]]; then
        PENDING="$ROOT/.claude/.pending_full_review"
        printf '{"diff_sha":"%s","ts":%s,"reason":"%s"}\n' \
            "$CURRENT_SHA" "$NOW" "${RISK_REASONS%;}" > "$PENDING"
    fi
    exit 0
fi

# 6. Migration SQL sanity — `init_schema.init_mariadb` and every
#    `alembic/versions/NNN_*.py` upgrader feed the raw .sql through
#    `for raw in sql.split(";"): _strip_comments(raw)`. The split runs
#    BEFORE the `--`-line filter, so a `;` inside a comment bisects the
#    comment block and the tail is fed to MariaDB as raw SQL (PR #84 / CI
#    failure on test_schema_drift, see docs/00_journal.md 2026-05-19).
#    Cheap grep guard: zero `;` allowed inside `--` lines of any staged
#    migrations/*.sql file. Spell out "SEMICOLON" or restructure with
#    em-dash / parentheses / sentence break instead.
STAGED_MIGRATIONS="$(printf '%s\n' "$STAGED" | grep -E '^migrations/.*\.sql$' || true)"
if [[ -n "$STAGED_MIGRATIONS" ]]; then
    OFFENDERS=""
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        # Match both `^-- ... ;` (full-line comment) and `SQL ... -- comment;`
        # (trailing inline comment) — `_strip_comments` only filters lines that
        # *start* with `--`, but the split-before-strip parser breaks on either
        # form because the `;` in the trailing comment also splits the file.
        if HIT="$(grep -nE -- '--[^\n]*;' "$f")"; then
            OFFENDERS+="$f:\n$HIT\n"
        fi
    done <<<"$STAGED_MIGRATIONS"
    if [[ -n "$OFFENDERS" ]]; then
        block "migration SQL contains \`;\` inside a \`--\` comment line — this trips the split-before-strip parser in alembic upgraders and init_schema.init_mariadb, producing 'syntax error near …' against the comment text. See docs/00_journal.md 2026-05-19 row.
$(printf '%b' "$OFFENDERS")
  Reword the comment to use em-dash / parentheses / sentence break, or spell out 'U+003B SEMICOLON'."
    fi
fi

# 7. Quality gate (commit-time): format + lint only. The full test suite
#    (`make test-gate`) moved to the pre-push hook (.claude/hooks/pre_push_gate.sh)
#    so commits stay fast; tests still run before code leaves the machine.
LOG_DIR="$(mktemp -d -t ragent-precommit-XXXXXX)"
trap 'rm -rf "$LOG_DIR"' EXIT
run_step() {
    local label="$1"; shift
    if ! "$@" >"$LOG_DIR/${label}.log" 2>&1; then
        local keep="$ROOT/.claude/logs"
        mkdir -p "$keep"
        cp "$LOG_DIR/${label}.log" "$keep/${label}.log" 2>/dev/null || true
        block "$label failed — see .claude/logs/${label}.log"
    fi
}

run_step format make format
run_step lint   make lint

# All blocking checks passed — safe to persist the high-risk marker now.
# Writing it here (not before format+lint) ensures a rejected commit never
# leaves a stale .pending_full_review that would incorrectly block pushes.
if [[ $_NEED_PENDING -eq 1 ]]; then
    PENDING="$ROOT/.claude/.pending_full_review"
    printf '{"diff_sha":"%s","ts":%s,"reason":"%s"}\n' \
        "$CURRENT_SHA" "$NOW" "${RISK_REASONS%;}" > "$PENDING"
fi

exit 0
