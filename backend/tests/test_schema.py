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
    "inventory": {
        "inventory_property_type",
        "inventory_project",
        "inventory_building",
        "inventory_property",
        "inventory_unit",
        "inventory_property_status_history",
        "inventory_property_owner",
        "inventory_listing",
        "inventory_media",
    },
    "crm": {
        "crm_lead_source",
        "crm_campaign",
        "crm_campaign_metric",
        "crm_campaign_step",
        "crm_landing_page",
        "crm_saved_search_alert",
        "crm_lead",
        "crm_lead_location_preference",
        "crm_lead_assignment",
        "crm_lead_status_history",
        "crm_lead_routing_rule",
        "crm_pipeline",
        "crm_pipeline_stage",
        "crm_deal",
        "crm_viewing",
        "crm_agent_field_session",
        "crm_agent_location_point",
        "crm_offer",
        "crm_closing_checklist",
        "crm_closing_checklist_item",
        "crm_transaction",
    },
    "property_ops": {
        "property_ops_lease",
        "property_ops_lease_party",
        "property_ops_rent_schedule",
        "property_ops_deposit",
        "property_ops_inspection",
        "property_ops_application",
        "property_ops_renewal",
        "property_ops_vendor",
        "property_ops_maintenance_request",
        "property_ops_work_order",
    },
    "finance": {
        "finance_account",
        "finance_account_entry",
        "finance_commission_plan",
        "finance_invoice",
        "finance_invoice_line",
        "finance_payment",
        "finance_payment_allocation",
        "finance_cheque",
        "finance_commission",
        "finance_commission_split",
        "finance_installment_plan",
        "finance_installment_milestone",
        "finance_expense",
        "finance_owner_statement",
        "finance_owner_statement_line",
        "finance_reconciliation",
    },
    "collaboration": {
        "collaboration_file",
        "collaboration_document",
        "collaboration_document_link",
        "collaboration_access_grant",
        "collaboration_esign_envelope",
        "collaboration_esign_signer",
        "collaboration_signature_event",
        "collaboration_activity",
        "collaboration_calendar_link",
        "collaboration_template",
        "collaboration_thread",
        "collaboration_message",
        "collaboration_call_log",
        "collaboration_internal_note",
        "collaboration_notification",
        "collaboration_notification_preference",
        "collaboration_notification_dispatch_log",
    },
    "platform": {
        "platform_audit_event",
        "platform_connection",
        "platform_external_mapping",
        "platform_webhook",
        "platform_sync_log",
        "platform_saved_report",
        "platform_report_schedule",
        "platform_dashboard_snapshot",
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
