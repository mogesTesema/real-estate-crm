"""Schema-shape guards.

The rebuild is schema-first, so these are the tests that make a phase's migration work
verifiable before any service or endpoint exists.
"""
from io import StringIO

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command

LOCAL_APP_LABELS = [
    "core",
    "identity",
    "contacts",
    "inventory",
    "crm",
    "property_ops",
    "finance",
    "collaboration",
    "platform",
]

# architecture.md §1.1: "Logical table names use the owning app prefix". Tables landed so far;
# extend this per roadmap phase. The list is explicit rather than derived so that a model
# silently moving between apps fails here.
EXPECTED_TABLES = {
    "core": {"core_custom_field", "core_sequence"},
    "identity": {
        "identity_company",
        "identity_branch",
        "identity_team",
        "identity_user",
        "identity_role",
        "identity_permission",
        "identity_user_role",
        "identity_role_permission",
        "identity_portal_profile",
        "identity_record_share",
        "identity_field_permission",
    },
    "contacts": {
        "contacts_contact",
        "contacts_contact_role",
        "contacts_contact_relationship",
        "contacts_consent",
    },
}


def test_no_missing_migrations(db):
    """Models and migrations agree — catches a field edited without makemigrations.

    Needs `db` because makemigrations loads the migration graph through a connection.
    """
    out = StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", verbosity=1, stdout=out)
    except SystemExit:  # raised with status 1 when changes are unwritten
        pytest.fail(f"Model changes have no migration. Run makemigrations.\n{out.getvalue()}")


@pytest.mark.parametrize("label", LOCAL_APP_LABELS)
def test_every_table_uses_its_owning_app_prefix(label):
    """architecture.md §1.1 — `crm_deal`, `finance_invoice`, `identity_user`, and so on.

    Django's default table name is `<app_label>_<modelname>`, which already satisfies this,
    but only by coincidence for single-word models; the spec's names are normative, so every
    model sets db_table explicitly. This catches a missed Meta.
    """
    offenders = [
        model._meta.db_table
        for model in django_apps.get_app_config(label).get_models()
        if not model._meta.db_table.startswith(f"{label}_")
    ]
    assert not offenders, f"{label}: tables missing the owning app prefix: {offenders}"


@pytest.mark.parametrize("label", sorted(EXPECTED_TABLES))
def test_expected_tables_are_built(label):
    built = {model._meta.db_table for model in django_apps.get_app_config(label).get_models()}
    assert built == EXPECTED_TABLES[label]
