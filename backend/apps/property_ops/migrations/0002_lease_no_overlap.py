"""Make double-letting physically impossible (architecture.md §10).

    "Exclusion constraint preventing overlapping active lease periods on the same rentable
     target — keyed on COALESCE(unit_id, property_id) so both unit-level and whole-property
     leases are protected (GiST, tstzrange over start/end, WHERE status IN ACTIVE-like states)."

Raw SQL rather than Meta.constraints. Django's ExclusionConstraint cannot express this one:
the key is a COALESCE expression over two columns rather than a plain field, and mixing a
scalar equality key with a range overlap key in one GiST index needs the btree_gist extension
(installed by core/0002).

Semantics, and why each piece is there:

* `daterange(start_date, end_date, '[]')` — inclusive on both ends, because a lease ending on
  the 31st and another starting on the 31st genuinely collide for that day. The spec says
  tstzrange; these are DATE columns, so daterange is the faithful equivalent and avoids an
  implicit timezone-dependent cast.
* `COALESCE(unit_id, property_id)` — a unit-level lease and a whole-property lease on the same
  property must contend for the same key. Without the COALESCE, letting a building wholesale
  while its units are individually let would pass unnoticed.
* `WHERE status IN (...)` — only leases that actually occupy the target participate. DRAFT,
  TERMINATED, and EXPIRED rows must be free to overlap, or you could never draft a successor
  lease while the current one runs. The list mirrors Lease.OCCUPYING_STATUSES.
"""
from django.db import migrations

OCCUPYING = "'PENDING_SIGNATURE', 'ACTIVE', 'EXPIRING', 'RENEWED'"

CREATE = f"""
ALTER TABLE property_ops_lease
ADD CONSTRAINT property_ops_lease_no_overlap
EXCLUDE USING gist (
    (COALESCE(unit_id, property_id)) WITH =,
    daterange(start_date, end_date, '[]') WITH &&
)
WHERE (status IN ({OCCUPYING}));
"""

DROP = """
ALTER TABLE property_ops_lease
DROP CONSTRAINT IF EXISTS property_ops_lease_no_overlap;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("property_ops", "0001_initial"),
        # btree_gist, needed to combine the `=` and `&&` operators in one GiST index.
        ("core", "0002_extensions"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE, reverse_sql=DROP),
    ]
