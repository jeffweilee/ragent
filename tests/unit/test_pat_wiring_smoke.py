"""T-PAT.11/29 — production-wiring smoke: the exact assembly composition.py
builds, proven end-to-end through the /pat/v1 router and the /brainagent/v1
**run** path (00_rule.md §Composition Root: Production-Wiring Coverage).

Builds a real PatService the way `build_container()` does — real `KeyManager`
+ `PATCipher`, real verifier via `import_pat_public_key`, real `PatCache` over
fakeredis — with only the DB repo and the refresh HTTP client faked, then:
  1. `POST /pat/v1/authorize` stores the PAT, and
  2. `POST /brainagent/v1` carries that PAT to the brain upstream.

The run path is the only PAT consumer (T-PAT.29): it is where brain invokes a
drive tool on the user's behalf. The `/upstream/*` management proxy resolves no
PAT at all, so it is not exercised here.
"""

from __future__ import annotations

import base64
import os

import fakeredis
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twp_ai.agents.brain import BrainAgent

from ragent.auth.pat_jwt import PatTokenVerifier, import_pat_public_key
from ragent.clients.brain_caller import BrainCaller
from ragent.clients.pat_cache import PatCache
from ragent.clients.pat_init_client import PatInitClient
from ragent.clients.pat_refresh_client import PatRefreshClient
from ragent.routers.brainagent import create_brainagent_v1_router
from ragent.routers.pat import ID_TOKEN_HEADER, create_pat_router
from ragent.security.key_manager import KeyManager
from ragent.security.pat_cipher import PATCipher
from ragent.services.pat_service import PatService
from tests.unit.pat_fakes import (
    AUD,
    ISS,
    JWT_CLAIM_USER_ID,
    NT_CLAIM,
    FakeRepo,
    id_token_manager,
    public_pem,
    sign,
    sign_id_token,
)

# The init service's own header name (deployment config) — distinct from the
# inbound `X-Id-Token` the FE sends us, which is fixed by our endpoint contract.
_INIT_AUTHORIZE_HEADER = "X-Sso-Id-Token"


def _real_key_manager() -> KeyManager:
    kek_b64 = base64.b64encode(os.urandom(32)).decode()
    encrypted_dek_b64 = KeyManager.wrap(kek_b64, os.urandom(32))
    return KeyManager(kek_b64=kek_b64, encrypted_dek_b64=encrypted_dek_b64)


def _build_service() -> PatService:
    # Mirrors composition.py's PAT branch construction exactly.
    return PatService(
        verifier=PatTokenVerifier(
            key=import_pat_public_key(public_pem(), "RS256"),
            algorithm="RS256",
            expected_iss=ISS,
            expected_aud=AUD,
            nt_claim=NT_CLAIM,
        ),
        cipher=PATCipher(_real_key_manager()),
        repo=FakeRepo(),
        cache=PatCache(
            fakeredis.FakeStrictRedis(decode_responses=True),
            ttl_seconds=41400,
            lock_ttl_seconds=10,
            tombstone_ttl_seconds=123,
        ),
        refresh_client=PatRefreshClient(
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            refresh_url="https://pat.example/refresh",
            header_key="X-Pat-Service-Token",
            header_value="secret",
            timeout=30.0,
        ),
        init_client=PatInitClient(
            # Mirrors production: the init service mints a PAT bound to the caller.
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"patToken": sign("alice")})
                )
            ),
            init_url="https://pat.example/api/pat/token",
            api_token_header_key="X-Pat-Init-Token",
            api_token_value="secret",
            authorize_header_key=_INIT_AUTHORIZE_HEADER,
            sso_header_key="X-Sso-Site",
            sso_site_url="https://sso.example",
            expire_days=360,
            timeout=30.0,
        ),
    )


def _app(service: PatService, upstream_handler) -> FastAPI:
    upstream = httpx.Client(transport=httpx.MockTransport(upstream_handler))
    app = FastAPI()
    app.include_router(
        create_pat_router(
            pat_service=service,
            token_manager=id_token_manager(),
            jwt_claim_user_id=JWT_CLAIM_USER_ID,
        )
    )
    app.include_router(
        create_brainagent_v1_router(
            http_client=upstream,
            brain_url="http://brain:8100",
            brain_key="k",
            # Mirrors composition.py's `_build_brain_agent_factory`: the resolved
            # PAT reaches the upstream only if it survives into BrainCaller's
            # extra_headers, so the real caller is what makes this a wiring test.
            agent_factory=lambda user_id, extra_headers=None: BrainAgent(
                BrainCaller(
                    http_client=upstream,
                    brain_url="http://brain:8100",
                    user_id=user_id,
                    brain_key="k",
                    extra_headers=extra_headers,
                )
            ),
            pat_service=service,
            pat_header_name="X-Pat-Token",
        )
    )
    return app


def _run_body() -> dict:
    return {
        "runId": "run_1",
        "threadId": "thread_1",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "tools": [],
        "state": None,
        "context": [],
        "forwardedProps": None,
    }


def _upstream(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["pat"] = request.headers.get("X-Pat-Token")
        return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})

    return handler


def test_authorize_then_pat_rides_the_run_path() -> None:
    service = _build_service()
    seen: dict = {}

    with TestClient(_app(service, _upstream(seen))) as client:
        auth = client.post(
            "/pat/v1/authorize",
            headers={"X-User-Id": "alice", ID_TOKEN_HEADER: sign_id_token("alice")},
        )
        assert auth.status_code == 204

        run = client.post("/brainagent/v1", json=_run_body(), headers={"X-User-Id": "alice"})
        assert run.status_code == 200

    # The PAT authorized in step 1 was resolved from cache and rode the run call.
    assert seen["pat"] is not None
    assert seen["pat"].startswith("eyJ")  # a JWT, i.e. the stored PAT round-tripped


def test_run_path_is_fail_open_without_authorization() -> None:
    service = _build_service()
    seen: dict = {}

    # bob never authorized → no PAT, but the run still goes through.
    with TestClient(_app(service, _upstream(seen))) as client:
        r = client.post("/brainagent/v1", json=_run_body(), headers={"X-User-Id": "bob"})
    assert r.status_code == 200
    assert seen["pat"] is None
