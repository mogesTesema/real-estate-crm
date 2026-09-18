"""Idempotent seed for the live e2e walkthrough: a company, a branch, a team and one
super-admin login. Safe to run repeatedly; prints the credentials it guarantees.

    python manage.py bootstrap_e2e
"""
from django.core.management.base import BaseCommand

EMAIL = "e2e-admin@walkthrough.test"
PASSWORD = "e2e-walkthrough-pass-1"


class Command(BaseCommand):
    help = "Ensure the e2e walkthrough's company/branch/team/admin exist."

    def handle(self, *args, **options):
        from apps.identity.models import Branch, Company, Role, Team, User, UserRole

        company, _ = Company.objects.get_or_create(
            name="Walkthrough Realty",
            defaults={"legal_name": "Walkthrough Realty LLC", "default_currency": "AED"},
        )
        branch, _ = Branch.objects.get_or_create(
            company=company, code="E2E", defaults={"name": "E2E Branch"}
        )
        team, _ = Team.objects.get_or_create(
            branch=branch, code="E2E", defaults={"name": "E2E Team"}
        )
        user = User.objects.filter(email=EMAIL).first()
        if user is None:
            user = User.objects.create_user(
                email=EMAIL, password=PASSWORD, first_name="E2E", last_name="Admin",
                branch=branch, team=team, is_staff=True, is_superuser=True,
            )
        else:
            user.set_password(PASSWORD)
            user.is_active = True
            user.must_change_password = False
            user.save()
        from apps.inventory.models import PropertyType

        PropertyType.objects.get_or_create(
            code="APARTMENT",
            defaults={"name": "Apartment", "category": "RESIDENTIAL"},
        )
        role = Role.objects.filter(code="super_admin").first()
        if role:
            UserRole.objects.get_or_create(user=user, role=role)
        self.stdout.write(self.style.SUCCESS(f"{EMAIL} / {PASSWORD}"))
