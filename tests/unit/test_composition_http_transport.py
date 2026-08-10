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
