"""T0.5 — utcnow() tz-aware UTC; to_iso ends in Z; from_db attaches UTC."""

from datetime import UTC, datetime


def test_utcnow_is_timezone_aware() -> None:
    from ragent.utility.datetime import utcnow

    now = utcnow()
    assert now.tzinfo is not None
    assert now.tzinfo == UTC


def test_to_iso_ends_with_z() -> None:
    from ragent.utility.datetime import to_iso, utcnow

    assert to_iso(utcnow()).endswith("Z")


def test_to_iso_format_is_valid_iso8601() -> None:
    from ragent.utility.datetime import to_iso, utcnow

    s = to_iso(utcnow())
    parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert parsed.tzinfo == UTC


def test_from_db_attaches_utc_to_naive() -> None:
    from ragent.utility.datetime import from_db

    naive = datetime(2024, 1, 15, 10, 30, 0)
    result = from_db(naive)
    assert result.tzinfo == UTC
    assert result.year == 2024 and result.month == 1 and result.day == 15


def test_from_db_preserves_aware_datetime() -> None:
    from ragent.utility.datetime import from_db

    aware = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert from_db(aware).tzinfo == UTC


def test_utcnow_is_recent() -> None:
    from ragent.utility.datetime import utcnow

    now = utcnow()
    delta = (datetime.now(UTC) - now).total_seconds()
    assert abs(delta) < 1


def test_from_iso_handles_z_suffix() -> None:
    from ragent.utility.datetime import from_iso, to_iso, utcnow

    s = to_iso(utcnow())  # ends in "Z"
    parsed = from_iso(s)
    assert parsed.tzinfo == UTC


def test_from_iso_handles_offset_suffix() -> None:
    from ragent.utility.datetime import from_iso

    parsed = from_iso("2026-05-15T12:34:56.789+00:00")
    assert parsed.tzinfo == UTC


def test_from_iso_attaches_utc_to_naive() -> None:
    from ragent.utility.datetime import from_iso

    parsed = from_iso("2026-05-15T12:34:56.789")
    assert parsed.tzinfo == UTC
