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
    "PAT_INIT_API_URL": "https://pat.example/api/pat/token",
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


def _fake_build_token_manager(**kwargs):
    """A REAL `VerifyingTokenManager` (no OIDC/JWKS network) that honours the
    verify flags — composition derives the PAT verifier from it via
    `dataclasses.replace`, which a MagicMock cannot stand in for."""
    from joserfc.jwk import KeySet

    from ragent.auth.jwt import VerifyingTokenManager

    return VerifyingTokenManager(
        jwks=KeySet([]),
        audience=kwargs["audience"],
        expected_iss=f"https://{kwargs['domain']}",
        verify_aud=kwargs.get("verify_aud", True),
        verify_exp=kwargs.get("verify_exp", True),
    )


def _build(*, pat_cache_from_env: MagicMock | None = None):
    """Run build_container() with every external dependency stubbed.

    `pat_cache_from_env` lets a test hold the `PatCache.from_env` stub so it can
    assert on the kwargs composition passes it."""
    pat_cache_from_env = pat_cache_from_env if pat_cache_from_env is not None else MagicMock()
    with (
        patch("ragent.bootstrap.init_schema.patch_aiomysql_ping"),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock()),
        patch("ragent.clients.embedding.EmbeddingClient", MagicMock()),
        patch("ragent.clients.llm.LLMClient", MagicMock()),
        patch("ragent.clients.rerank.RerankClient", MagicMock()),
        patch("ragent.clients.auth.TokenManager", MagicMock()),
        patch("ragent.auth.jwt.build_token_manager", side_effect=_fake_build_token_manager),
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
        patch("ragent.clients.pat_cache.PatCache.from_env", pat_cache_from_env),
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


def test_id_token_verifier_forces_aud_and_exp_checks(
    _base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The X-Id-Token verifier must check `aud` even when a dev flag loosened the
    middleware — that check is what separates an id token (aud=OIDC_AUDIENCE)
    from an access token (aud=the resource server)."""
    _set_pat_env(monkeypatch)
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_JWT_VERIFY_AUD", "false")
    monkeypatch.setenv("RAGENT_JWT_VERIFY_EXP", "false")

    container = _build()

    assert container.auth_token_manager.verify_aud is False  # middleware honours the flag
    assert container.pat_id_token_manager.verify_aud is True  # the PAT check does not
    assert container.pat_id_token_manager.verify_exp is True


def test_id_token_verifier_is_built_in_trust_header_mode(
    _base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trust-header deployment builds no middleware verifier, but authorize
    still has to verify the caller-supplied id token — so the slice builds one."""
    _set_pat_env(monkeypatch)
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")

    container = _build()

    assert container.auth_token_manager is None
    assert container.pat_id_token_manager is not None


def test_base_url_without_a_path_aborts_boot(
    _base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PAT_INIT_API_URL` carries the FULL mint endpoint. A leftover base URL
    would 404 on every mint and surface as `PAT_INIT_UNAVAILABLE` forever, so
    composition refuses it at boot instead."""
    _set_pat_env(monkeypatch)
    monkeypatch.setenv("PAT_INIT_API_URL", "https://pat.example")

    with pytest.raises(ValueError, match="PAT_INIT_API_URL"):
        _build()


def test_tombstone_ttl_tracks_the_wired_refresh_budget(
    _base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revocation tombstone must outlast the longest in-flight refresh, so it
    is derived from the SAME env values that size the refresh client and the
    service's retry loop — read once in composition, never re-read by the cache.

    Retuning the budget must move all three together; a second reader carrying
    its own defaults is exactly the drift this guards against."""
    _set_pat_env(monkeypatch)
    monkeypatch.setenv("PAT_REFRESH_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("PAT_REFRESH_MAX_RETRIES", "2")
    monkeypatch.setenv("PAT_REFRESH_BACKOFF_SECONDS", "1")

    from_env = MagicMock()
    service = _build(pat_cache_from_env=from_env).pat_service

    # 10 * (2 + 1) + 1 * (1 + 2) = 33
    assert from_env.call_args.kwargs["tombstone_ttl_seconds"] == 33
    assert service._refresh_client._timeout == 10.0  # noqa: SLF001
    assert service._max_retries == 2  # noqa: SLF001
    assert service._backoff_base == 1.0  # noqa: SLF001
