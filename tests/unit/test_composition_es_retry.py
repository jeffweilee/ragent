"""T-RETRY.4 — the Elasticsearch client's retry policy is explicit, not inherited.

`Elasticsearch(hosts=..., basic_auth=..., verify_certs=...)` leaves
`max_retries` at the library default, which `elastic_transport` resolves to 3
with `retry_on_status=(429, 502, 503, 504)`. That is a retry layer no call site
can see and no reviewer can find by reading composition — and it multiplies with
anything wrapping it. T-RETRY.1 removed application-level retry from every
upstream client; leaving an invisible one under ES would make the codebase say
one thing and do another.
"""

from __future__ import annotations


def test_es_client_is_built_with_retries_disabled() -> None:
    """Pins the kwarg, not the library default — an es-py upgrade that changes
    the default must not silently change ragent's behaviour."""
    from ragent.bootstrap.composition import _es_client

    client = _es_client(hosts=["http://es.example:9200"], basic_auth=None, verify_certs=False)

    # Private attribute by necessity: es-py 8.19.3 keeps the per-client request
    # option here and only merges it into the transport at request time, so
    # there is no public surface that reports the effective value.
    assert client._max_retries == 0  # noqa: SLF001


def test_es_client_default_would_have_retried() -> None:
    """Guards the premise: if a future es-py stops defaulting to 3 retries this
    test fails, and the explicit kwarg above can be re-justified rather than
    cargo-culted."""
    import inspect

    from elastic_transport import Transport

    default = inspect.signature(Transport.__init__).parameters["max_retries"].default
    assert default == 3
