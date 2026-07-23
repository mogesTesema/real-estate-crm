"""Enable Row-Level Security on properties tables (plan §2.1)."""
from django.db import migrations

from apps.core.rls import rls_operations


class Migration(migrations.Migration):
    dependencies = [("properties", "0001_initial")]

    operations = rls_operations(
        "properties_property",
        "properties_listing",
        "properties_listingstatushistory",
        "properties_listingmedia",
    )
