"""Leasing (SRS §3.5) — state machine, overlap guard, schedules, deposits, activation.

The design decision under test throughout: property_ops has no LeaseStatusHistory table —
the append-only audit trail via `lease_status_changed` IS the history — and every save that
enters the occupying set goes through one choke point that translates the GiST exclusion
into a field-keyed 400.
"""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.property_ops import services
from apps.property_ops.models import Deposit, Lease, RentSchedule


@pytest.fixture
def pm(make_user):
    return make_user("property_manager")


@pytest.fixture
def new_lease(pm, make_property, make_contact):
    def _make(actor=None, **kwargs):
        kwargs.setdefault("property", make_property(managed_by=pm))
        kwargs.setdefault("tenant", make_contact())
        kwargs.setdefault("landlord", make_contact())
        kwargs.setdefault("property_manager", pm)
        kwargs.setdefault("lease_type", Lease.LeaseType.RESIDENTIAL)
        kwargs.setdefault("start_date", "2026-01-01")
        kwargs.setdefault("end_date", "2026-12-31")
        kwargs.setdefault("rent_amount", Decimal("10000"))
        kwargs.setdefault("billing_frequency", Lease.BillingFrequency.MONTHLY)
        kwargs.setdefault("security_deposit", Decimal("10000"))
        return services.create_lease(actor=actor or pm, **kwargs)

    return _make


class TestLeaseLifecycle:
    def test_a_lease_is_born_draft_with_a_generated_reference(self, db, new_lease):
        lease = new_lease()
        assert lease.status == Lease.Status.DRAFT
        assert lease.reference_code.startswith("LSE-")

    def test_the_status_history_is_the_audit_trail(self, db, new_lease, pm):
        """No history table, on purpose — the append-only audit row is the history."""
        from apps.platform.models import AuditEvent

        lease = new_lease()
        services.change_lease_status(
            lease, Lease.Status.PENDING_SIGNATURE, actor=pm, reason="Sent for signing."
        )
        event = AuditEvent.objects.filter(
            entity_type="LEASE", entity_id=lease.pk, action=AuditEvent.Action.UPDATE
        ).latest("created_at")
        assert event.old_values["status"] == "DRAFT"
        assert event.new_values["status"] == "PENDING_SIGNATURE"
        assert event.new_values["reason"] == "Sent for signing."

    def test_terminal_states_are_terminal(self, db, new_lease, pm):
        lease = new_lease()
        services.change_lease_status(lease, Lease.Status.TERMINATED, actor=pm)
        with pytest.raises(ValidationError, match="terminal"):
            services.change_lease_status(lease, Lease.Status.ACTIVE, actor=pm)

    def test_tenant_and_landlord_must_differ(self, db, new_lease, make_contact):
        person = make_contact()
        with pytest.raises(ValidationError, match="same contact"):
            new_lease(tenant=person, landlord=person)

    def test_core_terms_freeze_once_live(self, db, new_lease, pm):
        """A live contract's numbers are not editable — renew or terminate."""
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        with pytest.raises(ValidationError, match="frozen"):
            services.update_lease(lease, actor=pm, rent_amount=Decimal("12000"))
        # Non-core administrivia stays editable.
        services.update_lease(lease, actor=pm, notice_period_days=60)


class TestOverlapGuard:
    def test_double_letting_is_a_400_not_a_500(self, db, new_lease, pm, make_property):
        """The GiST exclusion fires at statement time; without the choke point it surfaces
        as an IntegrityError 500 on a perfectly ordinary double-booking attempt."""
        prop = make_property(managed_by=pm)
        first = new_lease(property=prop)
        services.activate_lease(first, actor=pm)

        second = new_lease(property=prop, start_date="2026-06-01", end_date="2027-05-31")
        with pytest.raises(ValidationError) as caught:
            services.activate_lease(second, actor=pm)
        assert "start_date" in caught.value.message_dict
        second.refresh_from_db()
        assert second.status == Lease.Status.DRAFT  # the whole activation rolled back

    def test_the_guard_explains_inclusive_end_dates(self, db, new_lease, pm, make_property):
        """`daterange(start, end, '[]')` — back-to-back leases must not share a day, and the
        error message says so because nobody guesses that from a constraint name."""
        prop = make_property(managed_by=pm)
        services.activate_lease(new_lease(property=prop), actor=pm)
        touching = new_lease(property=prop, start_date="2026-12-31", end_date="2027-12-30")
        with pytest.raises(ValidationError, match="inclusive"):
            services.activate_lease(touching, actor=pm)

    def test_consecutive_leases_are_fine(self, db, new_lease, pm, make_property):
        prop = make_property(managed_by=pm)
        services.activate_lease(new_lease(property=prop), actor=pm)
        successor = new_lease(property=prop, start_date="2027-01-01", end_date="2027-12-31")
        services.activate_lease(successor, actor=pm)
        assert successor.status == Lease.Status.ACTIVE


class TestRentSchedule:
    def test_monthly_periods_tile_the_term(self, db, new_lease, pm):
        lease = new_lease()
        rows = services.generate_rent_schedule(lease, actor=pm)
        assert len(rows) == 12
        assert str(rows[0].period_start) == "2026-01-01"
        assert str(rows[0].period_end) == "2026-01-31"
        assert str(rows[-1].period_end) == "2026-12-31"
        assert all(row.amount == lease.rent_amount for row in rows)

    def test_the_last_period_is_clipped(self, db, new_lease, pm):
        lease = new_lease(end_date="2026-02-14")
        rows = services.generate_rent_schedule(lease, actor=pm)
        assert len(rows) == 2
        assert str(rows[-1].period_start) == "2026-02-01"
        assert str(rows[-1].period_end) == "2026-02-14"

    def test_quarterly(self, db, new_lease, pm):
        lease = new_lease(billing_frequency=Lease.BillingFrequency.QUARTERLY)
        assert len(services.generate_rent_schedule(lease, actor=pm)) == 4

    def test_generation_is_idempotent(self, db, new_lease, pm):
        lease = new_lease()
        services.generate_rent_schedule(lease, actor=pm)
        services.generate_rent_schedule(lease, actor=pm)
        assert lease.rent_schedules.count() == 12

    def test_custom_periods_must_tile(self, db, new_lease, pm):
        import datetime

        lease = new_lease(
            billing_frequency=Lease.BillingFrequency.CUSTOM,
            start_date="2026-01-01", end_date="2026-03-31",
        )
        with pytest.raises(ValidationError, match="tile"):
            services.generate_rent_schedule(
                lease, actor=pm,
                periods=[
                    {
                        "period_start": datetime.date(2026, 1, 1),
                        "period_end": datetime.date(2026, 1, 31),
                        "amount": Decimal("10000"),
                    },
                    # gap: February missing
                    {
                        "period_start": datetime.date(2026, 3, 1),
                        "period_end": datetime.date(2026, 3, 31),
                        "amount": Decimal("10000"),
                    },
                ],
            )

    def test_waiving_an_invoiced_period_is_refused(self, db, new_lease, pm):
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        invoiced = lease.rent_schedules.filter(
            status=RentSchedule.Status.INVOICED
        ).first()
        assert invoiced is not None
        with pytest.raises(ValidationError, match="void the invoice|void its invoice"):
            services.waive_rent_period(invoiced, actor=pm, reason="Goodwill.")


class TestActivation:
    """`activate_lease` is §1.2's orchestration: occupancy + schedule + expected deposit +
    due invoices, one transaction."""

    def test_activation_produces_the_whole_package(self, db, new_lease, pm):
        from apps.finance.models import Invoice

        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        lease.refresh_from_db()
        assert lease.status == Lease.Status.ACTIVE
        assert lease.rent_schedules.count() == 12
        deposit = lease.deposits.get()
        assert deposit.status == Deposit.Status.HELD
        assert deposit.amount == lease.security_deposit
        # Periods already due (the whole term is in the past relative to 2026-09) are
        # invoiced; future ones wait for the recurring command.
        invoiced = lease.rent_schedules.filter(status=RentSchedule.Status.INVOICED)
        assert invoiced.exists()
        assert Invoice.objects.filter(lease=lease).count() == invoiced.count()

    def test_no_deposit_row_when_no_deposit(self, db, new_lease, pm):
        lease = new_lease(security_deposit=Decimal("0"))
        services.activate_lease(lease, actor=pm)
        assert not lease.deposits.exists()

    def test_the_pm_is_notified(self, db, new_lease, pm):
        from apps.collaboration.models import Notification

        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        assert Notification.objects.filter(
            recipient=pm, title__contains=lease.reference_code
        ).exists()


class TestDeposits:
    def test_receipt_is_a_separate_fact_from_expectation(self, db, new_lease, pm):
        """SRS 3.5.4 — "the contract says we should hold it" and "we hold it" differ."""
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        deposit = lease.deposits.get()
        assert deposit.received_date is None
        assert deposit.held_amount == 0
        services.record_deposit_received(lease, actor=pm)
        deposit.refresh_from_db()
        assert deposit.held_amount == lease.security_deposit
        assert deposit.received_date is not None

    def test_refund_walks_to_fully_refunded(self, db, new_lease, pm):
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        services.record_deposit_received(lease, actor=pm)
        deposit = lease.deposits.get()
        services.refund_deposit(deposit, actor=pm, amount=Decimal("4000"))
        assert deposit.status == Deposit.Status.PARTIALLY_REFUNDED
        services.refund_deposit(deposit, actor=pm, amount=Decimal("6000"))
        assert deposit.status == Deposit.Status.FULLY_REFUNDED

    def test_over_refund_is_a_friendly_400(self, db, new_lease, pm):
        """Pre-validated so the DB CHECK never fires at a user."""
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        services.record_deposit_received(lease, actor=pm)
        deposit = lease.deposits.get()
        with pytest.raises(ValidationError, match="exceed"):
            services.refund_deposit(deposit, actor=pm, amount=Decimal("10001"))

    def test_deduction_needs_a_reason(self, db, new_lease, pm):
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        services.record_deposit_received(lease, actor=pm)
        with pytest.raises(ValidationError, match="reason"):
            services.deduct_from_deposit(
                lease.deposits.get(), actor=pm, amount=Decimal("500"), reason=" "
            )

    def test_every_movement_is_audited(self, db, new_lease, pm):
        from apps.platform.models import AuditEvent

        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        services.record_deposit_received(lease, actor=pm)
        deposit = lease.deposits.get()
        services.deduct_from_deposit(
            deposit, actor=pm, amount=Decimal("1000"), reason="Broken door."
        )
        movements = AuditEvent.objects.filter(entity_type="DEPOSIT", entity_id=deposit.pk)
        assert movements.count() == 2  # RECEIVED + DEDUCTION


class TestTermination:
    def test_uninvoiced_periods_cancel_invoiced_ones_survive(self, db, new_lease, pm):
        """Money artefacts are never silently destroyed — finance voids them explicitly."""
        lease = new_lease()
        services.activate_lease(lease, actor=pm)
        invoiced_before = lease.rent_schedules.filter(
            status=RentSchedule.Status.INVOICED
        ).count()
        scheduled_before = lease.rent_schedules.filter(
            status=RentSchedule.Status.SCHEDULED
        ).count()
        assert scheduled_before > 0

        services.terminate_lease(lease, actor=pm, reason="Tenant relocated.")
        lease.refresh_from_db()
        assert lease.status == Lease.Status.TERMINATED
        assert lease.rent_schedules.filter(
            status=RentSchedule.Status.CANCELLED
        ).count() == scheduled_before
        assert lease.rent_schedules.filter(
            status=RentSchedule.Status.INVOICED
        ).count() == invoiced_before


class TestMoneyScoping:
    """The portal halves of §2 for the money spine — the rows a client may and may not see.
    These are API-level because the review pass proved endpoint wiring is where scoping
    bugs actually live."""

    @pytest.fixture
    def tenant_portal(self, pm, make_property, make_contact, make_user, auth_client):
        """An ACTIVE lease whose tenant holds a working portal login."""
        from apps.identity.models import PortalProfile, Role, User, UserRole

        tenant = make_contact()
        lease = services.create_lease(
            actor=pm, property=make_property(managed_by=pm), tenant=tenant,
            landlord=make_contact(), property_manager=pm,
            lease_type=Lease.LeaseType.RESIDENTIAL,
            start_date="2026-01-01", end_date="2026-12-31",
            rent_amount=Decimal("1000"),
            billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit=Decimal("5000"),
        )
        services.activate_lease(lease, actor=pm)
        user = User.objects.create_user(
            email=tenant.email, password="portal-pass-8811", first_name="T", last_name="P",
        )
        UserRole.objects.create(user=user, role=Role.objects.get(code="portal"))
        PortalProfile.objects.create(
            user=user, contact=tenant, portal_type=PortalProfile.PortalType.TENANT,
            eligibility_status=PortalProfile.EligibilityStatus.ACTIVE,
        )
        return {"lease": lease, "tenant": tenant, "user": user, "client": auth_client(user)}

    def test_a_tenant_reads_their_own_lease_and_rent_history(self, db, tenant_portal):
        client = tenant_portal["client"]
        leases = client.get("/api/v1/leases/")
        assert leases.status_code == 200
        assert leases.data["count"] == 1

        ledger = client.get(
            f"/api/v1/leases/{tenant_portal['lease'].pk}/rent-schedule/"
        )
        assert ledger.status_code == 200
        assert len(ledger.data) == 12

    def test_a_tenant_sees_their_rent_invoices_but_not_a_strangers(
        self, db, tenant_portal, pm, make_property, make_contact
    ):
        other = services.create_lease(
            actor=pm, property=make_property(managed_by=pm), tenant=make_contact(),
            landlord=make_contact(), property_manager=pm,
            lease_type=Lease.LeaseType.RESIDENTIAL,
            start_date="2026-01-01", end_date="2026-12-31",
            rent_amount=Decimal("2000"),
            billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit=Decimal("0"),
        )
        services.activate_lease(other, actor=pm)

        response = tenant_portal["client"].get("/api/v1/invoices/")
        assert response.status_code == 200
        assert response.data["count"] > 0
        leases_billed = {row["lease"] for row in response.data["results"]}
        assert leases_billed == {tenant_portal["lease"].pk}

    def test_a_tenant_cannot_activate_or_terminate_anything(self, db, tenant_portal):
        """StaffWrite: portal clients are read-only on the money spine."""
        lease_id = tenant_portal["lease"].pk
        assert tenant_portal["client"].post(
            f"/api/v1/leases/{lease_id}/terminate/", {}, format="json"
        ).status_code == 403

    def test_the_ledger_is_invisible_below_the_finance_function(
        self, db, auth_client, make_user
    ):
        """account_entry: a scope the table does not mention sees NOTHING — and for the
        ledger that silence is most of the point."""
        agent = make_user("agent")
        assert auth_client(agent).get("/api/v1/account-entries/").data["count"] == 0
        fin = make_user("finance")
        assert auth_client(fin).get("/api/v1/account-entries/").status_code == 200

    def test_wrong_branch_staff_see_nothing(
        self, db, tenant_portal, auth_client, make_user, other_branch
    ):
        from apps.identity.models import Team

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARL")
        outsider = make_user("manager", branch=other_branch, team=far_team)
        assert auth_client(outsider).get("/api/v1/leases/").data["count"] == 0
