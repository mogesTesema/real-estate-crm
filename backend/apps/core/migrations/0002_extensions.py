"""Install the Postgres extensions the schema depends on.

Both are "trusted" extensions in PG13+, which means the database owner can install them
without being a superuser — so this works for the deliberately non-superuser `crm_app` role
and on managed hosts like Neon, with no manual dashboard step.

    pg_trgm      fuzzy name/address search (architecture.md §2 "Geospatial": Postgres
                 full-text + pg_trgm, with Elasticsearch intentionally omitted at
                 single-company scale) and contact dedupe similarity (§5)
    btree_gist   required by the lease exclusion constraint (§10), which mixes a scalar
                 key (COALESCE(unit_id, property_id)) with a range in one GiST index

PostGIS is NOT trusted and cannot be installed here. It is provisioned as infrastructure —
deploy/postgres-init.sql for Docker, an explicit psql step in CI, and the Neon dashboard for
production. See DEPLOY.md.
"""
from django.contrib.postgres.operations import BtreeGistExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        TrigramExtension(),
        BtreeGistExtension(),
    ]
