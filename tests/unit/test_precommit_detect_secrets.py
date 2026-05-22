from pathlib import Path

import yaml


def test_precommit_includes_detect_secrets_hook() -> None:
    config_path = Path('.pre-commit-config.yaml')
    assert config_path.exists()

    parsed = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    repos = parsed.get('repos', [])

    detect_repo = next(
        (repo for repo in repos if repo.get('repo') == 'https://github.com/Yelp/detect-secrets'),
        None,
    )
    assert detect_repo is not None

    hooks = detect_repo.get('hooks', [])
    detect_hook = next((hook for hook in hooks if hook.get('id') == 'detect-secrets'), None)
    assert detect_hook is not None
    assert '--baseline' in detect_hook.get('args', [])
