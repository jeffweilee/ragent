"""Tests for error codes (codes.py)."""

from ragent.errors import codes


def test_attachment_error_codes_exist():
    """T-CAT.1: Attachment error codes must be defined."""
    assert hasattr(codes.HttpErrorCode, "ATTACHMENT_MIME_UNSUPPORTED")
    assert hasattr(codes.HttpErrorCode, "ATTACHMENT_TOO_LARGE")
    assert hasattr(codes.HttpErrorCode, "ATTACHMENT_PARSE_FAILED")


def test_attachment_error_codes_values():
    """Verify error codes have expected values."""
    assert codes.HttpErrorCode.ATTACHMENT_MIME_UNSUPPORTED == "ATTACHMENT_MIME_UNSUPPORTED"
    assert codes.HttpErrorCode.ATTACHMENT_TOO_LARGE == "ATTACHMENT_TOO_LARGE"
    assert codes.HttpErrorCode.ATTACHMENT_PARSE_FAILED == "ATTACHMENT_PARSE_FAILED"
