"""UTC datetime helpers (spec §DateTime Handling)."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a tz-aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Serialize a datetime to ISO 8601 with a Z suffix."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_db(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime read from the database."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
