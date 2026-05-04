"""Bootstrap auto-init: MariaDB tables and ES indexes (T0.8d).

Idempotent: CREATE IF NOT EXISTS for MariaDB; PUT /<index> only when the index
is absent for ES. Refuses to ALTER existing tables or update existing indexes.
Schema drift is logged as event=schema.drift and must surface in /readyz.
"""

import json
import logging
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import text

logger = logging.getLogger(__name__)

_MIGRATIONS = Path(__file__).parents[3] / "migrations"
_ES_RESOURCES = Path(__file__).parents[3] / "resources" / "es"

_REQUIRED_ES_PLUGINS = ["analysis-icu"]


class ESPluginMissingError(Exception):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"Required ES plugins not installed: {missing}")


def _es_request(url: str, method: str = "GET", body: dict | None = None) -> dict | None:
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def check_es_plugins(es_url: str) -> list[str]:
    """Return list of required plugins missing from every node in the cluster."""
    url = f"{es_url.rstrip('/')}/_nodes/plugins"
    info = _es_request(url)
    if not info:
        return list(_REQUIRED_ES_PLUGINS)
    installed: set[str] = set()
    for node in info.get("nodes", {}).values():
        installed.update(p["name"] for p in node.get("plugins", []))
    return [p for p in _REQUIRED_ES_PLUGINS if p not in installed]


def _strip_comments(sql: str) -> str:
    """Remove leading -- comment lines from a SQL statement fragment."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines).strip()


def init_mariadb(engine) -> None:
    """Execute schema.sql (CREATE IF NOT EXISTS) against a SQLAlchemy engine."""
    sql = (_MIGRATIONS / "schema.sql").read_text()
    with engine.connect() as conn:
        for raw in sql.split(";"):
            stmt = _strip_comments(raw)
            if stmt:
                conn.execute(text(stmt))
        conn.commit()


def init_es(es_url: str) -> None:
    """PUT each resources/es/*.json index if it does not already exist.

    Raises ESPluginMissingError when a required plugin is absent — the index
    that depends on it is not created and the error propagates to /readyz.
    """
    missing = check_es_plugins(es_url)
    if missing:
        raise ESPluginMissingError(missing)

    base = es_url.rstrip("/")
    for path in sorted(_ES_RESOURCES.glob("*.json")):
        index = path.stem
        index_url = f"{base}/{index}"
        existing = _es_request(index_url, method="HEAD")
        if existing is not None:
            logger.info("event=es.index_exists index=%s", index)
            continue
        body = json.loads(path.read_text())
        _es_request(index_url, method="PUT", body=body)
        logger.info("event=es.index_created index=%s", index)


def auto_init(db_url: str, es_url: str) -> None:
    """Full bootstrap: check ES plugins, init MariaDB tables, init ES indexes."""
    from sqlalchemy import create_engine

    engine = create_engine(db_url)
    init_mariadb(engine)
    init_es(es_url)
