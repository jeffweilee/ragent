"""app-doctor — pre-flight readiness check for local API + worker.

Verifies the operator can actually run `python -m ragent.api` /
`python -m ragent.worker` and successfully drive ingest + chat against
real datastores and AI endpoints.

Usage:
    uv run --env-file .env python scripts/app_doctor.py
    uv run --env-file .env python scripts/app_doctor.py --probe-live
    uv run --env-file .env python scripts/app_doctor.py --skip ai,minio

Exit codes:
    0 — all checks PASS (warnings allowed)
    1 — one or more checks FAILED
    2 — usage / import error
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

OK = "\033[32m✓\033[0m"
WARN = "\033[33m!\033[0m"
FAIL = "\033[31m✗\033[0m"
DIM = "\033[2m"
RST = "\033[0m"


@dataclass
class Result:
    name: str
    status: str  # "ok" | "warn" | "fail" | "skip"
    detail: str = ""

    def render(self) -> str:
        glyph = {"ok": OK, "warn": WARN, "fail": FAIL, "skip": f"{DIM}-{RST}"}[self.status]
        suffix = f"  {DIM}{self.detail}{RST}" if self.detail else ""
        return f"  {glyph} {self.name}{suffix}"


_results: list[Result] = []


def _record(r: Result) -> Result:
    _results.append(r)
    print(r.render())
    return r


def _ok(name: str, detail: str = "") -> Result:
    return _record(Result(name, "ok", detail))


def _warn(name: str, detail: str = "") -> Result:
    return _record(Result(name, "warn", detail))


def _fail(name: str, detail: str = "") -> Result:
    return _record(Result(name, "fail", detail))


def _skip(name: str, detail: str = "") -> Result:
    return _record(Result(name, "skip", detail))


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _try(name: str, fn: Callable[[], str | None]) -> Result:
    try:
        detail = fn() or ""
        return _ok(name, detail)
    except _Warn as exc:
        return _warn(name, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(name, f"{type(exc).__name__}: {exc}")


class _Warn(Exception):
    """Soft-failure used by checks to downgrade to warning."""


# ------------------------------------------------------------------ tools


def check_tools() -> None:
    _section("Toolchain")

    py = sys.version_info
    if py >= (3, 12):
        _ok("python ≥ 3.12", f"{py.major}.{py.minor}.{py.micro}")
    else:
        _fail("python ≥ 3.12", f"got {py.major}.{py.minor}.{py.micro}")

    if shutil.which("uv"):
        _ok("uv installed")
    else:
        _warn("uv installed", "not on PATH (only needed for the README invocation)")

    if shutil.which("mysqldump"):
        _ok("mysqldump", "available (schema-drift gate)")
    else:
        _warn("mysqldump", "missing — `make bootstrap` to install (only blocks test_schema_drift)")


# ------------------------------------------------------------------ env


_REQUIRED_VARS = (
    "RAGENT_ENV",
    "MARIADB_DSN",
    "ES_HOSTS",
    "AI_API_AUTH_URL",
    "EMBEDDING_API_URL",
    "LLM_API_URL",
    "RERANK_API_URL",
)


def check_env() -> None:
    _section("Environment variables")

    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        _fail("required vars present", f"missing: {', '.join(missing)}")
    else:
        _ok("required vars present", f"{len(_REQUIRED_VARS)} checked")

    # Phase-1 guard contract (matches src/ragent/bootstrap/guard.py).
    env = os.environ.get("RAGENT_ENV", "")
    if env != "dev":
        _fail("RAGENT_ENV=dev (P1)", f"got {env!r}")
    else:
        _ok("RAGENT_ENV=dev (P1)")

    if os.environ.get("RAGENT_AUTH_DISABLED", "").lower() != "true":
        _fail("RAGENT_AUTH_DISABLED=true (P1)")
    else:
        _ok("RAGENT_AUTH_DISABLED=true (P1)")

    host = os.environ.get("RAGENT_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        _fail("RAGENT_HOST=127.0.0.1 (P1)", f"got {host!r}")
    else:
        _ok("RAGENT_HOST=127.0.0.1 (P1)")

    # AI tokens — required unless K8s SA mode.
    if os.environ.get("AI_USE_K8S_SERVICE_ACCOUNT_TOKEN", "").lower() == "true":
        sa_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        if os.path.exists(sa_path):
            _ok("K8s SA token", sa_path)
        else:
            _fail("K8s SA token", f"AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true but {sa_path} missing")
    else:
        for v in ("AI_LLM_API_J1_TOKEN", "AI_EMBEDDING_API_J1_TOKEN", "AI_RERANK_API_J1_TOKEN"):
            if os.environ.get(v):
                _ok(v)
            else:
                _fail(v, "unset (set token or AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true)")

    # Redis topology coherence.
    mode = os.environ.get("REDIS_MODE", "standalone")
    if mode == "sentinel":
        if not os.environ.get("REDIS_SENTINEL_HOSTS"):
            _fail("redis sentinel config", "REDIS_SENTINEL_HOSTS missing")
        else:
            _ok("redis sentinel config")
    else:
        if not os.environ.get("REDIS_BROKER_URL"):
            _warn("REDIS_BROKER_URL", "unset; standalone broker will fail to connect")
        else:
            _ok("REDIS_BROKER_URL")
        if not os.environ.get("REDIS_RATELIMIT_URL"):
            _warn("REDIS_RATELIMIT_URL", "unset; chat rate-limiter will fail to init")
        else:
            _ok("REDIS_RATELIMIT_URL")

    # MinIO: MINIO_SITES is the v2 source-of-truth. When unset, composition
    # falls back to the legacy MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY trio to
    # synthesise a __default__ site, so those vars are only required in the
    # legacy path.
    sites = os.environ.get("MINIO_SITES", "").strip()
    if sites:
        try:
            parsed = json.loads(sites)
            names = [s.get("name") for s in parsed]
            if "__default__" not in names:
                _fail("MINIO_SITES JSON", "missing __default__ entry")
            else:
                _ok("MINIO_SITES JSON", f"sites={names}")
        except json.JSONDecodeError as exc:
            _fail("MINIO_SITES JSON", f"invalid JSON: {exc}")
    else:
        legacy = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        miss = [v for v in legacy if not os.environ.get(v)]
        if miss:
            _fail(
                "legacy MinIO vars",
                f"missing {miss}; set MINIO_SITES or provide all three legacy vars",
            )
        else:
            _ok("legacy MinIO vars present", "(used to synthesise __default__ site)")

    # MINIO_BUCKET default drift: .env.example says 'ragent';
    # composition.py & minio_registry.py both default to 'ragent-uploads'.
    if not os.environ.get("MINIO_BUCKET"):
        _warn(
            "MINIO_BUCKET",
            "unset → code defaults to 'ragent-uploads' (.env.example shows 'ragent')",
        )

    # Chunk-config sanity (mirrors guard.enforce → validate_chunk_config).
    try:
        from ragent.pipelines.factory import validate_chunk_config

        validate_chunk_config()
        _ok("CHUNK_* config")
    except Exception as exc:  # noqa: BLE001
        _fail("CHUNK_* config", str(exc))


# ------------------------------------------------------------------ TCP


def _tcp(host: str, port: int, timeout: float = 2.0) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


# ------------------------------------------------------------------ datastores


def check_mariadb() -> None:
    _section("MariaDB")
    dsn = os.environ.get("MARIADB_DSN")
    if not dsn:
        _skip("MariaDB", "MARIADB_DSN unset")
        return

    def _connect() -> str:
        from sqlalchemy import create_engine, text

        # Force sync driver for the doctor — avoids a fresh asyncio loop here.
        sync_dsn = dsn.replace("+aiomysql", "+pymysql").replace("mysql://", "mysql+pymysql://")
        eng = create_engine(sync_dsn, pool_pre_ping=True)
        with eng.connect() as c:
            row = c.execute(text("SELECT VERSION()")).scalar()
        eng.dispose()
        return f"version={row}"

    _try("connect + SELECT 1", _connect)

    def _alembic() -> str:
        from sqlalchemy import create_engine, text

        sync_dsn = dsn.replace("+aiomysql", "+pymysql").replace("mysql://", "mysql+pymysql://")
        eng = create_engine(sync_dsn)
        with eng.connect() as c:
            try:
                rows = list(c.execute(text("SELECT version_num FROM alembic_version")))
            except Exception as exc:  # noqa: BLE001
                eng.dispose()
                raise _Warn(
                    f"alembic_version table missing — run `alembic upgrade head` ({exc})"
                ) from exc
        eng.dispose()
        if not rows:
            raise _Warn("alembic_version empty — run `alembic upgrade head`")
        current = rows[0][0]

        # Compare against alembic head from filesystem.
        out = subprocess.run(
            ["uv", "run", "alembic", "heads"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if out.returncode != 0:
            raise _Warn(f"could not query alembic heads: {out.stderr.strip()}")
        head = (out.stdout.split() or [""])[0]
        if head and head != current:
            raise _Warn(f"current={current} head={head} — run `alembic upgrade head`")
        return f"at head {current}"

    _try("alembic at head", _alembic)


def check_elasticsearch() -> None:
    _section("Elasticsearch")
    hosts = os.environ.get("ES_HOSTS", "").strip()
    if not hosts:
        _skip("Elasticsearch", "ES_HOSTS unset")
        return

    def _client():  # noqa: ANN202
        from elasticsearch import Elasticsearch

        verify = os.environ.get("ES_VERIFY_CERTS", "true").lower() != "false"
        password = os.environ.get("ES_PASSWORD")
        basic_auth = (os.environ.get("ES_USERNAME", "elastic"), password) if password else None
        api_key = os.environ.get("ES_API_KEY")
        kwargs = {"hosts": hosts.split(","), "verify_certs": verify}
        if api_key:
            kwargs["api_key"] = api_key
        elif basic_auth:
            kwargs["basic_auth"] = basic_auth
        return Elasticsearch(**kwargs, request_timeout=5)

    es = None

    def _health() -> str:
        nonlocal es
        es = _client()
        h = es.cluster.health()
        status = h.get("status")
        if status not in ("yellow", "green"):
            raise RuntimeError(f"cluster status={status}")
        return f"status={status}"

    _try("cluster health", _health)

    if es is None:
        return

    def _icu() -> str:
        plugins = es.cat.plugins(format="json")
        names = {p.get("component") for p in plugins}
        if "analysis-icu" not in names:
            raise RuntimeError("analysis-icu plugin missing — index PUT will refuse")
        return "analysis-icu present"

    _try("analysis-icu plugin", _icu)

    index = os.environ.get("ES_CHUNKS_INDEX", "chunks_v1")

    def _index_exists() -> str:
        if not es.indices.exists(index=index):
            raise _Warn(
                f"{index} missing — first API/worker boot creates it from "
                f"resources/es/chunks_v1.json"
            )
        return f"{index} present"

    _try("chunks index", _index_exists)


def check_redis() -> None:
    _section("Redis")
    mode = os.environ.get("REDIS_MODE", "standalone")
    if mode == "sentinel":
        _skip("Redis", "sentinel mode — manual verification recommended")
        return

    for label, var in (("broker", "REDIS_BROKER_URL"), ("ratelimit", "REDIS_RATELIMIT_URL")):
        url = os.environ.get(var)
        if not url:
            _skip(f"redis {label}", f"{var} unset")
            continue

        def _ping(u: str = url) -> str:
            import redis

            r = redis.Redis.from_url(u, socket_timeout=2)
            r.ping()
            return urlparse(u).netloc

        _try(f"redis {label} PING", _ping)


def check_minio() -> None:
    _section("MinIO")

    sites_raw = os.environ.get("MINIO_SITES", "").strip()
    if sites_raw:
        try:
            sites = json.loads(sites_raw)
        except json.JSONDecodeError:
            _fail("MinIO", "MINIO_SITES not valid JSON (see env section)")
            return
    else:
        endpoint = os.environ.get("MINIO_ENDPOINT")
        if not endpoint:
            _skip("MinIO", "no MINIO_SITES and no legacy endpoint")
            return
        sites = [
            {
                "name": "__default__",
                "endpoint": endpoint,
                "access_key": os.environ.get("MINIO_ACCESS_KEY", ""),
                "secret_key": os.environ.get("MINIO_SECRET_KEY", ""),
                "bucket": os.environ.get("MINIO_BUCKET", "ragent"),
                "secure": os.environ.get("MINIO_SECURE", "false").lower() == "true",
            }
        ]

    from minio import Minio

    for site in sites:
        name = site.get("name", "?")

        def _check(s: dict = site) -> str:
            cli = Minio(
                s["endpoint"],
                access_key=s.get("access_key"),
                secret_key=s.get("secret_key"),
                secure=bool(s.get("secure", False)),
            )
            list(cli.list_buckets())
            bucket = s.get("bucket")
            if bucket and not cli.bucket_exists(bucket):
                raise _Warn(f"reachable but bucket {bucket!r} missing")
            return f"{s['endpoint']} bucket={bucket}"

        _try(f"site {name}", _check)


# ------------------------------------------------------------------ AI APIs


def check_ai_endpoints() -> None:
    _section("AI endpoints (TCP reachability)")
    import httpx

    targets = {
        "AI_API_AUTH_URL": os.environ.get("AI_API_AUTH_URL"),
        "EMBEDDING_API_URL": os.environ.get("EMBEDDING_API_URL"),
        "LLM_API_URL": os.environ.get("LLM_API_URL"),
        "RERANK_API_URL": os.environ.get("RERANK_API_URL"),
    }
    for var, url in targets.items():
        if not url:
            _skip(var, "unset")
            continue
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            _fail(var, f"invalid URL {url!r}")
            continue

        def _probe(u: str = url, p=parsed) -> str:
            # TCP first — cheapest signal.
            port = p.port or (443 if p.scheme == "https" else 80)
            try:
                _tcp(p.hostname, port, timeout=2.0)
            except OSError as exc:
                raise RuntimeError(f"TCP {p.hostname}:{port} unreachable: {exc}") from exc
            # Then HEAD (auth-less). Any HTTP response = service alive; only
            # transport errors are failures.
            try:
                with httpx.Client(timeout=3.0, verify=False) as c:
                    resp = c.head(u)
                return f"{p.hostname}:{port} HTTP {resp.status_code}"
            except httpx.HTTPError:
                return f"{p.hostname}:{port} TCP ok (HTTP probe skipped)"

        _try(var, _probe)

    # Real J1→J2 exchange — boot path is silent on this; first /chat or first
    # ingest worker task surfaces stale creds as opaque 500s otherwise.
    auth_url = os.environ.get("AI_API_AUTH_URL")
    j1 = os.environ.get("AI_LLM_API_J1_TOKEN") or os.environ.get("AI_EMBEDDING_API_J1_TOKEN")
    if not (auth_url and j1):
        _skip("J1→J2 token exchange", "auth URL or J1 token unset")
        return

    def _exchange() -> str:
        url = auth_url.rstrip("/") + "/auth/api/accesstoken"
        with httpx.Client(timeout=5.0, verify=False) as c:
            resp = c.post(url, json={"key": j1})
        if resp.status_code >= 400:
            raise _Warn(f"HTTP {resp.status_code} from {url} — verify J1 token")
        try:
            data = resp.json()
        except ValueError as exc:
            raise _Warn(f"non-JSON response from {url}") from exc
        if "token" not in data or "expiresAt" not in data:
            raise _Warn(f"unexpected payload keys: {list(data)[:5]}")
        return f"got J2 (expires {data['expiresAt']})"

    _try("J1→J2 token exchange", _exchange)


# ------------------------------------------------------------------ live API


def check_live_api() -> None:
    _section("Live API (--probe-live)")
    import httpx

    host = os.environ.get("RAGENT_HOST", "127.0.0.1")
    port = os.environ.get("RAGENT_PORT", "8000")
    base = f"http://{host}:{port}"

    def _livez() -> str:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(f"{base}/livez")
            r.raise_for_status()
            return f"{base}/livez {r.json()}"

    _try("/livez", _livez)

    def _readyz() -> str:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/readyz")
            if r.status_code == 200:
                return f"{base}/readyz ok"
            raise _Warn(f"{r.status_code} {r.text[:200]}")

    _try("/readyz", _readyz)


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="ragent app-doctor — pre-flight check")
    ap.add_argument("--probe-live", action="store_true", help="GET /livez and /readyz")
    ap.add_argument(
        "--skip",
        default="",
        help="comma-separated sections to skip: tools,env,mariadb,es,redis,minio,ai,live",
    )
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    started = time.monotonic()
    print("\033[1mragent app-doctor\033[0m  —  pre-flight check\n")
    print(f"{DIM}cwd={os.getcwd()}{RST}")
    print(f"{DIM}.env loaded by uv: {'yes' if os.environ.get('MARIADB_DSN') else 'no'}{RST}")

    if "tools" not in skip:
        check_tools()
    if "env" not in skip:
        check_env()
    if "mariadb" not in skip:
        check_mariadb()
    if "es" not in skip:
        check_elasticsearch()
    if "redis" not in skip:
        check_redis()
    if "minio" not in skip:
        check_minio()
    if "ai" not in skip:
        check_ai_endpoints()
    if args.probe_live and "live" not in skip:
        check_live_api()

    fails = sum(1 for r in _results if r.status == "fail")
    warns = sum(1 for r in _results if r.status == "warn")
    oks = sum(1 for r in _results if r.status == "ok")
    elapsed = time.monotonic() - started

    print(f"\n\033[1mSummary\033[0m  {oks} ok · {warns} warn · {fails} fail  ({elapsed:.1f}s)")

    if fails:
        print(
            f"\n{FAIL} \033[1mNot ready.\033[0m "
            f"Fix the failed checks above before launching API/worker."
        )
        return 1
    if warns:
        print(f"\n{WARN} \033[1mReady with caveats.\033[0m Review warnings above.")
    else:
        print(f"\n{OK} \033[1mReady.\033[0m Launch:")
        print("  uv run --env-file .env alembic upgrade head")
        print("  uv run --env-file .env python -m ragent.api      # terminal 1")
        print("  uv run --env-file .env python -m ragent.worker   # terminal 2")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
