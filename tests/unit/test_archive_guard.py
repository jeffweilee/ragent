"""T-SEC.3 — Zip-archive preflight (assert_safe_zip).

Defends DOCX/PPTX splitters against archive-decompression bombs before
python-docx / python-pptx open the file.  Five rejection modes:

1. Too many zip members (millions-of-tiny-files bomb).
2. Declared-uncompressed / raw size ratio too high (classic 42.zip).
3. Total declared uncompressed size exceeds the absolute cap (padded
   input attempting to game the ratio check).
4. Any single member's declared size exceeds the per-member cap.
5. Path traversal in member name (`..` segment or leading `/`).

All checks read only the zip central directory — no member is inflated.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from ragent.security.archive_guard import (
    INGEST_MAX_ARCHIVE_EXPANDED_BYTES,
    INGEST_MAX_ARCHIVE_MEMBERS,
    INGEST_MAX_ARCHIVE_RATIO,
    ArchiveBombError,
    ArchiveBombReason,
    assert_safe_zip,
)


def _build_zip(members: list[tuple[str, bytes]], *, compress: bool = True) -> bytes:
    """Build an in-memory zip from (name, payload) pairs."""
    buf = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, mode="w", compression=method) as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_normal_zip_passes():
    raw = _build_zip([("word/document.xml", b"<xml>hello</xml>")])
    assert_safe_zip(raw)  # does not raise


def test_normal_zip_with_many_small_members_passes():
    members = [(f"f{i}.txt", b"x") for i in range(50)]
    raw = _build_zip(members)
    assert_safe_zip(raw)


# ---------------------------------------------------------------------------
# Rejection: error_code + http_status contract
# ---------------------------------------------------------------------------


def test_archive_bomb_error_carries_http_status_and_error_code():
    raw = _build_zip([(f"f{i}.txt", b"") for i in range(10)])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw, max_members=5)
    exc = exc_info.value
    assert exc.http_status == 413
    assert exc.error_code == "INGEST_ARCHIVE_UNSAFE"
    assert exc.reason == ArchiveBombReason.MEMBERS


# ---------------------------------------------------------------------------
# Rejection: 1) too many members
# ---------------------------------------------------------------------------


def test_too_many_members_rejected():
    raw = _build_zip([(f"f{i}.txt", b"x") for i in range(20)])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw, max_members=10)
    assert exc_info.value.reason == "members"


def test_member_count_at_cap_passes():
    raw = _build_zip([(f"f{i}.txt", b"x") for i in range(10)])
    assert_safe_zip(raw, max_members=10)


# ---------------------------------------------------------------------------
# Rejection: 2) compression ratio too high
# ---------------------------------------------------------------------------


def test_high_ratio_rejected():
    """5 MB of zeros compresses to ~5 KB → ratio ~1000."""
    payload = b"\x00" * (5 * 1024 * 1024)
    raw = _build_zip([("bomb.bin", payload)])
    assert len(raw) < len(payload) // 100  # sanity: ratio > 100
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw, max_ratio=100, max_expanded=50 * 1024 * 1024)
    assert exc_info.value.reason == "ratio"


def test_low_ratio_passes():
    """Incompressible payload yields ratio ~1; passes."""
    import os

    payload = os.urandom(64 * 1024)
    raw = _build_zip([("rand.bin", payload)], compress=False)
    assert_safe_zip(raw, max_ratio=100)


# ---------------------------------------------------------------------------
# Rejection: 3) total expanded size > absolute cap
# ---------------------------------------------------------------------------


def test_total_expanded_over_cap_rejected():
    """Declared uncompressed sum exceeds absolute cap independent of ratio."""
    payload = b"\x00" * (2 * 1024 * 1024)
    raw = _build_zip([(f"f{i}.bin", payload) for i in range(3)])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw, max_ratio=10_000, max_expanded=4 * 1024 * 1024)
    assert exc_info.value.reason == "expanded"


# ---------------------------------------------------------------------------
# Rejection: 4) single member exceeds per-member cap
# ---------------------------------------------------------------------------


def test_single_oversized_member_rejected():
    """One huge member alongside small ones — per-member check fires first
    inside the loop (before total is accumulated), defeating 'one giant +
    many small' bomb shapes."""
    payload = b"\x00" * (5 * 1024 * 1024)
    raw = _build_zip([("big.bin", payload), ("small.txt", b"hi")])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw, max_ratio=10_000, max_expanded=1 * 1024 * 1024)
    assert exc_info.value.reason == ArchiveBombReason.PER_MEMBER


# ---------------------------------------------------------------------------
# Rejection: 5) path traversal in member name
# ---------------------------------------------------------------------------


def test_path_traversal_dotdot_rejected():
    raw = _build_zip([("../evil.txt", b"x")])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw)
    assert exc_info.value.reason == "traversal"


def test_path_traversal_absolute_rejected():
    raw = _build_zip([("/etc/passwd", b"x")])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw)
    assert exc_info.value.reason == "traversal"


def test_path_traversal_nested_dotdot_rejected():
    raw = _build_zip([("word/../../etc/evil", b"x")])
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(raw)
    assert exc_info.value.reason == "traversal"


# ---------------------------------------------------------------------------
# Non-zip input
# ---------------------------------------------------------------------------


def test_non_zip_input_rejected():
    """Bytes that don't form a valid zip raise ArchiveBombError, not BadZipFile."""
    with pytest.raises(ArchiveBombError) as exc_info:
        assert_safe_zip(b"not a zip file at all")
    assert exc_info.value.reason == "invalid"


# ---------------------------------------------------------------------------
# Module-level defaults are env-overridable constants
# ---------------------------------------------------------------------------


def test_module_defaults_exposed():
    assert INGEST_MAX_ARCHIVE_MEMBERS == 5000
    assert INGEST_MAX_ARCHIVE_RATIO == 100
    assert INGEST_MAX_ARCHIVE_EXPANDED_BYTES == 524288000  # 500 MB
