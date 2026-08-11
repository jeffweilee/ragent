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

from haystack_integrations.document_stores.elasticsearch import (
    document_store as document_store_module,
)


def test_es_client_is_built_with_retries_disabled() -> None:
    """Pins the kwarg, not the library default — an es-py upgrade that changes
    the default must not silently change ragent's behaviour."""
    from ragent.bootstrap.composition import _es_client

    client = _es_client(hosts=["http://es.example:9200"], basic_auth=None, verify_certs=False)

    # Private attribute by necessity: es-py 8.19.3 keeps the per-client request
    # option here and only merges it into the transport at request time, so
    # there is no public surface that reports the effective value.
    assert client._max_retries == 0  # noqa: SLF001


def test_document_store_also_disables_retries() -> None:
    """The retrieval pipeline searches through `ElasticsearchDocumentStore`'s own
    internally-constructed client, not the standalone `es_client` — so disabling
    retries on one and not the other leaves the **user-facing** chat path still
    retrying 429/502/503/504 (Codex review PR #247 P2)."""
    from unittest.mock import MagicMock, patch

    from elasticsearch import Elasticsearch

    from ragent.bootstrap.composition import _document_store

    store = _document_store(
        hosts=["http://es.example:9200"],
        index="chunks_v1_active",
        basic_auth=None,
        verify_certs=False,
    )

    # The store defers client construction to first use, so drive it with the
    # constructor patched — that proves the kwarg is actually *forwarded*, not
    # merely parked on `_kwargs` where a future refactor could drop it. No
    # network: the real `client` property pings ES.
    with patch.object(
        document_store_module, "Elasticsearch", MagicMock(spec=Elasticsearch)
    ) as es_ctor:
        _ = store.client

    assert es_ctor.call_args.kwargs["max_retries"] == 0


def test_es_client_default_would_have_retried() -> None:
    """Guards the premise: if a future es-py stops defaulting to 3 retries this
    test fails, and the explicit kwarg above can be re-justified rather than
    cargo-culted."""
    import inspect

    from elastic_transport import Transport

    default = inspect.signature(Transport.__init__).parameters["max_retries"].default
    assert default == 3
