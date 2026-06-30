"""Static guards for the migration inventory.

The Docker-backed schema drift test proves the resulting DB shape. These tests
catch cheaper bookkeeping mistakes before a container is needed: missing raw
SQL revisions, broken numbering, and upgrade/downgrade pairs falling out of
sync.
"""

from __future__ import annotations

from pathlib import Path

from ragent.utility.migration_inventory import numbered_versions

ROOT = Path(__file__).parents[2]
UPGRADE_DIR = ROOT / "alembic" / "sql" / "upgrade"
DOWNGRADE_DIR = ROOT / "alembic" / "sql" / "downgrade"
SCHEMA_SQL = ROOT / "migrations" / "schema.sql"


def test_numbered_sql_migrations_are_contiguous() -> None:
    migration_numbers = numbered_versions(UPGRADE_DIR)

    assert migration_numbers, "expected at least one numbered SQL migration"
    assert migration_numbers == list(range(1, migration_numbers[-1] + 1)), (
        "numbered upgrade SQL migrations must be contiguous; missing revision(s) "
        "would make deployment history ambiguous"
    )


def test_downgrade_sql_migrations_are_contiguous() -> None:
    downgrade_numbers = numbered_versions(DOWNGRADE_DIR)

    assert downgrade_numbers, "expected at least one numbered downgrade SQL migration"
    assert downgrade_numbers == list(range(1, downgrade_numbers[-1] + 1)), (
        "numbered downgrade SQL migrations must be contiguous; missing revision(s) "
        "would make rollback history ambiguous"
    )


def test_every_upgrade_has_a_matching_downgrade() -> None:
    upgrade_names = {p.name for p in UPGRADE_DIR.glob("[0-9][0-9][0-9]_*.sql")}
    downgrade_names = {p.name for p in DOWNGRADE_DIR.glob("[0-9][0-9][0-9]_*.sql")}

    assert upgrade_names == downgrade_names, (
        "every alembic/sql/upgrade/<NNN>_*.sql file must have a same-named "
        "alembic/sql/downgrade/<NNN>_*.sql counterpart, and vice versa — "
        f"upgrade-only: {upgrade_names - downgrade_names or None}, "
        f"downgrade-only: {downgrade_names - upgrade_names or None}"
    )


def test_schema_snapshot_mentions_latest_numbered_migration() -> None:
    latest = max(UPGRADE_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    schema_sql = SCHEMA_SQL.read_text(encoding="utf-8")

    assert latest.name in schema_sql, (
        "migrations/schema.sql should document the latest raw SQL migration it "
        f"folds in; missing {latest.name} suggests the snapshot comment is stale"
    )
