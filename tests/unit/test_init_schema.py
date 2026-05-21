"""Unit tests for init_schema.py — mock DB and ES so Docker is not required."""

import base64
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from ragent.bootstrap.init_schema import (
    _ES_RESOURCES,
    _es_auth_headers,
    _es_request,
    auto_init,
    init_es,
    init_mariadb,
    init_minio_buckets,
)

_REPO_ROOT = Path(__file__).parents[2]
_PROD_CHUNKS_V1 = _REPO_ROOT / "resources" / "es" / "chunks_v1.json"
_TEST_CHUNKS_V1 = _REPO_ROOT / "tests" / "resources" / "es" / "chunks_v1.json"

# ── init_es ─────────────────────────────────────────────────────────────────


def test_init_es_creates_index_when_absent() -> None:
    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        if method == "HEAD":
            return None  # index does not exist
        return {}  # PUT success

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")  # must not raise


def test_init_es_skips_existing_index() -> None:
    calls = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        calls.append((method, url))
        if method == "HEAD":
            return {}  # index exists
        return {}

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")

    # B59 — pipeline PUT is idempotent (no HEAD guard); index PUT is skipped.
    index_put_calls = [c for c in calls if c[0] == "PUT" and "_ingest/pipeline/" not in c[1]]
    assert not index_put_calls, "index PUT should not be called when index already exists"


def test_init_es_puts_pipelines_before_indexes() -> None:
    """T-EI.3 / B59 — `chunks_v1.settings.index.default_pipeline` references
    `chunks_default`; ES rejects index creation if the referenced pipeline
    doesn't exist yet. The bootstrap MUST PUT every pipeline before the
    first index PUT."""
    calls: list[tuple[str, str]] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        calls.append((method, url))
        if method == "HEAD":
            return None  # index does not exist → triggers PUT
        return {}

    with patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request):
        init_es("http://es:9200")

    puts = [(method, url) for method, url in calls if method == "PUT"]
    pipeline_puts = [i for i, (_, url) in enumerate(puts) if "/_ingest/pipeline/" in url]
    index_puts = [i for i, (_, url) in enumerate(puts) if "/_ingest/pipeline/" not in url]
    assert pipeline_puts, "no pipeline PUT was made — `chunks_default` resource missing?"
    assert index_puts, "no index PUT was made"
    assert max(pipeline_puts) < min(index_puts), (
        "all pipeline PUTs must precede any index PUT — index creation rejects "
        f"a `default_pipeline` referencing an absent pipeline. order={puts}"
    )


# ── ICU analyzer mapping (B26 / B42) ─────────────────────────────────────────


def test_prod_mapping_defines_icu_text_analyzer_for_cjk_bm25() -> None:
    mapping = json.loads(_PROD_CHUNKS_V1.read_text(encoding="utf-8"))
    analyzers = mapping["settings"]["index"]["analysis"]["analyzer"]
    assert analyzers["icu_text"] == {
        "type": "custom",
        "tokenizer": "icu_tokenizer",
        "filter": ["icu_folding", "lowercase"],
    }
    props = mapping["mappings"]["properties"]
    assert props["text"]["analyzer"] == "icu_text"
    assert props["title"]["analyzer"] == "icu_text"


def test_test_mapping_uses_standard_analyzer_no_icu_dependency() -> None:
    mapping = json.loads(_TEST_CHUNKS_V1.read_text(encoding="utf-8"))
    # Test ES has no analysis-icu plugin; absence of `analysis` block means
    # `text`/`title` fall back to ES default `standard` analyzer.
    assert "analysis" not in mapping["settings"]["index"]
    props = mapping["mappings"]["properties"]
    assert "analyzer" not in props["text"]
    assert "analyzer" not in props["title"]


def test_prod_mapping_uses_bbq_hnsw_vector_index() -> None:
    """B58: P1 reversal of B26 — prod flips `embedding.index_options.type`
    from `flat` to `bbq_hnsw` (Better Binary Quantization HNSW, ES 8.16+);
    ~32× memory reduction at negligible recall cost. Test resource keeps
    `flat` so vanilla ES 9.2.3 CI containers stay light-weight."""
    prod = json.loads(_PROD_CHUNKS_V1.read_text(encoding="utf-8"))
    assert prod["mappings"]["properties"]["embedding"]["index_options"] == {"type": "bbq_hnsw"}


# ── B59 indexed_at / default_pipeline (T-EI.3) ──────────────────────────────


def test_chunks_v1_mapping_declares_indexed_at_date_field() -> None:
    """T-EI.3 / B59 — `indexed_at` is populated by the ES `chunks_default`
    ingest pipeline (no Python writer touches the field); the mapping MUST
    declare it as a `date` field so it surfaces in `_search` results."""
    for resource_path in (_PROD_CHUNKS_V1, _TEST_CHUNKS_V1):
        mapping = json.loads(resource_path.read_text(encoding="utf-8"))
        assert mapping["mappings"]["properties"].get("indexed_at") == {"type": "date"}, (
            f"{resource_path.relative_to(_REPO_ROOT)} missing indexed_at:date — "
            "ingest pipeline writes to this field, mapping MUST declare it."
        )


def test_init_es_uses_env_chunks_index_name_for_chunks_resource(tmp_path: Path) -> None:
    """T-EI.6 / B60 — when operator sets `ES_CHUNKS_INDEX=foo`, `init_es`
    must PUT the chunks_v1.json schema to `/foo` (not `/chunks_v1`), so the
    bootstrap-created index matches what the App reads/writes (T-EI.1).
    PR #83 gemini-code-assist high — closes the T-EI.1 audit gap."""
    custom_dir = tmp_path / "es"
    custom_dir.mkdir()
    (custom_dir / "chunks_v1.json").write_text('{"settings": {}}')

    seen_index_puts: list[str] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        if method == "PUT" and "_ingest/pipeline/" not in url:
            seen_index_puts.append(url.rsplit("/", 1)[-1])
        return None  # HEAD → absent triggers PUT; PUT → return None is fine

    with (
        patch.dict(
            os.environ,
            {"RAGENT_ES_RESOURCES_DIR": str(custom_dir), "ES_CHUNKS_INDEX": "foo"},
        ),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request),
    ):
        init_es("http://es:9200")

    assert seen_index_puts == ["foo"], (
        f"expected PUT to /foo (env ES_CHUNKS_INDEX), got {seen_index_puts}"
    )


def test_init_es_keeps_filename_stem_for_non_chunks_resources(tmp_path: Path) -> None:
    """T-EI.6 / B60 — `ES_CHUNKS_INDEX` ONLY renames the chunks index;
    other resources (e.g. feedback_v1) keep filename-as-name semantics."""
    custom_dir = tmp_path / "es"
    custom_dir.mkdir()
    (custom_dir / "chunks_v1.json").write_text('{"settings": {}}')
    (custom_dir / "feedback_v1.json").write_text('{"settings": {}}')

    seen_index_puts: list[str] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        if method == "PUT" and "_ingest/pipeline/" not in url:
            seen_index_puts.append(url.rsplit("/", 1)[-1])
        return None

    with (
        patch.dict(
            os.environ,
            {"RAGENT_ES_RESOURCES_DIR": str(custom_dir), "ES_CHUNKS_INDEX": "foo"},
        ),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request),
    ):
        init_es("http://es:9200")

    assert sorted(seen_index_puts) == ["feedback_v1", "foo"], (
        f"expected feedback_v1 to keep stem, only chunks renamed; got {seen_index_puts}"
    )


def test_chunks_v1_settings_reference_default_pipeline() -> None:
    """T-EI.3 / B59 — `index.default_pipeline = chunks_default` is what wires
    the pipeline to every chunk write; both prod and test resources MUST set it."""
    for resource_path in (_PROD_CHUNKS_V1, _TEST_CHUNKS_V1):
        mapping = json.loads(resource_path.read_text(encoding="utf-8"))
        assert mapping["settings"]["index"].get("default_pipeline") == "chunks_default", (
            f"{resource_path.relative_to(_REPO_ROOT)} missing "
            "settings.index.default_pipeline=chunks_default."
        )


def test_test_mapping_structurally_matches_prod_except_documented_deltas() -> None:
    """B42 (ICU) + B58 (bbq_hnsw): two documented deltas separate the prod
    mapping from the test mapping; everything else (field set, types, dims,
    similarity) must stay identical so integration tests exercise the same
    shape that prod runs."""
    prod = json.loads(_PROD_CHUNKS_V1.read_text(encoding="utf-8"))
    test = json.loads(_TEST_CHUNKS_V1.read_text(encoding="utf-8"))

    # B42 — ICU analyzer is a prod-only block.
    prod["settings"]["index"].pop("analysis")
    for field in ("text", "title"):
        prod["mappings"]["properties"][field].pop("analyzer")

    # B58 — prod uses `bbq_hnsw`; test stays on `flat`. Pop both so the
    # remaining structural equality check is delta-neutral.
    assert prod["mappings"]["properties"]["embedding"].pop("index_options") == {"type": "bbq_hnsw"}
    assert test["mappings"]["properties"]["embedding"].pop("index_options") == {"type": "flat"}

    assert prod == test, (
        "tests/resources/es/chunks_v1.json has drifted from "
        "resources/es/chunks_v1.json beyond the documented ICU + bbq_hnsw deltas. "
        "Update the test mapping to match the prod mapping (sans documented deltas)."
    )


def test_init_es_reads_resources_dir_env_override(tmp_path: Path) -> None:
    custom_dir = tmp_path / "es"
    custom_dir.mkdir()
    (custom_dir / "marker_index.json").write_text('{"settings": {}}')

    seen_indexes: list[str] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        # Track index HEAD checks only (not alias checks).
        if method == "HEAD" and "/_alias/" not in url:
            seen_indexes.append(url.rsplit("/", 1)[-1])
            return None
        return {}

    with (
        patch.dict(os.environ, {"RAGENT_ES_RESOURCES_DIR": str(custom_dir)}),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request),
    ):
        init_es("http://es:9200")

    assert seen_indexes == ["marker_index"]


def test_init_es_falls_back_to_default_resources_when_env_unset() -> None:
    seen_indexes: list[str] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        # Track index HEAD checks only (not alias checks).
        if method == "HEAD" and "/_alias/" not in url:
            seen_indexes.append(url.rsplit("/", 1)[-1])
        return {}

    env = {k: v for k, v in os.environ.items() if k != "RAGENT_ES_RESOURCES_DIR"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request),
    ):
        init_es("http://es:9200")

    expected = sorted(p.stem for p in _ES_RESOURCES.glob("*.json"))
    assert sorted(seen_indexes) == expected


# ── init_mariadb ─────────────────────────────────────────────────────────────


def test_init_mariadb_executes_schema_statements() -> None:
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_conn
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    init_mariadb(mock_engine)
    # documents table is the only DDL statement after C6 (chunks dropped).
    assert mock_conn.execute.call_count >= 1


def test_init_mariadb_handles_semicolon_inside_trailing_dash_comments(
    tmp_path, monkeypatch
) -> None:
    """Inline trailing `--` comments on the same line as a statement also
    hide `;` from the splitter. The whole-line filter wouldn't catch this
    because the line doesn't START with `--`. Same bug class as the
    whole-line variant; gemini-code-assist flagged it on PR #89."""
    schema = "CREATE TABLE t (id INT);  -- inline trailing; with semicolon\n"
    (tmp_path / "schema.sql").write_text(schema, encoding="utf-8")
    monkeypatch.setattr("ragent.bootstrap.init_schema._MIGRATIONS", tmp_path)

    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_conn
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    init_mariadb(mock_engine)

    executed = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
    assert len(executed) == 1, f"expected 1 statement, got {len(executed)}: {executed!r}"
    assert "CREATE TABLE t" in executed[0]
    assert "with semicolon" not in executed[0]
    assert "inline trailing" not in executed[0]


def test_init_mariadb_handles_semicolon_inside_dash_comments(tmp_path, monkeypatch) -> None:
    """`;` inside a `--` comment must not tear the comment in half. Naive
    `sql.split(';')` followed by per-fragment `--` strip leaves the post-`;`
    portion of a comment without its `--` prefix, which then gets fed to the
    engine as broken SQL (MariaDB error 1064). Surfaced twice on PR #86 — once
    for schema.sql, once for migrations/010_feedback.sql."""
    schema = (
        "-- pre-DDL note; with a semicolon mid-sentence\n"
        "-- and a follow-up; line that also has one\n"
        "CREATE TABLE t (id INT);\n"
        "-- post-DDL note; also semicoloned\n"
        "INSERT INTO t (id) VALUES (1);\n"
    )
    (tmp_path / "schema.sql").write_text(schema, encoding="utf-8")
    monkeypatch.setattr("ragent.bootstrap.init_schema._MIGRATIONS", tmp_path)

    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_conn
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    init_mariadb(mock_engine)

    executed = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
    # Only the two real statements should reach the engine.
    assert len(executed) == 2, f"expected 2 statements, got {len(executed)}: {executed!r}"
    assert any("CREATE TABLE t" in s for s in executed)
    assert any("INSERT INTO t" in s for s in executed)
    # The naive parser would surface comment text as a "statement"; verify it doesn't.
    for stmt in executed:
        assert "mid-sentence" not in stmt
        assert "follow-up" not in stmt
        assert "post-DDL" not in stmt


# ── _es_request ──────────────────────────────────────────────────────────────


def test_es_request_returns_parsed_json_on_success() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"ok": True}).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("ragent.bootstrap.init_schema.urlopen", return_value=mock_resp):
        result = _es_request("http://es:9200/_cat")
    assert result == {"ok": True}


def test_es_request_returns_none_on_404() -> None:
    err = HTTPError("http://x", 404, "Not Found", {}, None)
    with patch("ragent.bootstrap.init_schema.urlopen", side_effect=err):
        result = _es_request("http://es:9200/missing", method="HEAD")
    assert result is None


def test_es_request_reraises_non_404_http_error() -> None:
    err = HTTPError("http://x", 500, "Server Error", {}, None)
    with patch("ragent.bootstrap.init_schema.urlopen", side_effect=err), pytest.raises(HTTPError):
        _es_request("http://es:9200/index")


def test_es_request_returns_empty_dict_for_head_with_no_body() -> None:
    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("ragent.bootstrap.init_schema.urlopen", return_value=mock_resp):
        result = _es_request("http://es:9200/chunks_v1", method="HEAD")
    assert result == {}


# ── auto_init ────────────────────────────────────────────────────────────────


def test_auto_init_calls_init_mariadb_and_init_es_and_init_minio() -> None:
    with (
        patch("ragent.bootstrap.init_schema.init_mariadb") as mock_db,
        patch("ragent.bootstrap.init_schema.init_es") as mock_es,
        patch("ragent.bootstrap.init_schema.init_minio_buckets") as mock_minio,
        patch("sqlalchemy.create_engine") as mock_engine_fn,
    ):
        auto_init("mysql+pymysql://u:p@h/db", "http://es:9200")
    mock_engine_fn.assert_called_once_with("mysql+pymysql://u:p@h/db")
    mock_db.assert_called_once()
    mock_es.assert_called_once_with("http://es:9200")
    mock_minio.assert_called_once_with()


# ── init_minio_buckets ───────────────────────────────────────────────────────


def _site(name: str, bucket: str, exists: bool):
    rec = MagicMock()
    rec.bucket = bucket
    rec.client.bucket_exists.return_value = exists
    return name, rec


def test_init_minio_buckets_creates_bucket_when_missing() -> None:
    name, rec = _site("__default__", "ragent-uploads", exists=False)
    registry = MagicMock()
    registry._sites = {name: rec}
    with patch("ragent.storage.minio_registry.MinioSiteRegistry.from_env", return_value=registry):
        init_minio_buckets()
    rec.client.make_bucket.assert_called_once_with("ragent-uploads")


def test_init_minio_buckets_is_idempotent_when_bucket_exists() -> None:
    name, rec = _site("__default__", "ragent-uploads", exists=True)
    registry = MagicMock()
    registry._sites = {name: rec}
    with patch("ragent.storage.minio_registry.MinioSiteRegistry.from_env", return_value=registry):
        init_minio_buckets()
    rec.client.make_bucket.assert_not_called()


def test_init_minio_buckets_skips_silently_when_unconfigured() -> None:
    with patch(
        "ragent.storage.minio_registry.MinioSiteRegistry.from_env",
        side_effect=ValueError("MinIO config missing"),
    ):
        init_minio_buckets()  # must not raise


def test_init_minio_buckets_propagates_errors() -> None:
    name, rec = _site("__default__", "ragent-uploads", exists=False)
    rec.client.make_bucket.side_effect = RuntimeError("S3 down")
    registry = MagicMock()
    registry._sites = {name: rec}
    with (
        patch("ragent.storage.minio_registry.MinioSiteRegistry.from_env", return_value=registry),
        pytest.raises(RuntimeError),
    ):
        init_minio_buckets()


# ── ES auth headers ──────────────────────────────────────────────────────────


def test_es_auth_headers_uses_api_key_when_set() -> None:
    with patch.dict(os.environ, {"ES_API_KEY": "my-key"}, clear=False):
        headers = _es_auth_headers()
    assert headers == {"Authorization": "ApiKey my-key"}


def test_es_auth_headers_uses_basic_auth_when_no_api_key() -> None:
    env = {"ES_USERNAME": "user", "ES_PASSWORD": "pass"}
    with patch.dict(os.environ, env, clear=False):
        headers = _es_auth_headers()
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    assert headers == {"Authorization": expected}


def test_es_auth_headers_api_key_takes_precedence_over_basic() -> None:
    env = {"ES_API_KEY": "key", "ES_USERNAME": "u", "ES_PASSWORD": "p"}
    with patch.dict(os.environ, env, clear=False):
        headers = _es_auth_headers()
    assert headers["Authorization"].startswith("ApiKey ")


def test_es_auth_headers_empty_when_no_credentials() -> None:
    with patch.dict(os.environ, {}, clear=True):
        headers = _es_auth_headers()
    assert headers == {}


# ── T-EM-R.2 — read alias creation ──────────────────────────────────────────


def test_init_es_creates_read_alias_after_index_creation(tmp_path: Path) -> None:
    """When the physical index is absent, init_es creates `{index}_active` alias."""
    custom_dir = tmp_path / "es"
    custom_dir.mkdir()
    (custom_dir / "chunks_v1.json").write_text('{"settings": {}}')

    calls: list[tuple[str, str]] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        calls.append((method, url))
        if method == "HEAD":
            return None  # both index and alias absent
        return {}  # PUT / POST success

    with (
        patch.dict(os.environ, {"RAGENT_ES_RESOURCES_DIR": str(custom_dir)}),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request),
    ):
        init_es("http://es:9200")

    alias_posts = [(m, u) for m, u in calls if m == "POST" and "_aliases" in u]
    assert alias_posts, "expected POST /_aliases to create read alias"
    # The POST must target the /_aliases endpoint.
    assert all("/_aliases" in u for _, u in alias_posts)


def test_init_es_read_alias_not_created_when_alias_exists(tmp_path: Path) -> None:
    """When the alias already exists, init_es must not re-POST /_aliases."""
    custom_dir = tmp_path / "es"
    custom_dir.mkdir()
    (custom_dir / "chunks_v1.json").write_text('{"settings": {}}')

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        # physical index exists; alias exists
        if method == "HEAD":
            return {}
        return {}

    alias_posts: list[str] = []

    def tracking_request(url: str, method: str = "GET", body: dict | None = None):
        if method == "POST" and "_aliases" in url:
            alias_posts.append(url)
        return fake_request(url, method, body)

    with (
        patch.dict(os.environ, {"RAGENT_ES_RESOURCES_DIR": str(custom_dir)}),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=tracking_request),
    ):
        init_es("http://es:9200")

    assert not alias_posts, "alias POST should be skipped when alias already exists"


def test_init_es_creates_alias_with_correct_name_for_custom_chunks_index(tmp_path: Path) -> None:
    """When ES_CHUNKS_INDEX=foo, the alias created must be `foo_active`."""
    custom_dir = tmp_path / "es"
    custom_dir.mkdir()
    (custom_dir / "chunks_v1.json").write_text('{"settings": {}}')

    alias_post_bodies: list[dict] = []

    def fake_request(url: str, method: str = "GET", body: dict | None = None):
        if method == "POST" and "_aliases" in url:
            alias_post_bodies.append(body or {})
        if method == "HEAD":
            return None  # absent → create
        return {}

    with (
        patch.dict(
            os.environ,
            {"RAGENT_ES_RESOURCES_DIR": str(custom_dir), "ES_CHUNKS_INDEX": "foo"},
        ),
        patch("ragent.bootstrap.init_schema._es_request", side_effect=fake_request),
    ):
        init_es("http://es:9200")

    assert alias_post_bodies, "expected an alias POST"
    actions = alias_post_bodies[0].get("actions", [])
    add_actions = [a["add"] for a in actions if "add" in a]
    assert add_actions, "alias POST body must include add action"
    assert add_actions[0]["alias"] == "foo_active"
    assert add_actions[0]["index"] == "foo"
