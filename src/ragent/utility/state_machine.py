"""Document status state machine (spec S10)."""

_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("UPLOADED", "PENDING"),
        ("PENDING", "READY"),
        ("PENDING", "FAILED"),
        ("PENDING", "DELETING"),
        ("READY", "DELETING"),
        ("FAILED", "DELETING"),
    }
)


class IllegalStateTransition(Exception):
    pass


def assert_transition(from_status: str, to_status: str) -> None:
    """Raise IllegalStateTransition if the transition is not allowed."""
    if (from_status, to_status) not in _ALLOWED:
        raise IllegalStateTransition(f"{from_status} → {to_status} is not a valid transition")
