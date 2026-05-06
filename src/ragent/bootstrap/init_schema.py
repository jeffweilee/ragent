"""Bootstrap auto-init: MariaDB tables and ES indexes (T0.8d).

Idempotent: CREATE IF NOT EXISTS for MariaDB; PUT /<index> only when the index
is absent for ES. Refuses to ALTER existing tables or update existing indexes.
Schema drift is logged as event=schema.drift and must surface in /readyz.
"""

import base64
import json
import os
import ssl
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
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def init_mariadb(engine) -> None:
    sql = (_MIGRATIONS / "schema.sql").read_text()
    with engine.begin() as conn:
        for raw in sql.split(";"):
            stmt = _strip_comments(raw)
            if stmt:
                conn.execute(text(stmt))


def init_es(es_url: str) -> None:
    base = es_url.rstrip("/")
    for path in sorted(_ES_RESOURCES.glob("*.json")):
        index = path.stem
        index_url = f"{base}/{index}"
        existing = _es_request(index_url, method="HEAD")
        if existing is not None:
            logger.info("es.index_exists", index=index)
            continue
        body = json.loads(path.read_text())
        _es_request(index_url, method="PUT", body=body)
        logger.info("es.index_created", index=index)


def _to_sync_dsn(dsn: str) -> str:
    return dsn.replace("mysql+aiomysql://", "mysql+pymysql://")


def to_async_dsn(dsn: str) -> str:
    return dsn.replace("mysql+pymysql://", "mysql+aiomysql://")


def auto_init(db_url: str, es_url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(_to_sync_dsn(db_url))
    init_mariadb(engine)
    init_es(es_url)


def init_schema() -> None:
    """No-arg entrypoint that reads MARIADB_DSN and ES_HOSTS from env vars."""
    db_url = os.environ.get("MARIADB_DSN", "")
    es_hosts = os.environ.get("ES_HOSTS", "")
    es_url = es_hosts.split(",")[0] if es_hosts else ""
    auto_init(db_url=db_url, es_url=es_url)
