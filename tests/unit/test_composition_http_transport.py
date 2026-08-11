"""T-RETRY.2 — the shared httpx clients retry connection establishment only.

Application-level retry was removed from every upstream client (T-RETRY.1): a
timeout has already spent its whole budget and a 4xx is an answer, so neither
is worth re-asking. A failure to *establish* the connection is a different
class — the server never received the request, so retrying adds no load and
duplicates no side effect — and httpx's transport-level `retries` covers
exactly that case and nothing else.

The `verify` assertions are not incidental: passing a custom transport makes
`httpx.Client(verify=...)` a no-op, because the transport owns the TLS context.
Wiring the retry without moving `verify` across would silently disable
`RAGENT_TLS_VERIFY` — a security regression with no test to catch it.
"""

from __future__ import annotations

import ssl

import httpx
import pytest

from ragent.bootstrap.composition import _CONNECT_RETRIES, _http_client


def test_shared_http_client_retries_connection_establishment() -> None:
    client = _http_client(timeout=60.0, verify=True)
    assert client._transport._pool._retries == _CONNECT_RETRIES  # noqa: SLF001
    assert _CONNECT_RETRIES > 0


def test_http_client_honours_timeout() -> None:
    client = _http_client(timeout=10.0, verify=True)
    assert client.timeout.connect == 10.0
    assert client.timeout.read == 10.0


def test_http_client_keeps_tls_verification_on_by_default() -> None:
    """`verify=True` must reach the transport's SSL context, not just the
    Client that no longer builds one."""
    client = _http_client(timeout=60.0, verify=True)
    ctx = client._transport._pool._ssl_context  # noqa: SLF001
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_http_client_propagates_verify_false_to_the_transport() -> None:
    """RAGENT_TLS_VERIFY=false must still disable verification — proving the
    flag was not stranded on the Client when the transport took over."""
    client = _http_client(timeout=60.0, verify=False)
    ctx = client._transport._pool._ssl_context  # noqa: SLF001
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


# --- environment proxies (Codex PR #247 P1) ---------------------------------
#
# `httpx.Client` only reads HTTP_PROXY / HTTPS_PROXY / NO_PROXY when
# `transport is None` (`allow_env_proxies = trust_env and transport is None`,
# httpx 0.28.1). Handing it a retrying transport therefore silently drops every
# environment proxy — in a proxy-only network that breaks *all* outbound calls:
# AI, auth, PAT, chatagent, brain.
#
# These assert the property that actually matters: **we route exactly like stock
# httpx would**. Comparing against a plain Client built under the same env beats
# asserting specific patterns, because NO_PROXY semantics are httpx's to define
# (and the test host's own proxy config cannot skew a differential check).

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_PROBE_URLS = (
    "https://upstream.example/v1",
    "http://upstream.example/v1",
    "https://internal.example/v1",
    "https://sub.internal.example/v1",
)


def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)


def _routing(client: httpx.Client) -> dict[str, bool]:
    """url -> True when it goes through a proxy mount rather than direct."""
    return {
        url: client._transport_for_url(httpx.URL(url)) is not client._transport  # noqa: SLF001
        for url in _PROBE_URLS
    }


@pytest.mark.parametrize(
    "env",
    [
        {"HTTPS_PROXY": "http://proxy.example:8080"},
        {"HTTP_PROXY": "http://proxy.example:8080"},
        {"ALL_PROXY": "http://proxy.example:8080"},
        {"HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "internal.example"},
    ],
)
def test_http_client_routes_proxies_exactly_like_stock_httpx(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    _clear_proxy_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert _routing(_http_client(timeout=60.0, verify=True)) == _routing(httpx.Client())


def test_http_client_proxy_mount_keeps_tls_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy mount is a second transport — `verify` has to reach it too, or
    TLS silently downgrades for exactly the deployments that use a proxy."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

    client = _http_client(timeout=60.0, verify=True)

    proxied = client._transport_for_url(httpx.URL("https://upstream.example/v1"))  # noqa: SLF001
    assert proxied is not client._transport  # noqa: SLF001
    assert proxied._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED  # noqa: SLF001


def test_http_client_has_no_mounts_without_env_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)

    assert _http_client(timeout=60.0, verify=True)._mounts == {}  # noqa: SLF001


def test_connection_retries_do_not_apply_behind_a_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents an httpx limitation rather than our choice: `HTTPTransport`
    forwards `retries` only to the direct `ConnectionPool`, never to
    `httpcore.HTTPProxy`. So a proxied deployment gets no connection retry.

    That is not a regression — the pre-T-RETRY client had `retries=0`
    everywhere — but it means the retry we kept is a direct-connection benefit
    only. Pinned so an httpx upgrade that starts honouring it shows up here.
    """
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

    client = _http_client(timeout=60.0, verify=True)

    assert client._transport._pool._retries == _CONNECT_RETRIES  # noqa: SLF001
    proxied = client._transport_for_url(httpx.URL("https://upstream.example/v1"))  # noqa: SLF001
    assert proxied._pool._retries == 0  # noqa: SLF001
