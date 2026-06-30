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


def iter_statements(sql: str) -> Iterator[str]:
    for raw in _strip_comments(sql).split(";"):
        stmt = raw.strip()
        if stmt:
            yield stmt


def init_mariadb(engine) -> None:
    sql = (_MIGRATIONS / "schema.sql").read_text(encoding="utf-8")
    with engine.begin() as conn:
        for stmt in iter_statements(sql):
            conn.execute(text(stmt))


def put_es_pipelines(es_url: str, resources_dir: Path | None = None) -> None:
    """PUT every `<resources_dir>/pipelines/*.json` ingest pipeline (B59).

    Pipelines MUST land before indexes: `chunks_v1.settings.index.
    default_pipeline` references `chunks_default`, and ES rejects index
    creation when its referenced pipeline doesn't exist. PUT is idempotent
    on the ES side (overwrite-by-id), no HEAD guard needed.
    """
    resources_dir = resources_dir or Path(
        os.environ.get("RAGENT_ES_RESOURCES_DIR") or _ES_RESOURCES
    )
    base = es_url.rstrip("/")
    pipelines_dir = resources_dir / "pipelines"
    if not pipelines_dir.is_dir():
        return
    for path in sorted(pipelines_dir.glob("*.json")):
        pipeline_id = path.stem
        body = json.loads(path.read_text(encoding="utf-8"))
        _es_request(f"{base}/_ingest/pipeline/{pipeline_id}", method="PUT", body=body)
        logger.info("es.pipeline_put", pipeline=pipeline_id)


def init_es(es_url: str) -> None:
    # Test override: integration tests run against vanilla ES (no analysis-icu
    # plugin) and point this at tests/resources/es/ which omits the ICU analyzer
    # (B36).
    resources_dir = Path(os.environ.get("RAGENT_ES_RESOURCES_DIR") or _ES_RESOURCES)
    base = es_url.rstrip("/")
    put_es_pipelines(es_url, resources_dir)

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

    # T-EM-R.2 — create read alias `{chunks_index_name}_active` if absent.
    # The alias name is stable across promote/commit cycles; its target flips
    # via lifecycle cutover/rollback (POST /_aliases swap), never by re-running
    # this bootstrap. We only create it here for fresh installs and upgrades.
    alias_name = f"{chunks_index_name}_active"
    alias_check_url = f"{base}/_alias/{alias_name}"
    if _es_request(alias_check_url, method="HEAD") is None:
        _es_request(
            f"{base}/_aliases",
            method="POST",
            body={"actions": [{"add": {"index": chunks_index_name, "alias": alias_name}}]},
        )
        logger.info("es.alias_created", alias=alias_name, index=chunks_index_name)
    else:
        logger.info("es.alias_exists", alias=alias_name)


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


def _wrap_ping(dbapi_conn: object) -> None:
    # aiomysql ping(reconnect: bool) has no default; do_ping omits it on
    # the _send_false_to_ping=False path, raising TypeError.
    # Patch the class (not the instance) — AsyncAdapt_aiomysql_connection
    # uses __slots__ so instance attribute assignment raises AttributeError.
    cls = type(dbapi_conn)
    if getattr(cls, "_ragent_ping_patched", False):
        return
    _orig = cls.ping  # type: ignore[attr-defined]
    cls.ping = lambda self, reconnect=False: _orig(self, reconnect)  # type: ignore[attr-defined]
    cls._ragent_ping_patched = True  # type: ignore[attr-defined]


def patch_aiomysql_ping(engine: object) -> None:
    from sqlalchemy import event

    event.listen(
        engine.sync_engine,  # type: ignore[attr-defined]
        "connect",
        lambda dbapi_conn, _: _wrap_ping(dbapi_conn),
    )


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
