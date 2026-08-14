"""
Seed a realistic demo tenant for client demos.

Populated so the dashboard, pipeline, and property grid look alive on first load:
agents, contacts with roles, properties with photos (Unsplash URLs), listings in varied
statuses, a full pipeline with deals in every stage, and some SLA-breached leads.

    python manage.py seed_demo            # create if absent
    python manage.py seed_demo --reset    # wipe demo tenant and recreate
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.activities.models import Activity
from apps.contacts.models import Contact, ContactRole
from apps.core.models import Branch, Company, Notification, Role, Tenant, User
from apps.core.tenancy import tenant_context
from apps.deals.models import Opportunity, Pipeline, Stage
from apps.deals.services import move_stage
from apps.leads.models import Lead
from apps.leads.services import capture_lead, convert_lead
from apps.properties.models import Listing, ListingStatus, Property
from apps.properties.services import change_listing_status

IMAGES = [
    "https://images.unsplash.com/photo-1560518883-ce09059eeffa",
    "https://images.unsplash.com/photo-1568605114967-8130f3a36994",
    "https://images.unsplash.com/photo-1512917774080-9991f1c4c750",
    "https://images.unsplash.com/photo-1600585154340-be6161a56a0c",
    "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9",
    "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c",
    "https://images.unsplash.com/photo-1580587771525-78b9dba3b914",
    "https://images.unsplash.com/photo-1570129477492-45c003edd2be",
    "https://images.unsplash.com/photo-1605276374104-dee2a0ed3cd6",
    "https://images.unsplash.com/photo-1600566753190-17f0baa2a6c3",
]


def img(i):
    return f"{IMAGES[i % len(IMAGES)]}?auto=format&fit=crop&w=800&q=60"


CITIES = ["Downtown", "Marina", "Uptown", "Riverside", "Old Town", "Business Bay"]
FIRST = ["Sarah", "James", "Aisha", "Omar", "Maria", "David", "Lena", "Noah", "Priya", "Yusuf"]
LAST = ["Khan", "Smith", "Al-Farsi", "Johnson", "Chen", "Okoro", "Rossi", "Haddad"]


class Command(BaseCommand):
    help = "Create a realistic demo tenant."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true")

    def handle(self, *args, **options):
        existing = Tenant.objects.filter(subdomain="demo").first()
        if existing and not options["reset"]:
            self.stdout.write("Demo tenant already exists — use --reset to rebuild.")
            return
        if existing:
            with tenant_context(existing.id):
                for model in (
                    Opportunity,
                    Lead,
                    Listing,
                    Property,
                    ContactRole,
                    Contact,
                    Stage,
                    Pipeline,
                    Activity,
                    Notification,
                ):
                    model.all_objects.all().delete()
            User.objects.filter(tenant=existing).delete()
            with tenant_context(existing.id):
                Branch.all_objects.all().delete()
                Company.all_objects.all().delete()
            existing.delete()
            self.stdout.write("Removed existing demo tenant.")

        # Clear any orphaned demo logins left by interrupted seeds.
        demo_emails = [
            "superadmin@demo.test",
            "admin@demo.test",
            "owner@demo.test",
            "manager@demo.test",
            "pm@demo.test",
            "marketing@demo.test",
            "finance@demo.test",
            "portal@demo.test",
            "tenant@demo.test",
            "landlord@demo.test",
            "buyer@demo.test",
            *[f"agent{i}@demo.test" for i in range(1, 6)],
        ]
        orphans = list(User.objects.filter(email__in=demo_emails))
        if orphans:
            orphan_ids = [u.id for u in orphans]
            # Activities/notifications may still point at these users across tenants.
            Activity.all_objects.filter(actor_id__in=orphan_ids).delete()
            Notification.all_objects.filter(user_id__in=orphan_ids).delete()
            User.objects.filter(id__in=orphan_ids).delete()

        random.seed(42)
        tenant = Tenant.objects.create(name="Demo Realty", subdomain="demo")
        password = "demo12345"

        # System super admin (tenant-bound so API tenant checks pass for demos).
        User.objects.create_user(
            email="superadmin@demo.test",
            password=password,
            full_name="Sam Super",
            tenant=tenant,
            role=Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
            mfa_enabled=False,
        )

        admin = User.objects.create_user(
            email="admin@demo.test",
            password=password,
            full_name="Alex Morgan",
            tenant=tenant,
            role=Role.OWNER,
            is_staff=True,
            mfa_enabled=False,
        )
        # Alias login matching the demo credential list.
        User.objects.create_user(
            email="owner@demo.test",
            password=password,
            full_name="Alex Morgan (Owner)",
            tenant=tenant,
            role=Role.OWNER,
            is_staff=True,
            mfa_enabled=False,
        )

        with tenant_context(tenant.id):
            company = Company.objects.create(tenant=tenant, name="Demo Realty HQ")
            branch = Branch.objects.create(tenant=tenant, company=company, name="Central Branch")

            User.objects.create_user(
                email="manager@demo.test",
                password=password,
                full_name="Morgan Branch",
                tenant=tenant,
                role=Role.MANAGER,
                branch=branch,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="pm@demo.test",
                password=password,
                full_name="Pat Property",
                tenant=tenant,
                role=Role.PROPERTY_MANAGER,
                branch=branch,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="marketing@demo.test",
                password=password,
                full_name="Mia Marketing",
                tenant=tenant,
                role=Role.MARKETING,
                branch=branch,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="finance@demo.test",
                password=password,
                full_name="Finn Finance",
                tenant=tenant,
                role=Role.FINANCE,
                branch=branch,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="portal@demo.test",
                password=password,
                full_name="Perry Portal",
                tenant=tenant,
                role=Role.PORTAL,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="tenant@demo.test",
                password=password,
                full_name="Tessa Tenant",
                tenant=tenant,
                role=Role.PORTAL,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="landlord@demo.test",
                password=password,
                full_name="Larry Landlord",
                tenant=tenant,
                role=Role.PORTAL,
                mfa_enabled=False,
            )
            User.objects.create_user(
                email="buyer@demo.test",
                password=password,
                full_name="Bella Buyer",
                tenant=tenant,
                role=Role.PORTAL,
                mfa_enabled=False,
            )

            agents = [admin]
            for i in range(5):
                agents.append(User.objects.create_user(
                    email=f"agent{i + 1}@demo.test", password=password,
                    full_name=f"{FIRST[i]} {LAST[i % len(LAST)]}",
                    tenant=tenant, role=Role.AGENT, branch=branch,
                    mfa_enabled=False,
                ))

            pipeline = Pipeline.objects.create(
                tenant=tenant, name="Residential Sales", is_default=True,
            )
            stage_defs = [
                ("New", 10, False, False), ("Contacted", 25, False, False),
                ("Viewing", 45, False, False), ("Offer", 65, False, False),
                ("Negotiation", 80, False, False), ("Won", 100, True, False),
                ("Lost", 0, False, True),
            ]
            stages = [
                Stage.objects.create(tenant=tenant, pipeline=pipeline, name=n, order=o,
                                     probability=p, is_won=w, is_lost=lo)
                for o, (n, p, w, lo) in enumerate(stage_defs)
            ]

            # Contacts with roles
            contacts = []
            for i in range(14):
                c = Contact.objects.create(
                    tenant=tenant, full_name=f"{random.choice(FIRST)} {random.choice(LAST)}",
                    email=f"contact{i}@example.com", phone=f"55510{i:02d}",
                    location_preference=random.choice(CITIES),
                    budget_max=random.choice([350000, 500000, 750000, 1200000]),
                    is_vip=i % 6 == 0, source=random.choice(["referral", "website", "portal"]),
                    assigned_agent=random.choice(agents),
                )
                ContactRole.objects.create(
                    tenant=tenant, contact=c,
                    role=random.choice([r for r in ContactRole.RoleType.values]),
                )
                contacts.append(c)

            # Properties + listings
            statuses = [ListingStatus.ACTIVE, ListingStatus.ACTIVE, ListingStatus.UNDER_OFFER,
                        ListingStatus.RESERVED, ListingStatus.SOLD, ListingStatus.ACTIVE]
            for i in range(10):
                beds = random.randint(1, 5)
                ptype = random.choice(
                    ['Apartment', 'Villa', 'Townhouse'],
                )
                city = random.choice(CITIES)
                title = f"{beds}BR {ptype}, {city}"
                prop = Property.objects.create(
                    tenant=tenant,
                    category=Property.Category.RESIDENTIAL,
                    title=title,
                    city=random.choice(CITIES),
                    bedrooms=beds,
                    bathrooms=random.randint(1, beds),
                    area_built=random.choice(
                        [850, 1100, 1600, 2200, 3000],
                    ),
                    owner=random.choice(contacts),
                    custom_fields={"image_url": img(i)},
                )
                listing = Listing.objects.create(
                    tenant=tenant, property=prop, listing_type=Listing.ListingType.SALE,
                    price=random.choice([320000, 480000, 640000, 890000, 1350000]),
                    listing_agent=random.choice(agents),
                )
                target = statuses[i % len(statuses)]
                # walk the state machine to the target status
                path = {
                    ListingStatus.ACTIVE: [
                        ListingStatus.ACTIVE,
                    ],
                    ListingStatus.UNDER_OFFER: [
                        ListingStatus.ACTIVE,
                        ListingStatus.UNDER_OFFER,
                    ],
                    ListingStatus.RESERVED: [
                        ListingStatus.ACTIVE,
                        ListingStatus.UNDER_OFFER,
                        ListingStatus.RESERVED,
                    ],
                    ListingStatus.SOLD: [
                        ListingStatus.ACTIVE,
                        ListingStatus.UNDER_OFFER,
                        ListingStatus.SOLD,
                    ],
                }[target]
                for s in path:
                    change_listing_status(listing=listing, to_status=s, user=admin, reason="seed")

            # Leads (some breached, some converted)
            for i in range(12):
                lead = capture_lead(tenant_id=tenant.id, data={
                    "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
                    "email": f"lead{i}@example.com", "phone": f"55520{i:02d}",
                    "lead_type": random.choice(["buy", "rent_in", "investment"]),
                    "source": random.choice(["referral", "website", "portal", "facebook"]),
                    "budget_max": random.choice([300000, 550000, 800000]),
                    "preferred_location": random.choice(CITIES),
                    "timeline": random.choice(["immediate", "1_month", "3_months"]),
                }, actor=admin)
                if i % 4 == 0:  # breach a quarter of them
                    Lead.objects.filter(id=lead.id).update(
                        sla_due_at=timezone.now() - timedelta(hours=2), sla_breached=True)

            # Opportunities spread across stages
            for i in range(9):
                opp = convert_lead(
                    lead=Lead.objects.filter(converted_opportunity__isnull=True).first()
                    or capture_lead(tenant_id=tenant.id, data={
                        "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
                        "email": f"opp{i}@example.com", "lead_type": "buy",
                        "source": "referral", "budget_max": 600000,
                    }, actor=admin),
                    pipeline=pipeline, stage=stages[0], actor=admin,
                )
                # move some deals forward
                steps = random.randint(0, 5)
                for s in range(1, steps + 1):
                    move_stage(opportunity=opp, to_stage=stages[s], user=admin,
                               reason="Progressing deal", next_action="Follow up")

        self.stdout.write(self.style.SUCCESS(
            "Seeded 'Demo Realty' — role logins (password demo12345):\n"
            "  superadmin@demo.test  owner@demo.test / admin@demo.test\n"
            "  manager@demo.test     agent1@demo.test\n"
            "  pm@demo.test          marketing@demo.test\n"
            "  finance@demo.test     portal@demo.test (combo)\n"
            "  tenant@demo.test      landlord@demo.test      buyer@demo.test"
        ))
