"""Does the built schema actually cover architecture.md?

The rest of the suite checks that individual constraints behave. This file checks the
*whole spec* is present: every table it names exists, and every foreign key it declares was
actually built. It parses architecture.md directly, so the spec stays the authority rather
than a copy of it drifting inside the tests.

This is what makes the deferred-FK build rule safe. Fields pointing at a not-yet-built app
are declared in the phase that introduces the target, which means a forgotten one leaves no
trace in the owning app's models — nothing fails, the column is simply absent. These tests
are what notice.
"""
import re
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.db import connection

APPS = "core|identity|contacts|inventory|crm|property_ops|finance|collaboration|platform"


def _find_spec() -> Path:
    """architecture.md lives at the repo root, which is reachable differently depending on
    where the suite runs: the whole repo is checked out in CI and for a local run, while
    docker-compose mounts only backend/ plus the spec itself (see docker-compose.yml)."""
    for candidate in (
        *(parent / "architecture.md" for parent in Path(__file__).resolve().parents),
        Path("/architecture.md"),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "architecture.md not found. It is the authority these tests check against; "
        "mount or check out the repo root."
    )


SPEC_PATH = _find_spec()

# A bare `app_table` line opens a table block; field lines follow until the next such line.
TABLE_RE = re.compile(rf"^({APPS})_[a-z_]+$")
FK_RE = re.compile(rf"^([a-z_]+)\s+UUID\s+FK\s+(({APPS})_[a-z_]+)", re.I)

# Tables architecture.md defines with no foreign keys at all. Listed explicitly so that a
# table losing its FKs by accident is not mistaken for one that never had any.
STANDALONE_TABLES = {
    "core_custom_field",
    "core_sequence",
    "platform_dashboard_snapshot",
}


def parse_spec():
    """-> (tables, fks) where fks is {(table, column, target_table)}."""
    tables, fks, current = set(), set(), None
    for raw in SPEC_PATH.read_text().splitlines():
        line = raw.strip()
        if TABLE_RE.match(line):
            current = line
            tables.add(current)
            continue
        if current:
            m = FK_RE.match(line)
            if m:
                column = m.group(1)
                # The spec is inconsistent about the _id suffix — `contact_id UUID FK ...`
                # but `created_by UUID FK ...`. Postgres always has the suffix.
                if not column.endswith("_id"):
                    column = f"{column}_id"
                fks.add((current, column, m.group(2)))
    return tables, fks


@pytest.fixture(scope="module")
def spec():
    tables, fks = parse_spec()
    # Guard the parser itself: if a spec reformat breaks the regexes, these tests would
    # silently pass by checking nothing.
    assert len(tables) >= 95, f"only parsed {len(tables)} tables — parser likely broken"
    assert len(fks) >= 250, f"only parsed {len(fks)} FKs — parser likely broken"
    return tables, fks


@pytest.fixture(scope="module")
def built_fks(django_db_setup, django_db_blocker):
    """Every foreign key in the live schema, as {(table, column, target_table)}."""
    with django_db_blocker.unblock(), connection.cursor() as cur:
        cur.execute(
            """
            SELECT c.conrelid::regclass::text,
                   a.attname,
                   c.confrelid::regclass::text
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY k(attnum, ord) ON TRUE
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
            """
        )
        return {tuple(row) for row in cur.fetchall()}


def test_every_table_in_the_spec_exists(spec):
    specified, _ = spec
    built = {m._meta.db_table for m in django_apps.get_models()}
    missing = sorted(specified - built)
    assert not missing, f"architecture.md defines tables that were never built: {missing}"


def test_every_foreign_key_in_the_spec_exists(built_fks, spec):
    _, specified = spec
    missing = sorted(specified - built_fks)
    assert not missing, (
        "architecture.md declares foreign keys that are absent from the built schema. "
        "A deferred FK was probably never wired up in the phase that introduced its "
        "target:\n" + "\n".join(f"  {t}.{c} -> {g}" for t, c, g in missing)
    )


def test_tables_expected_to_have_no_foreign_keys_still_have_none(built_fks, spec):
    """The inverse guard: a table in STANDALONE_TABLES that grows an FK, or a table that
    should have FKs and lost them, both show up here."""
    specified_tables, _ = spec
    with_fks = {t for t, _, _ in built_fks} | {g for _, _, g in built_fks}
    actually_standalone = {t for t in specified_tables if t not in with_fks}
    assert actually_standalone == STANDALONE_TABLES


def test_no_spec_foreign_key_was_left_as_a_bare_uuid_column(spec):
    """A forward reference stubbed as `models.UUIDField()` and never promoted would satisfy
    nothing above — the column exists but carries no constraint. Catch it by name."""
    _, specified = spec
    by_table = {}
    for table, column, _ in specified:
        by_table.setdefault(table, set()).add(column)

    offenders = []
    for model in django_apps.get_models():
        expected = by_table.get(model._meta.db_table, set())
        for field in model._meta.get_fields():
            column = getattr(field, "column", None)
            if column in expected and not field.is_relation:
                offenders.append(f"{model._meta.db_table}.{column}")

    assert not offenders, (
        "columns the spec declares as foreign keys are plain (non-relational) fields: "
        f"{sorted(offenders)}"
    )
