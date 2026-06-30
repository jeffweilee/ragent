"""Shared lookup over the numbered alembic/sql/<upgrade|downgrade> SQL files.

Used by both scripts/app_doctor.py (runtime head-version check) and
tests/unit/test_migration_inventory.py (static contiguity checks) so the
NNN_*.sql glob/regex pattern lives in exactly one place.
"""

from __future__ import annotations

import re
from pathlib import Path

_NUMBERED_SQL = re.compile(r"^(\d{3})_")


def numbered_versions(dir_path: Path) -> list[int]:
    return sorted(
        int(match.group(1))
        for path in dir_path.glob("[0-9][0-9][0-9]_*.sql")
        if (match := _NUMBERED_SQL.match(path.name))
    )
