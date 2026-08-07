"""Shared fakes/builders for PatService unit tests (T-PAT.7/8/9).

Uses the REAL cipher, verifier, and cache (over fakeredis) so validity, TTL, and
the refresh lock behave as in production; only the repository and the refresh
HTTP client are faked.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

import fakeredis
from joserfc import jwt as _jwt
from joserfc.jwk import KeySet, RSAKey

from ragent.auth.jwt import VerifyingTokenManager
from ragent.auth.pat_jwt import PatTokenVerifier
from ragent.clients.pat_cache import PatCache
from ragent.clients.pat_init_client import MintedPat
from ragent.security.pat_cipher import PATCipher
from ragent.services.pat_service import PatService
from ragent.utility.datetime import utcnow

ISS = "https://sso.example/pat"
AUD = "ragent-agent"
NT_CLAIM = "nt"

# The SSO id token the FE sends on POST /pat/v1/authorize is a DIFFERENT token
# from the PAT: same IdP, but the OIDC issuer/audience and the username claim
# the auth middleware reads. Kept distinct from ISS/AUD so a test that mixes
# them up fails instead of silently passing.
OIDC_ISS = "https://sso.example/realms/corp"
OIDC_AUD = "ragent"
JWT_CLAIM_USER_ID = "preferred_username"

_KEY = RSAKey.generate_key(2048)


def sign_id_token(
    username: str = "alice",
    *,
    exp_delta: int = 3600,
    iss: str = OIDC_ISS,
    aud: str = OIDC_AUD,
    claim: str = JWT_CLAIM_USER_ID,
) -> str:
    """An SSO id token for ``username``; override a kwarg to forge a bad one."""
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "exp": now + exp_delta, claim: username}
    return _jwt.encode({"alg": "RS256"}, claims, _KEY)


def id_token_manager() -> VerifyingTokenManager:
    """A real `VerifyingTokenManager` over :func:`sign_id_token`'s key — the
    same object `build_token_manager` produces at composition."""
    return VerifyingTokenManager(
        jwks=KeySet([_KEY]),
        audience=OIDC_AUD,
        expected_iss=OIDC_ISS,
    )


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

    async def upsert(
        self, *, user_id: str, pat_cipher: str, authorization_expires_at: date | None = None
    ) -> None:
        self.rows[user_id] = {
            "user_id": user_id,
            "pat_cipher": pat_cipher,
            "status": "active",
            "authorized_at": utcnow(),
            "authorization_expires_at": authorization_expires_at,
        }

    async def rotate(self, *, user_id: str, pat_cipher: str) -> int:
        """UPDATE-only, mirroring the real repo: a revoked row is NOT recreated."""
        row = self.rows.get(user_id)
        if row is None:
            return 0
        row["pat_cipher"] = pat_cipher
        row["status"] = "active"
        return 1

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


class FakeInitClient:
    """Returns ``result`` (a minted PAT str) or raises it when it is an Exception.

    Records the forwarded id token so authorize's on-behalf-of wiring is checked.
    A str result is wrapped in `MintedPat` with `expire_date`, mirroring the real
    client which reports the window it actually asked init for."""

    def __init__(self, result: Any, *, expire_date: date | None = None) -> None:
        self._result = result
        self.expire_date = expire_date if expire_date is not None else date(2027, 7, 21)
        self.calls: list[str] = []

    def init(self, id_token: str) -> MintedPat:
        self.calls.append(id_token)
        if isinstance(self._result, Exception):
            raise self._result
        return MintedPat(token=self._result, expire_date=self.expire_date)


async def _no_sleep(_seconds: float) -> None:
    return None


def build_service(
    *,
    repo: FakeRepo | None = None,
    refresh_client: FakeRefreshClient | None = None,
    init_client: FakeInitClient | None = None,
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
        init_client=init_client if init_client is not None else FakeInitClient(sign()),
        max_retries=max_retries,
        backoff_base_seconds=0.0,
        sleeper=_no_sleep,
    )
    return service, repo, cache, cipher
