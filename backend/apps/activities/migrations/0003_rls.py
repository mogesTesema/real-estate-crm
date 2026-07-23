"""Enable Row-Level Security on activities table (plan §2.1)."""
from django.db import migrations

from apps.core.rls import rls_operations


class Migration(migrations.Migration):
    dependencies = [("activities", "0002_initial")]

    operations = rls_operations("activities_activity")
