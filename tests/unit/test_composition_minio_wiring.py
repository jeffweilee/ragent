"""T-RR.4 (B37) — Composition + /readyz minio probe wired through MinioSiteRegistry.

When `MINIO_SITES` is set, the legacy single-site `MINIO_ENDPOINT`/
`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` triple must not be required at boot,
and `/readyz`'s minio probe must list buckets via
`container.minio_registry.default().client` rather than the legacy
`MinIOClient`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set only the non-MinIO required env vars; MinIO config is per-test."""
    pairs = {
        "AI_API_AUTH_URL": "http://auth.example/token",
        "AI_LLM_API_J1_TOKEN": "j1-llm",
        "AI_EMBEDDING_API_J1_TOKEN": "j1-emb",
        "AI_RERANK_API_J1_TOKEN": "j1-rerank",
        "EMBEDDING_API_URL": "http://emb.example",
        "LLM_API_URL": "http://llm.example",
        "RERANK_API_URL": "http://rerank.example",
        "ES_HOSTS": "http://es.example:9200",
        "MARIADB_DSN": "mysql+aiomysql://u:p@h:3306/db",
    }
    for k, v in pairs.items():
        monkeypatch.setenv(k, v)
    for legacy in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET"):
        monkeypatch.delenv(legacy, raising=False)
    import ragent.bootstrap.composition as comp

    comp._container = None  # noqa: SLF001


def test_build_container_succeeds_with_only_minio_sites(
    _minimal_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites = json.dumps(
        [
            {
                "name": "__default__",
                "endpoint": "minio.example:9000",
                "access_key": "ak",
                "secret_key": "sk",
                "bucket": "ragent-uploads",
            }
        ]
    )
    monkeypatch.setenv("MINIO_SITES", sites)

    from ragent.bootstrap.composition import build_container

    container = build_container()

    assert container.minio_registry is not None
    assert container.minio_registry.default().bucket == "ragent-uploads"


def test_readyz_minio_probe_uses_registry_default_client(_minimal_env: None) -> None:
    """`_build_probes` must source the minio probe from `minio_registry.default().client`.

    Failure mode pre-fix: probe pulled from a legacy `MinIOClient` wrapper that
    a `MINIO_SITES`-only deployment never constructed, so MinIO outages did not
    surface on /readyz.
    """
    from ragent.bootstrap.app import _build_probes

    fake_minio = MagicMock()
    fake_minio.list_buckets.return_value = []
    site = SimpleNamespace(client=fake_minio)
    registry = SimpleNamespace(default=lambda: site)

    container = SimpleNamespace(
        engine=MagicMock(),
        es_client=MagicMock(),
        minio_registry=registry,
        rate_limiter=SimpleNamespace(_redis=None),
    )

    probes = _build_probes(container)
    assert "minio" in probes

    asyncio.run(probes["minio"]())

    fake_minio.list_buckets.assert_called_once()
