"""T-RG.6 — ES and Redis are advisory dependencies; MariaDB and MinIO gate.

The chat/retrieve read surface needs MariaDB; ingest staging needs MinIO. Neither
ES nor Redis is load-bearing for keeping the process useful: ES failures degrade
retrieval quality, and every Redis surface has a documented fallback (rate
limiting fails open, the PAT cache falls through to MariaDB, chat streams drop to
connection-bound SSE).

Treating them as gating dependencies inverted that. A red ES probe made
`/readyz` 503, and with a k8s `readinessProbe` at `failureThreshold: 3` the Pod
leaves the Service ~15 s later — the whole API goes dark over a dependency the
code is written to survive. `_check_infra_ready` went further and refused to boot
at all.

`/livez` must stay independent of all of it so a degraded pod is never *restarted*
on a dependency's behalf.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.bootstrap.app import _check_infra_ready
from ragent.routers.health import create_health_router
from ragent.routers.health_probes import ADVISORY_PROBES


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise RuntimeError("dependency down")


def _client(probes: dict) -> TestClient:
    app = FastAPI()
    app.include_router(create_health_router(probes=probes))
    return TestClient(app, raise_server_exceptions=True)


def _probes(**overrides):
    base = {"mariadb": _ok, "minio": _ok, "es": _ok, "redis_rate_limiter": _ok}
    base.update(overrides)
    return base


# --- /readyz ------------------------------------------------------------


@pytest.mark.parametrize("advisory", sorted(ADVISORY_PROBES))
def test_readyz_stays_200_when_an_advisory_probe_fails(advisory: str) -> None:
    resp = _client(_probes(**{advisory: _fail})).get("/readyz")
    assert resp.status_code == 200


def test_readyz_names_the_degraded_dependency_so_ops_can_still_see_it() -> None:
    """Advisory must not mean invisible."""
    resp = _client(_probes(es=_fail)).get("/readyz")
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["degraded"] == ["es"]


def test_readyz_is_plain_ok_when_everything_is_green() -> None:
    assert _client(_probes()).get("/readyz").json() == {"status": "ok"}


@pytest.mark.parametrize("required", ["mariadb", "minio"])
def test_readyz_still_503s_when_a_required_probe_fails(required: str) -> None:
    resp = _client(_probes(**{required: _fail})).get("/readyz")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_a_required_failure_outranks_a_simultaneous_advisory_one() -> None:
    resp = _client(_probes(mariadb=_fail, es=_fail)).get("/readyz")
    assert resp.status_code == 503
    assert "mariadb" in resp.json()["detail"]


# --- /startupz ----------------------------------------------------------


def test_startupz_latches_green_despite_a_failing_advisory_probe() -> None:
    """Otherwise a Redis that is down at boot pins the pod in 'starting' forever."""
    resp = _client(_probes(redis_rate_limiter=_fail)).get("/startupz")
    assert resp.status_code == 200


def test_startupz_still_waits_for_a_required_probe() -> None:
    assert _client(_probes(mariadb=_fail)).get("/startupz").status_code == 503


# --- boot ---------------------------------------------------------------


async def test_boot_survives_a_failing_advisory_probe() -> None:
    broker = _StubBroker()
    await _check_infra_ready(_probes(es=_fail, redis_rate_limiter=_fail), broker, _StubContainer())


async def test_boot_still_aborts_on_a_failing_required_probe() -> None:
    with pytest.raises(RuntimeError, match="mariadb"):
        await _check_infra_ready(_probes(mariadb=_fail), _StubBroker(), _StubContainer())


class _StubBroker:
    def find_task(self, _label: str) -> object:
        return object()


class _StubContainer:
    token_managers: tuple = ()


# --- boot-time schema init ----------------------------------------------


def test_auto_init_still_raises_when_mariadb_is_unreachable(monkeypatch) -> None:
    import ragent.bootstrap.init_schema as mod

    def _boom(_engine) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(mod, "init_mariadb", _boom)
    monkeypatch.setattr(mod, "to_sync_dsn", lambda dsn: dsn)
    monkeypatch.setattr("sqlalchemy.create_engine", lambda _dsn: object())

    with pytest.raises(OSError):
        mod.auto_init(db_url="mysql+pymysql://x/y", es_url="http://es:9200")


# --- ES degradation is for AVAILABILITY only (Codex P1, PR #246) --------


def _patch_init(monkeypatch, es_side_effect):
    import ragent.bootstrap.init_schema as mod

    calls: list[str] = []
    monkeypatch.setattr(mod, "init_mariadb", lambda _e: calls.append("mariadb"))
    monkeypatch.setattr(mod, "init_minio_buckets", lambda: calls.append("minio"))
    monkeypatch.setattr(mod, "to_sync_dsn", lambda dsn: dsn)
    monkeypatch.setattr("sqlalchemy.create_engine", lambda _dsn: object())

    def _es(_url: str) -> None:
        calls.append("es")
        raise es_side_effect

    monkeypatch.setattr(mod, "init_es", _es)
    return mod, calls


def test_boot_survives_an_unreachable_elasticsearch(monkeypatch) -> None:
    """Transport failure = the advisory case; skip and serve."""
    from urllib.error import URLError

    mod, calls = _patch_init(monkeypatch, URLError("connection refused"))
    mod.auto_init(db_url="mysql+pymysql://x/y", es_url="http://es:9200")
    assert calls == ["mariadb", "es", "minio"]  # MinIO still runs


def test_boot_still_aborts_on_a_malformed_es_resource(monkeypatch) -> None:
    """A malformed resources/es/*.json is a deployment defect, not an outage.

    Swallowing it would let the process boot with a required ingest pipeline
    never created. `probe_es` only checks cluster health plus index existence —
    an index left over from a previous deploy keeps the probe green while writes
    fail on the missing pipeline. Nothing self-heals that, so it must abort.
    """
    import json

    mod, _ = _patch_init(monkeypatch, json.JSONDecodeError("bad", "{", 0))
    with pytest.raises(json.JSONDecodeError):
        mod.auto_init(db_url="mysql+pymysql://x/y", es_url="http://es:9200")


def test_boot_still_aborts_when_es_rejects_the_request(monkeypatch) -> None:
    """A 4xx means ES answered — bad mapping, bad auth. Config defect, not availability."""
    from urllib.error import HTTPError

    mod, _ = _patch_init(monkeypatch, HTTPError("http://es:9200/x", 403, "Forbidden", {}, None))
    with pytest.raises(HTTPError):
        mod.auto_init(db_url="mysql+pymysql://x/y", es_url="http://es:9200")


def test_boot_survives_an_es_5xx(monkeypatch) -> None:
    """5xx (incl. a proxy in front of ES) is an availability signal — degrade."""
    from urllib.error import HTTPError

    mod, calls = _patch_init(
        monkeypatch, HTTPError("http://es:9200/x", 503, "Service Unavailable", {}, None)
    )
    mod.auto_init(db_url="mysql+pymysql://x/y", es_url="http://es:9200")
    assert calls == ["mariadb", "es", "minio"]
