"""Enable Row-Level Security on leads tables (plan §2.1)."""
from django.db import migrations

from apps.core.rls import rls_operations


class Migration(migrations.Migration):
    dependencies = [("leads", "0001_initial")]

    operations = rls_operations("leads_lead")
