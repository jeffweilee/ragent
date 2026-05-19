"""Bootstrap auto-init: MariaDB tables and ES indexes (T0.8d).

Idempotent: CREATE IF NOT EXISTS for MariaDB; PUT /<index> only when the index
is absent for ES. Refuses to ALTER existing tables or update existing indexes.
Schema drift is logged as event=schema.drift and must surface in /readyz.
"""

import base64
import json
import os
import ssl
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import structlog
from sqlalchemy import text

logger = structlog.get_logger(__name__)

_MIGRATIONS = Path(__file__).parents[3] / "migrations"
_ES_RESOURCES = Path(__file__).parents[3] / "resources" / "es"


def _es_auth_headers() -> dict[str, str]:
    """Build Authorization header from env vars (API key takes precedence over Basic)."""
    api_key = os.environ.get("ES_API_KEY")
    if api_key:
        return {"Authorization": f"ApiKey {api_key}"}
    user = os.environ.get("ES_USERNAME")
    password = os.environ.get("ES_PASSWORD")
    if user and password:
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {}


def _es_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if os.environ.get("ES_VERIFY_CERTS", "true").lower() in ("false", "0"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _es_request(url: str, method: str = "GET", body: dict | None = None) -> dict | None:
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers.update(_es_auth_headers())
    req = Request(url, data=data, method=method, headers=headers)
    # ES 9 takes ~30s to finish warming up after port 9200 opens; use 120s for writes.
    timeout = 120 if method in ("PUT", "POST", "DELETE") else 30
    try:
        with urlopen(req, timeout=timeout, context=_es_ssl_context()) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _strip_comments(sql: str) -> str:
    """Drop `--` line comments — both whole-line (`^--`) and trailing
    (`<sql> -- tail`). Preserves blank lines so multi-line statements stay
    intact after the `;` split. Does NOT handle `--` inside string literals
    because the project's migrations contain no such literals (the JSON
    seed in 009_system_settings uses `JSON_OBJECT(...)`, not string lits).
    Adding tokenizer support is a future change if that invariant breaks.
    """
    out: list[str] = []
    for ln in sql.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("--"):
            continue
        idx = ln.find("--")
        if idx >= 0:
            ln = ln[:idx].rstrip()
        out.append(ln)
    return "\n".join(out).strip()


def _iter_statements(sql: str) -> Iterator[str]:
    """Yield non-empty SQL statements from a multi-statement script.

    Strips `--` line comments FIRST, then splits on `;`. The reverse order
    (split-then-strip — the historical pattern across init_schema and the
    alembic wrappers) tears a `--` comment in half whenever it contains a
    `;` mid-line, leaving the trailing portion without its `--` prefix and
    fed to the engine as broken SQL (PR #86 hit this twice, once on
    schema.sql and once on migrations/010_feedback.sql).
    """
    for raw in _strip_comments(sql).split(";"):
        stmt = raw.strip()
        if stmt:
            yield stmt


def init_mariadb(engine) -> None:
    sql = (_MIGRATIONS / "schema.sql").read_text(encoding="utf-8")
    with engine.begin() as conn:
        for stmt in _iter_statements(sql):
            conn.execute(text(stmt))


def init_es(es_url: str) -> None:
    # Test override: integration tests run against vanilla ES (no analysis-icu
    # plugin) and point this at tests/resources/es/ which omits the ICU analyzer
    # (B36).
    resources_dir = Path(os.environ.get("RAGENT_ES_RESOURCES_DIR") or _ES_RESOURCES)
    base = es_url.rstrip("/")

    # B59 — pipelines MUST land before indexes. `chunks_v1.settings.index.
    # default_pipeline` references `chunks_default`; ES rejects index creation
    # when its referenced pipeline doesn't exist. Pipeline PUT is idempotent
    # on the ES side (overwrite-by-id), no HEAD guard needed.
    pipelines_dir = resources_dir / "pipelines"
    if pipelines_dir.is_dir():
        for path in sorted(pipelines_dir.glob("*.json")):
            pipeline_id = path.stem
            body = json.loads(path.read_text(encoding="utf-8"))
            _es_request(f"{base}/_ingest/pipeline/{pipeline_id}", method="PUT", body=body)
            logger.info("es.pipeline_put", pipeline=pipeline_id)

    # B60 / T-EI.6 — chunks index name is env-overridable (matches the App's
    # `Container.chunks_index_name` resolution, T-EI.1); other resources keep
    # filename-as-name. Without this, an `ES_CHUNKS_INDEX=foo` operator gets
    # bootstrap-created `chunks_v1` but App reads/writes `foo` — silent split.
    chunks_index_name = os.environ.get("ES_CHUNKS_INDEX", "chunks_v1")
    for path in sorted(resources_dir.glob("*.json")):
        index = chunks_index_name if path.stem == "chunks_v1" else path.stem
        index_url = f"{base}/{index}"
        existing = _es_request(index_url, method="HEAD")
        if existing is not None:
            logger.info("es.index_exists", index=index)
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        _es_request(index_url, method="PUT", body=body)
        logger.info("es.index_created", index=index)


def init_minio_buckets() -> None:
    """Create the configured bucket on every site if missing. Idempotent."""
    from ragent.storage.minio_registry import MinioSiteRegistry

    try:
        registry = MinioSiteRegistry.from_env()
    except ValueError as exc:
        # MinIO not configured (e.g. lightweight test boot): skip silently.
        logger.info("minio.init_skipped", reason=str(exc))
        return
    for name, rec in registry._sites.items():  # noqa: SLF001 — boot path
        try:
            if not rec.client.bucket_exists(rec.bucket):
                rec.client.make_bucket(rec.bucket)
                logger.info("minio.bucket_created", site=name, bucket=rec.bucket)
            else:
                logger.info("minio.bucket_exists", site=name, bucket=rec.bucket)
        except Exception:
            logger.exception("minio.bucket_init_error", site=name, bucket=rec.bucket)
            raise


def _to_sync_dsn(dsn: str) -> str:
    return dsn.replace("mysql+aiomysql://", "mysql+pymysql://")


def to_async_dsn(dsn: str) -> str:
    return dsn.replace("mysql+pymysql://", "mysql+aiomysql://")


def auto_init(db_url: str, es_url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(_to_sync_dsn(db_url))
    init_mariadb(engine)
    init_es(es_url)
    init_minio_buckets()


def init_schema() -> None:
    """No-arg entrypoint that reads MARIADB_DSN and ES_HOSTS from env vars."""
    db_url = os.environ.get("MARIADB_DSN", "")
    es_hosts = os.environ.get("ES_HOSTS", "")
    es_url = es_hosts.split(",")[0] if es_hosts else ""
    auto_init(db_url=db_url, es_url=es_url)
