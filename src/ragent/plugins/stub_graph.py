"""StubGraphExtractor — Phase 1 placeholder (spec §5 S5).

Conforms to ExtractorPlugin so the Chat-side fallback path works on day 1
without implying any real graph capability. Replaced in Phase 3 (plan §8.2).
"""


class StubGraphExtractor:
    name = "graph_stub"
    required = False
    queue = "extract.graph"

    def extract(self, document_id: str) -> None:
        return None

    def delete(self, document_id: str) -> None:
        return None

    def health(self) -> bool:
        return True
