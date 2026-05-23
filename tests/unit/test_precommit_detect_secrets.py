import json
from pathlib import Path

import yaml


def test_precommit_includes_detect_secrets_hook_with_baseline_and_excludes() -> None:
    config_path = Path(".pre-commit-config.yaml")
    assert config_path.exists()

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repos = parsed.get("repos", [])

    detect_repo = next(
        (repo for repo in repos if repo.get("repo") == "https://github.com/Yelp/detect-secrets"),
        None,
    )
    assert detect_repo is not None

    hooks = detect_repo.get("hooks", [])
    detect_hook = next((hook for hook in hooks if hook.get("id") == "detect-secrets"), None)
    assert detect_hook is not None

    args = detect_hook.get("args", [])
    assert "--baseline" in args
    assert ".secrets.baseline" in args
    assert "--exclude-files" in args


def test_secrets_baseline_is_valid_json_and_has_no_tracked_findings() -> None:
    baseline_path = Path(".secrets.baseline")
    assert baseline_path.exists()

    parsed = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert parsed.get("version") == "1.5.0"
    assert isinstance(parsed.get("plugins_used"), list)
    assert isinstance(parsed.get("filters_used"), list)
    assert parsed.get("results") == {}
