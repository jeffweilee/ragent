"""Subprocess black-box tests for .claude/hooks/* quality-gate scripts.

Per docs/00_rule.md §Shell Hook Testing: hooks are load-bearing quality
gates and every behaviour path needs a subprocess test against a temporary
git repo fixture, not a unit test against extracted bash functions.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAMP_SCRIPT = REPO_ROOT / ".claude/hooks/stamp_pre_commit_approved.sh"
GATE_SCRIPT = REPO_ROOT / ".claude/hooks/pre_commit_gate.sh"

FRESHNESS = 3600  # must match pre_commit_gate.sh's FRESHNESS
SIMPLIFY_FULL = "simplify:full"
REVIEW_FULL = "review:full"


@pytest.fixture
def gate_repo(tmp_path):
    """A throwaway git repo with the real hook scripts and a stub Makefile.

    The hooks resolve ROOT via `git rev-parse --show-toplevel` and operate
    relative to it, so running them with cwd inside this repo fully
    isolates them from the real project's .claude/ state.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "Makefile").write_text("format:\n\t@true\n\nlint:\n\t@true\n")
    subprocess.run(["git", "add", "Makefile"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    hooks_dir = repo / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "stamp_pre_commit_approved.sh").write_text(STAMP_SCRIPT.read_text())
    (hooks_dir / "pre_commit_gate.sh").write_text(GATE_SCRIPT.read_text())
    return repo


def run_hook(repo, script, command):
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(repo / ".claude/hooks" / script)],
        cwd=repo,
        input=payload,
        capture_output=True,
        text=True,
    )


def stage_file(repo, rel_path, content):
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", rel_path], cwd=repo, check=True)


def stage_default_change(repo):
    """Stage a trivial src/ change — triggers CODE_GATE without adding risk."""
    stage_file(repo, "src/ragent/foo.py", "def foo():\n    return 1\n")


def staged_diff_sha(repo):
    diff = subprocess.run(
        ["git", "diff", "--cached"], cwd=repo, check=True, capture_output=True
    ).stdout
    return hashlib.sha256(diff).hexdigest()


def write_marker(repo, sha, ts, by):
    (repo / ".claude/.pre_commit_approved").write_text(
        json.dumps({"diff_sha": sha, "ts": ts, "by": by})
    )


def append_audit(repo, sha, ts, by):
    with open(repo / ".claude/.stamp_audit.log", "a") as f:
        f.write(json.dumps({"ts": ts, "by": by, "diff_sha": sha}) + "\n")


def approve(repo, *, sha=None, now=None, simplify_ts=None, review_ts=None):
    """Write a marker + audit log that satisfies the gate for `sha`."""
    sha = sha or staged_diff_sha(repo)
    now = now if now is not None else int(time.time())
    simplify_ts = now if simplify_ts is None else simplify_ts
    review_ts = now if review_ts is None else review_ts
    write_marker(repo, sha, max(simplify_ts, review_ts), REVIEW_FULL)
    append_audit(repo, sha, simplify_ts, SIMPLIFY_FULL)
    append_audit(repo, sha, review_ts, REVIEW_FULL)
    return sha, now


# --- stamp_pre_commit_approved.sh -------------------------------------------


def run_stamp(repo, skill_arg, token_set=True):
    env = {**os.environ}
    if token_set:
        env["RAGENT_SKILL_INVOCATION_TOKEN"] = "1"
    else:
        env.pop("RAGENT_SKILL_INVOCATION_TOKEN", None)
    return subprocess.run(
        ["bash", str(repo / ".claude/hooks/stamp_pre_commit_approved.sh"), skill_arg],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_stamp_rejects_without_token(gate_repo):
    stage_file(gate_repo, "x.py", "x = 1\n")
    proc = run_stamp(gate_repo, "review", token_set=False)
    assert proc.returncode == 2
    assert "RAGENT_SKILL_INVOCATION_TOKEN" in proc.stderr
    assert not (gate_repo / ".claude/.pre_commit_approved").exists()


def test_stamp_rejects_invalid_skill_name(gate_repo):
    proc = run_stamp(gate_repo, "bogus")
    assert proc.returncode == 2
    assert "invalid skill name" in proc.stderr


def test_stamp_appends_valid_json_audit_entry(gate_repo):
    stage_file(gate_repo, "x.py", "x = 1\n")
    sha = staged_diff_sha(gate_repo)
    proc = run_stamp(gate_repo, "review")
    assert proc.returncode == 0
    audit_lines = (gate_repo / ".claude/.stamp_audit.log").read_text().strip().splitlines()
    last = json.loads(audit_lines[-1])
    assert last["by"] == REVIEW_FULL
    assert last["diff_sha"] == sha
    marker = json.loads((gate_repo / ".claude/.pre_commit_approved").read_text())
    assert marker["diff_sha"] == sha
    assert marker["by"] == REVIEW_FULL


# --- pre_commit_gate.sh: marker / audit-log gate ----------------------------


def test_gate_accepts_with_both_fresh_entries(gate_repo):
    stage_default_change(gate_repo)
    approve(gate_repo)
    result = run_hook(gate_repo, "pre_commit_gate.sh", 'git commit -m "[STRUCTURAL] x"')
    assert result.returncode == 0, result.stderr


def test_gate_rejects_when_audit_log_missing(gate_repo):
    stage_default_change(gate_repo)
    sha = staged_diff_sha(gate_repo)
    write_marker(gate_repo, sha, ts=int(time.time()), by=REVIEW_FULL)
    result = run_hook(gate_repo, "pre_commit_gate.sh", 'git commit -m "[STRUCTURAL] x"')
    assert result.returncode == 2
    assert "audit log" in result.stderr


def test_gate_rejects_when_only_one_skill_present(gate_repo):
    stage_default_change(gate_repo)
    sha = staged_diff_sha(gate_repo)
    now = int(time.time())
    write_marker(gate_repo, sha, now, REVIEW_FULL)
    append_audit(gate_repo, sha, now, REVIEW_FULL)
    result = run_hook(gate_repo, "pre_commit_gate.sh", 'git commit -m "[STRUCTURAL] x"')
    assert result.returncode == 2
    assert "simplify=no" in result.stderr
    assert "review=yes" in result.stderr


def test_gate_rejects_when_audit_entry_older_than_freshness_window(gate_repo):
    stage_default_change(gate_repo)
    sha = staged_diff_sha(gate_repo)
    now = int(time.time())
    stale = now - FRESHNESS - 60
    # Marker itself is fresh (now) so the marker-staleness check passes and
    # the failure is attributable specifically to the stale audit entries.
    write_marker(gate_repo, sha, now, REVIEW_FULL)
    append_audit(gate_repo, sha, stale, SIMPLIFY_FULL)
    append_audit(gate_repo, sha, stale, REVIEW_FULL)
    result = run_hook(gate_repo, "pre_commit_gate.sh", 'git commit -m "[STRUCTURAL] x"')
    assert result.returncode == 2
    assert "audit log missing fresh" in result.stderr


# --- pre_commit_gate.sh: _classify_risk auth/security boundary (PR #205 review) --


def test_classify_risk_low_for_authors_md_false_positive(gate_repo):
    stage_file(gate_repo, "AUTHORS.md", "Jane Doe\n")
    stage_default_change(gate_repo)
    approve(gate_repo)
    result = run_hook(gate_repo, "pre_commit_gate.sh", 'git commit -m "[STRUCTURAL] x"')
    assert result.returncode == 0, result.stderr
    assert not (gate_repo / ".claude/.pending_full_review").exists()


@pytest.mark.parametrize(
    "rel_path",
    [
        "src/ragent/auth_mode.py",
        "src/ragent/oauth_client.py",
        "src/ragent/authentication/handler.py",
        "src/ragent/authorization.py",
    ],
)
def test_classify_risk_high_for_auth_path_segment(gate_repo, rel_path):
    # Each alternative is staged and asserted in isolation (not consolidated
    # into one combined-paths test) because the gate's reason string comes
    # from a single grep over the whole staged set — a combined test would
    # pass even if only one alternative still matched, masking a regression
    # in the other three. Per-alternative isolation is the point of pinning.
    stage_file(gate_repo, rel_path, "MODE = 'oidc'\n")
    approve(gate_repo)
    result = run_hook(gate_repo, "pre_commit_gate.sh", 'git commit -m "[STRUCTURAL] x"')
    assert result.returncode == 0, result.stderr
    pending = json.loads((gate_repo / ".claude/.pending_full_review").read_text())
    assert "auth/security" in pending["reason"]
