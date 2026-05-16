"""UTC datetime helpers (spec §DateTime Handling)."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_iso(s: str) -> datetime:
    """Parse an ISO-8601 string back to a tz-aware UTC datetime.

    Normalises the trailing ``Z`` to ``+00:00`` so the parse works on the
    project's full supported Python range (3.11+ already handles ``Z``, but
    going through the helper keeps call sites consistent with `to_iso`).
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def from_db(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes returned by the MariaDB driver."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
