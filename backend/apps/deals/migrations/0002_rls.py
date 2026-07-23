"""Enable Row-Level Security on deals tables (plan §2.1)."""
from django.db import migrations

from apps.core.rls import rls_operations


class Migration(migrations.Migration):
    dependencies = [("deals", "0001_initial")]

    operations = rls_operations(
        "deals_pipeline",
        "deals_stage",
        "deals_opportunity",
        "deals_opportunitystagehistory",
    )
