"""PatTokenVerifier — static-PEM verification of a Personal Access Token (T-PAT).

Unlike `VerifyingTokenManager` (JWKS-backed via OIDC discovery, `auth/jwt.py`),
a PAT is verified against a **single static public key** read from
`PAT_PUBLIC_KEY` at composition (a PEM, or the headerless one-line base64 DER
SSO services often hand out — see `import_pat_public_key`). The PAT is an
SSO-signed JWT; a valid one has
an unexpired `exp`, `iss == PAT_ISS`, `aud == PAT_AUD`, and a signature that
verifies under the configured key/algorithm.

Every verification failure collapses to one typed error, `PatTokenInvalid`
(carrying `PAT_REAUTH_REQUIRED` / 401) — the PAT surface has a single remedy
(re-authorize), so it does not need the SSO path's granular expired/invalid/
claim-missing split.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, Key, OKPKey, RSAKey
from joserfc.jwt import JWTClaimsRegistry

from ragent.errors.codes import HttpErrorCode


def import_pat_public_key(value: str, algorithm: str) -> Key:
    """Load the static PAT public key for the configured JWS algorithm.

    Accepts EITHER a full PEM (``-----BEGIN PUBLIC KEY-----`` …) OR the
    headerless one-line base64 DER (SubjectPublicKeyInfo) that SSO services
    commonly hand out — the latter cannot be a multi-line PEM inside a ``.env``
    file, so it is normalised to DER bytes here. Literal ``\\n`` escapes (a
    common env-file pitfall) are un-escaped first.

    Picks the joserfc key class by algorithm family (`RS*`/`PS*` → RSA, `ES*` →
    EC, `EdDSA` → OKP) so an operator can run any asymmetric signer, not only
    the default RS256."""
    text = value.strip().replace("\\n", "\n")
    key_material: str | bytes = text
    if "-----BEGIN" not in text:
        try:
            key_material = base64.b64decode(text, validate=True)
        except ValueError as exc:
            raise ValueError("PAT_PUBLIC_KEY is neither a PEM nor a base64 DER public key") from exc

    if algorithm.startswith(("RS", "PS")):
        return RSAKey.import_key(key_material)
    if algorithm.startswith("ES"):
        return ECKey.import_key(key_material)
    if algorithm == "EdDSA":
        return OKPKey.import_key(key_material)
    raise ValueError(f"unsupported PAT_JWT_ALG {algorithm!r}")


@dataclass
class PatTokenInvalid(Exception):
    """Raised on any PAT verification / nt-binding failure.

    Not ``frozen`` — Python assigns ``__traceback__`` during ``raise ... from``.
    """

    error_code: HttpErrorCode = HttpErrorCode.PAT_REAUTH_REQUIRED
    http_status: int = 401

    def __str__(self) -> str:
        return self.error_code


@dataclass(frozen=True)
class PatTokenVerifier:
    """Verifies a PAT JWT against a single static public key (built once)."""

    key: Key
    algorithm: str
    expected_iss: str
    expected_aud: str
    nt_claim: str

    def verify(self, token: str) -> dict[str, Any]:
        """Return the PAT's claims, or raise ``PatTokenInvalid``.

        Signature + algorithm are checked by ``jwt.decode``; ``exp``/``aud`` by
        the claims registry; ``iss`` by a direct compare (trailing-slash-tolerant,
        matching the SSO path)."""
        if not token:
            raise PatTokenInvalid()
        try:
            decoded = jwt.decode(token, self.key, algorithms=[self.algorithm])
        except (JoseError, ValueError) as exc:
            raise PatTokenInvalid() from exc

        claims = decoded.claims
        try:
            # `exp` is essential: joserfc only runs the expiry validator when the
            # claim is present, so without this a PAT that omits `exp` would
            # verify forever and resolve() would never refresh it (contradicting
            # the 12 h lifetime — Codex review r3619473868).
            JWTClaimsRegistry(
                exp={"essential": True},
                aud={"essential": True, "value": self.expected_aud},
            ).validate(claims)
        except JoseError as exc:
            raise PatTokenInvalid() from exc

        if str(claims.get("iss") or "").rstrip("/") != self.expected_iss.rstrip("/"):
            raise PatTokenInvalid()
        return claims

    def nt_of(self, claims: dict[str, Any]) -> str:
        """Extract the SSO nt the PAT is bound to (``PAT_NT_KEY_NAME`` claim)."""
        nt = claims.get(self.nt_claim)
        if not isinstance(nt, str) or not nt:
            raise PatTokenInvalid()
        return nt
