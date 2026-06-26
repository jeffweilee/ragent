"""T-CAT.9 — _CsvASTSplitter: CSV → structured rows (unit, mocked)."""

from haystack.dataclasses import Document

from ragent.pipelines.ingest.splitter import _CsvASTSplitter


class TestCsvASTSplitter:
    """CSV splitter: one row per document atom, preserving headers."""

    def test_split_basic_csv_with_header(self):
        """Parse a simple 3-row CSV with header; emit one doc per row."""
        csv_content = """name,age,city
alice,30,NYC
bob,25,LA"""
        doc = Document(content=csv_content, meta={"mime_type": "text/csv"})

        splitter = _CsvASTSplitter()
        result = splitter.run([doc])

        atoms = result["documents"]
        assert len(atoms) == 2, "Should emit one doc per data row (not header)"
        assert atoms[0].content == "name: alice, age: 30, city: NYC"
        assert atoms[1].content == "name: bob, age: 25, city: LA"

    def test_csv_preserves_parent_metadata(self):
        """Atoms inherit parent metadata from original document."""
        csv_content = """product,price
widget,9.99
gadget,19.99"""
        doc = Document(content=csv_content, meta={"mime_type": "text/csv", "source": "test"})

        splitter = _CsvASTSplitter()
        result = splitter.run([doc])

        atoms = result["documents"]
        assert atoms[0].meta.get("mime_type") == "text/csv"
        assert atoms[0].meta.get("source") == "test"
        assert atoms[1].meta.get("mime_type") == "text/csv"
        assert atoms[1].meta.get("source") == "test"

    def test_csv_preserves_mime_type_meta(self):
        """Atoms inherit mime_type from the original doc."""
        csv_content = "a,b\n1,2"
        doc = Document(content=csv_content, meta={"mime_type": "text/csv"})

        splitter = _CsvASTSplitter()
        result = splitter.run([doc])

        atoms = result["documents"]
        assert atoms[0].meta.get("mime_type") == "text/csv"

    def test_csv_skip_empty_rows(self):
        """Skip rows with only whitespace."""
        csv_content = """x,y
1,2

3,4"""
        doc = Document(content=csv_content, meta={"mime_type": "text/csv"})

        splitter = _CsvASTSplitter()
        result = splitter.run([doc])

        atoms = result["documents"]
        assert len(atoms) == 2, "Should skip empty row"

    def test_csv_with_quoted_fields(self):
        """Handle quoted fields with embedded delimiters/newlines (RFC 4180)."""
        csv_content = '''name,description
alice,"works in NYC, loves coding"
bob,"multi-line:
comment here"'''
        doc = Document(content=csv_content, meta={"mime_type": "text/csv"})

        splitter = _CsvASTSplitter()
        result = splitter.run([doc])

        atoms = result["documents"]
        assert len(atoms) == 2
        assert "works in NYC, loves coding" in atoms[0].content
        assert "multi-line" in atoms[1].content
