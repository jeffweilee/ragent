"""Shared fakes/builders for PatService unit tests (T-PAT.7/8/9).

Uses the REAL cipher, verifier, and cache (over fakeredis) so validity, TTL, and
the refresh lock behave as in production; only the repository and the refresh
HTTP client are faked.
"""

from __future__ import annotations

import os
import time
from typing import Any

import fakeredis
from joserfc import jwt as _jwt
from joserfc.jwk import RSAKey

from ragent.auth.pat_jwt import PatTokenVerifier
from ragent.clients.pat_cache import PatCache
from ragent.security.pat_cipher import PATCipher
from ragent.services.pat_service import PatService

ISS = "https://sso.example/pat"
AUD = "ragent-agent"
NT_CLAIM = "nt"

_KEY = RSAKey.generate_key(2048)


def sign(nt: str = "alice", *, exp_delta: int = 3600) -> str:
    """A signed PAT for ``nt``; ``exp_delta`` < 0 makes it already-expired."""
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "exp": now + exp_delta, "nt": nt}
    return _jwt.encode({"alg": "RS256"}, claims, _KEY)


def public_pem() -> str:
    """The PEM public key matching :func:`sign` — feed to ``import_pat_public_key``."""
    return _KEY.as_pem(private=False).decode()


class _StubKeyManager:
    def __init__(self) -> None:
        self.dek = os.urandom(32)


class FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.mark_invalid_calls: list[str] = []

    async def upsert(self, *, user_id: str, pat_cipher: str) -> None:
        self.rows[user_id] = {"user_id": user_id, "pat_cipher": pat_cipher, "status": "active"}

    async def get(self, *, user_id: str):
        return self.rows.get(user_id)

    async def mark_invalid(self, *, user_id: str) -> int:
        self.mark_invalid_calls.append(user_id)
        if user_id in self.rows:
            self.rows[user_id]["status"] = "invalid"
            return 1
        return 0


class FakeRefreshClient:
    """`script` items are either a new-token str (success) or an Exception to raise."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[str] = []

    def refresh(self, current_token: str) -> str:
        self.calls.append(current_token)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def _no_sleep(_seconds: float) -> None:
    return None


def build_service(
    *,
    repo: FakeRepo | None = None,
    refresh_client: FakeRefreshClient | None = None,
    cache: PatCache | None = None,
    cipher: PATCipher | None = None,
    max_retries: int = 3,
) -> tuple[PatService, FakeRepo, PatCache, PATCipher]:
    repo = repo if repo is not None else FakeRepo()
    cipher = cipher if cipher is not None else PATCipher(_StubKeyManager())
    cache = (
        cache
        if cache is not None
        else PatCache(
            fakeredis.FakeStrictRedis(decode_responses=True), ttl_seconds=41400, lock_ttl_seconds=10
        )
    )
    verifier = PatTokenVerifier(
        key=RSAKey.import_key(_KEY.as_pem(private=False)),
        algorithm="RS256",
        expected_iss=ISS,
        expected_aud=AUD,
        nt_claim=NT_CLAIM,
    )
    service = PatService(
        verifier=verifier,
        cipher=cipher,
        repo=repo,
        cache=cache,
        refresh_client=refresh_client if refresh_client is not None else FakeRefreshClient([]),
        max_retries=max_retries,
        backoff_base_seconds=0.0,
        sleeper=_no_sleep,
    )
    return service, repo, cache, cipher
