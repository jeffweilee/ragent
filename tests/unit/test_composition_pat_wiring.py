"""T-PAT.19 — build_container()'s PAT branch, exercised with real env vars.

`tests/unit/test_pat_wiring_smoke.py` hand-assembles a `PatService` mirroring
composition; that proves the object graph but never runs `build_container()`, so
a typo in any `PAT_INIT_*` env name would ship undetected (00_rule.md
§Composition Root: Production-Wiring Coverage). These tests drive the real
composition-root branch: the slice is absent without `PAT_PUBLIC_KEY`, present
with the full block, and a missing `PAT_INIT_*` var aborts boot.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.pat_fakes import AUD, ISS, NT_CLAIM, public_pem

# Every PAT_* var build_container() reads when the slice is enabled.
_PAT_ENV = {
    "PAT_PUBLIC_KEY": None,  # filled per-test from public_pem()
    "PAT_ISS": ISS,
    "PAT_AUD": AUD,
    "PAT_NT_KEY_NAME": NT_CLAIM,
    "PAT_REFRESH_API": "https://pat.example/refresh",
    "PAT_API_HEADER_TOKEN_KEY": "X-Pat-Service-Token",
    "PAT_API_HEADER_TOKEN_VALUE": "svc-secret",
    "PAT_INIT_API_URL": "https://pat.example",
    "PAT_INIT_API_TOKEN": "init-secret",
    "PAT_INIT_API_TOKEN_HEADER_KEY_NAME": "X-Pat-Init-Token",
    "PAT_INIT_AUTHORIZE_HEADER_KEY_NAME": "X-Auth-Token",
    "PAT_INIT_SSO_HEADER_KEY_NAME": "X-Sso-Site",
    "PAT_INIT_SSO_SITE_URL": "https://sso.example",
}

_KEK_ENV = {
    # A KeyManager envelope the PAT cipher can actually open.
    "RAGENT_KEK_BASE64": None,  # filled per-test
    "RAGENT_ENCRYPTED_DEK_BASE64": None,
}


@pytest.fixture()
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimum non-PAT env for build_container() to reach the PAT branch."""
    import ragent.bootstrap.composition as comp

    for key, value in {
        "RAGENT_AUTH_MODE": "jwt_header",
        "OIDC_DOMAIN": "idp.example",
        "OIDC_AUDIENCE": "ragent",
        "MARIADB_DSN": "mysql+aiomysql://u:p@h:3306/db",
        "AI_API_AUTH_URL": "http://auth.example/token",
        "AI_LLM_API_J1_TOKEN": "j1-llm",
        "AI_EMBEDDING_API_J1_TOKEN": "j1-emb",
        "AI_RERANK_API_J1_TOKEN": "j1-rerank",
        "EMBEDDING_API_URL": "http://emb.example",
        "LLM_API_URL": "http://llm.example",
        "RERANK_API_URL": "http://rerank.example",
        "ES_HOSTS": "http://es.example:9200",
        "MINIO_SITES": (
            '[{"name":"__default__","endpoint":"minio.example:9000",'
            '"access_key":"ak","secret_key":"example_minio_secret_not_real",'
            '"bucket":"b"}]'  # pragma: allowlist secret
        ),
    }.items():
        monkeypatch.setenv(key, value)
    for key in (*_PAT_ENV, *_KEK_ENV):
        monkeypatch.delenv(key, raising=False)
    comp._container = None  # noqa: SLF001


def _set_pat_env(monkeypatch: pytest.MonkeyPatch, *, omit: str | None = None) -> None:
    import base64
    import os

    from ragent.security.key_manager import KeyManager

    kek_b64 = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("RAGENT_KEK_BASE64", kek_b64)
    monkeypatch.setenv("RAGENT_ENCRYPTED_DEK_BASE64", KeyManager.wrap(kek_b64, os.urandom(32)))
    for key, value in {**_PAT_ENV, "PAT_PUBLIC_KEY": public_pem()}.items():
        if key == omit:
            continue
        monkeypatch.setenv(key, value)


def _build():
    """Run build_container() with every external dependency stubbed."""
    with (
        patch("ragent.bootstrap.init_schema.patch_aiomysql_ping"),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock()),
        patch("ragent.clients.embedding.EmbeddingClient", MagicMock()),
        patch("ragent.clients.llm.LLMClient", MagicMock()),
        patch("ragent.clients.rerank.RerankClient", MagicMock()),
        patch("ragent.clients.auth.TokenManager", MagicMock()),
        patch("ragent.auth.jwt.build_token_manager", MagicMock()),
        patch(
            "haystack_integrations.document_stores.elasticsearch.ElasticsearchDocumentStore",
            MagicMock(),
        ),
        patch("elasticsearch.Elasticsearch", MagicMock()),
        patch("ragent.pipelines.retrieve.build_retrieval_pipeline", MagicMock()),
        patch("ragent.pipelines.ingest.build_ingest_pipeline", MagicMock()),
        patch("ragent.repositories.document_repository.DocumentRepository", MagicMock()),
        patch("ragent.storage.minio_registry.MinioSiteRegistry", MagicMock()),
        patch("ragent.clients.rate_limiter.RateLimiter", MagicMock()),
        patch("ragent.clients.pat_cache.PatCache.from_env", MagicMock()),
        patch("ragent.extractors.registry.PluginRegistry", MagicMock()),
        patch("ragent.extractors.stub_graph.StubGraphExtractor", MagicMock()),
        patch("httpx.Client"),
    ):
        import ragent.bootstrap.composition as comp

        return comp.build_container()


def test_pat_slice_absent_without_public_key(_base_env: None) -> None:
    """Unset PAT_PUBLIC_KEY → no /pat/v1 router, no PAT attach (today's behaviour)."""
    assert _build().pat_service is None


def test_pat_slice_wired_with_full_env(_base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The full PAT_* block builds a PatService whose init client points at the
    documented mint URL — proving every PAT_INIT_* env NAME resolves."""
    _set_pat_env(monkeypatch)

    container = _build()

    assert container.pat_service is not None
    init_client = container.pat_service._init_client  # noqa: SLF001
    assert init_client._url == "https://pat.example/api/pat/token"  # noqa: SLF001
    assert init_client._static_headers == {  # noqa: SLF001
        "X-Pat-Init-Token": "init-secret",
        "X-Sso-Site": "https://sso.example",
    }
    assert init_client._authorize_header_key == "X-Auth-Token"  # noqa: SLF001


@pytest.mark.parametrize(
    "missing",
    [
        "PAT_INIT_API_URL",
        "PAT_INIT_API_TOKEN",
        "PAT_INIT_API_TOKEN_HEADER_KEY_NAME",
        "PAT_INIT_AUTHORIZE_HEADER_KEY_NAME",
        "PAT_INIT_SSO_HEADER_KEY_NAME",
        "PAT_INIT_SSO_SITE_URL",
    ],
)
def test_missing_pat_init_var_aborts_boot(
    _base_env: None, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """A PAT_INIT_* var left unset must fail at boot, not on the first authorize."""
    _set_pat_env(monkeypatch, omit=missing)

    with pytest.raises(SystemExit):
        _build()


def test_out_of_range_expire_days_aborts_boot(
    _base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAT_INIT_EXPIRE_DAYS beyond the init API's one-year ceiling fails at boot."""
    _set_pat_env(monkeypatch)
    monkeypatch.setenv("PAT_INIT_EXPIRE_DAYS", "400")

    with pytest.raises(ValueError, match="PAT_INIT_EXPIRE_DAYS"):
        _build()


def test_non_jwt_auth_mode_warns_that_authorize_needs_the_id_token_header(
    _base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAT enabled under a trust-header mode still boots (resolve works, and
    authorize works for a caller who sends the JWT header explicitly), but the
    operator gets a signal that the mode does not require that header."""
    from structlog.testing import capture_logs

    _set_pat_env(monkeypatch)
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")

    with capture_logs() as captured:
        container = _build()

    assert container.pat_service is not None  # slice still wired — resolve is fine
    warnings = [e for e in captured if e.get("event") == "pat.authorize_needs_id_token_header"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
