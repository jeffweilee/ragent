"""T-SEC.7 — `ragent_ingest_rejected_total{reason}` counter.

The three guard layers (magic-byte, zip preflight, PDF page-count cap) each
emit one increment with a closed `reason` label so operators can see in
Grafana whether a threshold is tuned too tight (and which one).

Closed label set keeps Prometheus cardinality bounded:
  magic | invalid | members | ratio | expanded | per_member | traversal | pdf_pages
"""

from __future__ import annotations

import io
import zipfile

import pytest
from prometheus_client import REGISTRY

from ragent.bootstrap.metrics import record_ingest_rejection


def _value(reason: str) -> float:
    return REGISTRY.get_sample_value("ragent_ingest_rejected_total", {"reason": reason}) or 0.0


# ---------------------------------------------------------------------------
# Direct helper increments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["magic", "invalid", "members", "ratio", "expanded", "per_member", "traversal", "pdf_pages"],
)
def test_record_ingest_rejection_increments_for_reason(reason: str) -> None:
    before = _value(reason)
    record_ingest_rejection(reason)
    assert _value(reason) == before + 1


def test_record_ingest_rejection_rejects_unknown_reason() -> None:
    """Closed label set blocks typos before they leak as new Prometheus series."""
    with pytest.raises(ValueError, match="unknown ingest-rejection reason"):
        record_ingest_rejection("bogus_reason_not_in_set")


# ---------------------------------------------------------------------------
# Emission from each guard module (integration)
# ---------------------------------------------------------------------------


def test_magic_byte_guard_emits_metric() -> None:
    from ragent.schemas.ingest import IngestMime
    from ragent.security.file_signature import MagicByteMismatchError, assert_magic_byte

    before = _value("magic")
    with pytest.raises(MagicByteMismatchError):
        assert_magic_byte(IngestMime.DOCX, b"NOT_A_ZIP")
    assert _value("magic") == before + 1


def test_zip_guard_invalid_emits_metric() -> None:
    from ragent.security.archive_guard import ArchiveBombError, assert_safe_zip

    before = _value("invalid")
    with pytest.raises(ArchiveBombError):
        assert_safe_zip(b"not a zip")
    assert _value("invalid") == before + 1


def test_zip_guard_members_emits_metric() -> None:
    from ragent.security.archive_guard import ArchiveBombError, assert_safe_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for i in range(20):
            zf.writestr(f"f{i}.txt", b"x")
    before = _value("members")
    with pytest.raises(ArchiveBombError):
        assert_safe_zip(buf.getvalue(), max_members=10)
    assert _value("members") == before + 1


def test_zip_guard_traversal_emits_metric() -> None:
    from ragent.security.archive_guard import ArchiveBombError, assert_safe_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("../evil", b"x")
    before = _value("traversal")
    with pytest.raises(ArchiveBombError):
        assert_safe_zip(buf.getvalue())
    assert _value("traversal") == before + 1


def test_zip_guard_per_member_emits_metric() -> None:
    from ragent.security.archive_guard import ArchiveBombError, assert_safe_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"\x00" * (3 * 1024 * 1024))
    before = _value("per_member")
    with pytest.raises(ArchiveBombError):
        assert_safe_zip(buf.getvalue(), max_ratio=10_000, max_expanded=1024 * 1024)
    assert _value("per_member") == before + 1


def test_zip_guard_expanded_emits_metric() -> None:
    from ragent.security.archive_guard import ArchiveBombError, assert_safe_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(3):
            zf.writestr(f"f{i}.bin", b"\x00" * (2 * 1024 * 1024))
    before = _value("expanded")
    with pytest.raises(ArchiveBombError):
        assert_safe_zip(buf.getvalue(), max_ratio=10_000, max_expanded=4 * 1024 * 1024)
    assert _value("expanded") == before + 1


def test_zip_guard_ratio_emits_metric() -> None:
    from ragent.security.archive_guard import ArchiveBombError, assert_safe_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\x00" * (5 * 1024 * 1024))
    before = _value("ratio")
    with pytest.raises(ArchiveBombError):
        assert_safe_zip(buf.getvalue(), max_ratio=100, max_expanded=50 * 1024 * 1024)
    assert _value("ratio") == before + 1


def test_pdf_page_count_guard_emits_metric() -> None:
    from ragent.security.archive_guard import PdfTooManyPagesError, assert_safe_pdf_page_count

    before = _value("pdf_pages")
    with pytest.raises(PdfTooManyPagesError):
        assert_safe_pdf_page_count(10, max_pages=2)
    assert _value("pdf_pages") == before + 1
