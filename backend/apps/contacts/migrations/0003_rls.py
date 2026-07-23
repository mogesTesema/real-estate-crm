"""Enable Row-Level Security on contacts tables (plan §2.1)."""
from django.db import migrations

from apps.core.rls import rls_operations


class Migration(migrations.Migration):
    dependencies = [("contacts", "0002_initial")]

    operations = rls_operations("contacts_contact", "contacts_contactrole")
