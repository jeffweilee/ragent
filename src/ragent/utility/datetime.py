"""UTC datetime helpers (spec §DateTime Handling)."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_db(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes returned by the MariaDB driver."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
