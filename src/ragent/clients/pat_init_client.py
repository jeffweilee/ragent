"""PatInitClient — mints a fresh PAT via the init service (T-PAT).

`POST {PAT_INIT_API_URL}` — the FULL mint endpoint, same shape as
`PAT_REFRESH_API` — with three headers: the service
credential (`PAT_INIT_API_TOKEN_HEADER_KEY_NAME`), the inbound SSO id token
forwarded on-behalf-of the user (`PAT_INIT_AUTHORIZE_HEADER_KEY_NAME`), and the
SSO site url (`PAT_INIT_SSO_HEADER_KEY_NAME`) — plus body `{"expireDate": …}`
(a date within one year) → `{"patToken": <new>}`.

The HTTP status is mapped to a typed error so `PatService.authorize` can react:
401 → re-authorize (bad id / api token), 400 → our bug (expireDate > 1y / empty
body), 429 → rate limited (init caps 10/60s per client+nt), anything else /
transport failure / malformed 200 → transient.

Replaces the FE-manual `{patToken}` submission: ragent now fetches the PAT
itself; downstream keeps it and refreshes via `PatRefreshClient` (init is not
re-called per request — it is rate limited on purpose).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from ragent.utility.datetime import utcnow

logger = structlog.get_logger(__name__)

# The init API rejects an expireDate more than a year out with a 400. Capped a
# day short of the anniversary: we compute the date in UTC while the init
# service evaluates the ceiling in its own timezone, so `today + 365` can land
# on the far side of the boundary.
_MAX_EXPIRE_DAYS = 364


@dataclass(frozen=True)
class MintedPat:
    """A freshly minted PAT together with the window it was minted for.

    `expire_date` is the value this client actually sent as `expireDate`, not a
    recomputation — so what `PatService` persists as the authorization window and
    what the upstream was asked for can never drift apart. A named pair rather
    than a tuple: both fields are read at the storage site, where positional
    unpacking would be easy to get backwards.
    """

    token: str
    expire_date: date


class PatInitError(Exception):
    """Base for all init outcomes other than success."""


class PatInitUnauthorized(PatInitError):
    """401 — the id token or the service api token is invalid."""


class PatInitBadRequest(PatInitError):
    """400 — expireDate > 1 year / empty body; a caller-side bug."""


class PatInitRateLimited(PatInitError):
    """429 — the init service is rate-limiting (10/60s per client+nt)."""


class PatInitTransient(PatInitError):
    """5xx / transport failure / malformed 200 — retry later."""


def _utc_today() -> date:
    return utcnow().date()


class PatInitClient:
    def __init__(
        self,
        http_client: httpx.Client,
        *,
        init_url: str,
        api_token_header_key: str,
        api_token_value: str,
        authorize_header_key: str,
        sso_header_key: str,
        sso_site_url: str,
        expire_days: int,
        timeout: float = 30.0,
        clock: Callable[[], date] = _utc_today,
    ) -> None:
        # Fail at composition rather than 500-ing every mint: the init API
        # rejects an expireDate beyond one year with a 400.
        if not 0 < expire_days <= _MAX_EXPIRE_DAYS:
            raise ValueError(
                f"PAT_INIT_EXPIRE_DAYS must be in 1..{_MAX_EXPIRE_DAYS} (got {expire_days}) — "
                "the init API rejects an expireDate more than a year out"
            )
        # A path-less URL is almost certainly a leftover BASE url from before
        # this variable carried the full endpoint. Left alone it would 404 on
        # every mint, and a 404 maps to `PatInitTransient` — i.e. the slice
        # would report "init transiently unavailable" forever. Fail at boot.
        if urlparse(init_url).path.strip("/") == "":
            raise ValueError(
                "PAT_INIT_API_URL must be the FULL mint endpoint, not a base URL "
                f"(got {init_url!r}) — e.g. https://pat.example/api/pat/token"
            )
        self._http = http_client
        self._url = init_url
        # The two service-level headers never vary; only the id token is per-call.
        self._static_headers = {
            api_token_header_key: api_token_value,
            sso_header_key: sso_site_url,
        }
        self._authorize_header_key = authorize_header_key
        self._expire_days = expire_days
        self._timeout = timeout
        self._clock = clock

    def init(self, id_token: str) -> MintedPat:
        """Return a freshly minted PAT plus its window, or raise ``PatInitError``."""
        headers = {**self._static_headers, self._authorize_header_key: id_token}
        expire_date = self._expire_date()
        try:
            resp = self._http.post(
                self._url,
                json={"expireDate": expire_date.strftime("%Y/%m/%d")},
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            logger.warning("pat.init_transport_error", error_type=type(exc).__name__)
            raise PatInitTransient(str(exc)) from exc

        if resp.status_code == 200:
            return MintedPat(token=self._extract(resp), expire_date=expire_date)
        if resp.status_code == 401:
            raise PatInitUnauthorized()
        if resp.status_code == 400:
            raise PatInitBadRequest()
        if resp.status_code == 429:
            raise PatInitRateLimited()
        logger.warning("pat.init_unexpected_status", http_status=resp.status_code)
        raise PatInitTransient(f"unexpected status {resp.status_code}")

    def _expire_date(self) -> date:
        """The end of the authorization window: `expire_days` out (a small margin
        under one year avoids the API's 400-on-`> 1y`). Serialised `YYYY/MM/DD`
        for the wire by the caller, and carried on `MintedPat` so the persisted
        window is the same value."""
        return self._clock() + timedelta(days=self._expire_days)

    @staticmethod
    def _extract(resp: httpx.Response) -> str:
        try:
            body: Any = resp.json()
            token = body["patToken"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PatInitTransient("malformed init response body") from exc
        if not isinstance(token, str) or not token:
            raise PatInitTransient("init response carried no patToken")
        return token
