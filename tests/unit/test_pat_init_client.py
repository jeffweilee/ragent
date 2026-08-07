"""T-PAT.16 — PatInitClient request shape + typed error mapping.

The init service mints a brand-new PAT for a user (on-behalf-of the inbound SSO
id token), replacing the FE-manual `{patToken}` body. Request shape and the
HTTP-status → typed-error map are pinned here (MockTransport, no real network).
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from ragent.clients.pat_init_client import (
    PatInitBadRequest,
    PatInitClient,
    PatInitRateLimited,
    PatInitTransient,
    PatInitUnauthorized,
)

_URL = "https://pat.example/api/pat/token"


def _client(
    handler, *, expire_days: int = 360, today: date | None = None, init_url: str = _URL
) -> PatInitClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs = {"clock": (lambda: today)} if today is not None else {}
    return PatInitClient(
        http,
        init_url=init_url,
        api_token_header_key="X-Pat-Init-Token",
        api_token_value="svc-secret",
        authorize_header_key="X-Id-Token",
        sso_header_key="X-Sso-Site",
        sso_site_url="https://sso.example",
        expire_days=expire_days,
        timeout=5.0,
        **kwargs,
    )


def test_success_returns_token_and_sends_expected_shape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["api_token"] = request.headers.get("X-Pat-Init-Token")
        seen["id_token"] = request.headers.get("X-Id-Token")
        seen["sso"] = request.headers.get("X-Sso-Site")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"patToken": "NEW"})

    minted = _client(handler, expire_days=360, today=date(2026, 8, 3)).init("ID-TOKEN")

    assert minted.token == "NEW"
    # T-PAT.25: the window reported back is the one actually sent, not a second
    # computation — what gets persisted can never drift from what init was asked
    # for. Same value as `seen["body"]["expireDate"]` asserted below.
    assert minted.expire_date == date(2027, 7, 29)
    assert seen["method"] == "POST"
    assert seen["url"] == _URL
    assert seen["api_token"] == "svc-secret"
    assert seen["id_token"] == "ID-TOKEN"  # the inbound SSO id token, forwarded
    assert seen["sso"] == "https://sso.example"
    assert seen["content_type"] == "application/json"
    # 2026-08-03 + 360 days = 2027-07-29, formatted YYYY/MM/DD (init contract).
    assert seen["body"] == {"expireDate": "2027/07/29"}


def test_posts_to_the_configured_url_verbatim() -> None:
    """`PAT_INIT_API_URL` is the FULL mint URL (same shape as `PAT_REFRESH_API`);
    the client does not append or rewrite any path."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"patToken": "NEW"})

    _client(handler, init_url="https://other.example/v2/mint").init("ID")

    assert seen["url"] == "https://other.example/v2/mint"


@pytest.mark.parametrize(
    "bad_url",
    ["https://pat.example", "https://pat.example/", "http://host:8080", "https://pat.example//"],
)
def test_url_without_a_path_is_refused_at_construction(bad_url: str) -> None:
    """The trap this env shape introduces: an operator who leaves the old BASE
    url in place would otherwise get 404s from init, which map to
    `PatInitUnavailable` — i.e. "transiently unavailable" forever. Fail at boot
    with a message naming the variable instead."""
    with pytest.raises(ValueError, match="PAT_INIT_API_URL"):
        _client(lambda r: httpx.Response(200, json={"patToken": "NEW"}), init_url=bad_url)


def test_expire_date_stays_within_one_year() -> None:
    """The margin (default 360 days) keeps the request under the init API's
    one-year ceiling regardless of the request date."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["expireDate"] = json.loads(request.content)["expireDate"]
        return httpx.Response(200, json={"patToken": "NEW"})

    _client(handler, expire_days=360, today=date(2026, 1, 1)).init("ID")

    target = date.fromisoformat(captured["expireDate"].replace("/", "-"))
    assert (target - date(2026, 1, 1)).days == 360
    assert target < date(2027, 1, 1)  # strictly within one year


@pytest.mark.parametrize("days", [0, -1, 366, 400])
def test_out_of_range_expire_days_is_refused_at_construction(days: int) -> None:
    """A misconfigured PAT_INIT_EXPIRE_DAYS must abort boot, not 500 every mint:
    the init API rejects an expireDate more than a year out."""
    with pytest.raises(ValueError, match="PAT_INIT_EXPIRE_DAYS"):
        _client(lambda req: httpx.Response(200, json={"patToken": "N"}), expire_days=days)


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, PatInitUnauthorized),
        (400, PatInitBadRequest),
        (429, PatInitRateLimited),
        (500, PatInitTransient),
        (503, PatInitTransient),
    ],
)
def test_status_maps_to_typed_error(status: int, exc: type[Exception]) -> None:
    client = _client(lambda req: httpx.Response(status, json={}))
    with pytest.raises(exc):
        client.init("ID")


def test_transport_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(PatInitTransient):
        _client(handler).init("ID")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"nope": 1}),  # KeyError — wrong field
        httpx.Response(200, text="<html>gateway</html>"),  # ValueError — not JSON at all
        httpx.Response(200, json=[1, 2]),  # TypeError — JSON, but not an object
    ],
    ids=["wrong-field", "not-json", "not-an-object"],
)
def test_malformed_200_body_is_transient(response: httpx.Response) -> None:
    """A proxy/WAF can answer 200 with an HTML error page; that must not be
    mistaken for a mint."""
    client = _client(lambda req: response)
    with pytest.raises(PatInitTransient):
        client.init("ID")


@pytest.mark.parametrize("token", ["", None, 123])
def test_200_with_unusable_pat_token_is_transient(token: object) -> None:
    """A 200 whose patToken is empty or not a string is as useless as a 5xx —
    never return it as if it were a real PAT."""
    client = _client(lambda req: httpx.Response(200, json={"patToken": token}))
    with pytest.raises(PatInitTransient):
        client.init("ID")
