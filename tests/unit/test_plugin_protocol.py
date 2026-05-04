"""Phase 1 W2 — Plugin Protocol v1 contract tests (spec §5 S4, S5)."""

from ragent.plugins import ExtractorPlugin
from ragent.plugins.stub_graph import StubGraphExtractor


class _GoodPlugin:
    name = "good"
    required = False
    queue = "extract.good"

    def extract(self, document_id: str) -> None:
        return None

    def delete(self, document_id: str) -> None:
        return None

    def health(self) -> bool:
        return True


class _BadMissingHealth:
    name = "bad"
    required = False
    queue = "extract.bad"

    def extract(self, document_id: str) -> None: ...
    def delete(self, document_id: str) -> None: ...


def test_protocol_accepts_conforming_class() -> None:
    assert isinstance(_GoodPlugin(), ExtractorPlugin)


def test_protocol_rejects_class_missing_method() -> None:
    assert not isinstance(_BadMissingHealth(), ExtractorPlugin)


def test_stub_graph_extractor_is_noop_and_healthy() -> None:
    plugin = StubGraphExtractor()
    assert plugin.name == "graph_stub"
    assert plugin.required is False
    assert plugin.queue == "extract.graph"
    assert plugin.extract("doc_123") is None
    assert plugin.delete("doc_123") is None
    assert plugin.health() is True
    assert isinstance(plugin, ExtractorPlugin)
