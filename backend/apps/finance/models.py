"""`finance` models (architecture.md §11).

Accounts and ledger, commission plans, invoices, payments and allocations, cheques,
commissions and splits, installment plans, expenses, owner statements, reconciliation.

**All creates/updates/posts of money go through `finance.services` only** (§11 header, §1.2).
No other app writes a row in this module, and no signal creates one.

Two invariants the spec assigns to the service layer, recorded here so the services pass
inherits them rather than rediscovering them:

* ``Invoice.amount_paid`` MUST equal SUM of non-reversed allocations against that invoice.
  It is maintained by the allocation service inside ``transaction.atomic()``; the spec
  recommends a DB trigger doing the recompute. Until that exists the CHECK below constrains
  the *arithmetic* (balance_due = total - paid) but nothing ties `amount_paid` to reality —
  never write it from a view.
* ``CommissionSplit`` percentages across one commission sum to 100 (or the amounts sum to
  ``net_commission``), enforced on commission approval.

FX (§2, §4): when ``identity.Company.fx_mode`` is SINGLE_CURRENCY, `currency` must equal the
company default and the FX columns stay null. Under MULTI_CURRENCY, `exchange_rate` and
`amount_base` carry the conversion to company base currency.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import AppendOnlyModel, BaseModel


class Account(models.Model):
    """A bank, trust, cash, or mobile-money account.

    Trust accounts matter: client money held on account must be segregable from operating
    funds for reconciliation and audit.
    """

    class AccountType(models.TextChoices):
        OPERATING_ACCOUNT = "OPERATING_ACCOUNT", "Operating account"
        TRUST_ACCOUNT = "TRUST_ACCOUNT", "Trust account"
        CASH = "CASH", "Cash"
        MOBILE_MONEY = "MOBILE_MONEY", "Mobile money"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    account_type = models.CharField(max_length=30, choices=AccountType.choices)
    account_number = models.CharField(max_length=100, null=True, blank=True)
    currency = models.CharField(max_length=3)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "finance_account"

    def __str__(self):
        return self.name


class AccountEntry(AppendOnlyModel):
    """A ledger line against an account.

    Append-only, enforced by a database-role REVOKE (finance/0002). A posted entry is never
    updated or deleted — corrections are made by posting an opposite entry, so the history of
    what was believed when stays intact. `Reconciliation.closing_balance_system` is derived
    from the sum of these.
    """

    class EntryType(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    class ReferenceType(models.TextChoices):
        PAYMENT = "PAYMENT", "Payment"
        ALLOCATION = "ALLOCATION", "Allocation"
        RECONCILIATION = "RECONCILIATION", "Reconciliation"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        CHEQUE = "CHEQUE", "Cheque"
        COMMISSION = "COMMISSION", "Commission"

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="entries")
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    reference_type = models.CharField(
        max_length=20, choices=ReferenceType.choices, null=True, blank=True
    )
    reference_id = models.UUIDField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    posted_at = models.DateTimeField()

    class Meta:
        db_table = "finance_account_entry"


class CommissionPlan(models.Model):
    """How a commission is computed (SRS 3.6.4).

    `config` carries tier bands, split rules, and head-office fee rules for the non-flat plan
    types; `rate` only means anything for FLAT_PERCENT and FIXED_AMOUNT.
    """

    class PlanType(models.TextChoices):
        FLAT_PERCENT = "FLAT_PERCENT", "Flat percent"
        FIXED_AMOUNT = "FIXED_AMOUNT", "Fixed amount"
        TIERED = "TIERED", "Tiered"
        SPLIT = "SPLIT", "Split"

    class AppliesTo(models.TextChoices):
        SALE = "SALE", "Sale"
        RENTAL = "RENTAL", "Rental"
        BOTH = "BOTH", "Both"

    class Base(models.TextChoices):
        GROSS_AMOUNT = "GROSS_AMOUNT", "Gross amount"
        NET_AMOUNT = "NET_AMOUNT", "Net amount"
        RENT_PERIODS = "RENT_PERIODS", "Rent periods"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    plan_type = models.CharField(max_length=20, choices=PlanType.choices)
    applies_to = models.CharField(max_length=10, choices=AppliesTo.choices)
    base = models.CharField(max_length=20, choices=Base.choices)
    rate = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_commission_plan"

    def __str__(self):
        return self.name


class Invoice(models.Model):
    """A receivable.

    `amount_paid` is NOT free-form — see the module docstring. The CHECK here keeps the
    arithmetic self-consistent; keeping it *true* is the allocation service's job.
    """

    class InvoiceType(models.TextChoices):
        SALE = "SALE", "Sale"
        RENT = "RENT", "Rent"
        COMMISSION = "COMMISSION", "Commission"
        MANAGEMENT_FEE = "MANAGEMENT_FEE", "Management fee"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        PAID = "PAID", "Paid"
        OVERDUE = "OVERDUE", "Overdue"
        VOID = "VOID", "Void"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=50, unique=True)
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="invoices"
    )
    deal = models.ForeignKey(
        "crm.Deal", null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    transaction = models.ForeignKey(
        "crm.Transaction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    invoice_type = models.CharField(max_length=20, choices=InvoiceType.choices)
    issue_date = models.DateField()
    due_date = models.DateField()
    subtotal = models.DecimalField(max_digits=15, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_invoice"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0), name="finance_invoice_total_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    balance_due=models.F("total_amount") - models.F("amount_paid")
                ),
                name="finance_invoice_balance_consistent",
            ),
        ]

    def __str__(self):
        return self.invoice_number


class InvoiceLine(models.Model):
    """A line item. `property` is optional and enables per-property revenue reporting."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=15, decimal_places=2)
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
    )

    class Meta:
        db_table = "finance_invoice_line"


class Payment(models.Model):
    """Cash received.

    Posted payments are never edited or deleted; `status` moves to REVERSED or REFUNDED and a
    compensating entry is posted instead.
    """

    class PaymentMethod(models.TextChoices):
        BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
        CHEQUE = "CHEQUE", "Cheque"
        CREDIT_CARD = "CREDIT_CARD", "Credit card"
        CASH = "CASH", "Cash"

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"
        REFUNDED = "REFUNDED", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment_reference = models.CharField(max_length=50, unique=True)
    payer = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="payments"
    )
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    payment_date = models.DateField()
    external_reference = models.CharField(max_length=200, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.POSTED)
    notes = models.TextField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_payments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_payment"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="finance_payment_amount_positive"
            )
        ]

    def __str__(self):
        return self.payment_reference


class PaymentAllocation(models.Model):
    """Applies part or all of a payment to an invoice.

    This is the table that drives `Invoice.amount_paid`. The unique constraint means a payment
    hits any given invoice at most once — top-ups adjust the existing allocation rather than
    stacking rows, which keeps the sum unambiguous.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="allocations")
    allocated_amount = models.DecimalField(max_digits=15, decimal_places=2)
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    allocated_amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_payment_allocation"
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "invoice"], name="finance_allocation_payment_invoice_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(allocated_amount__gt=0),
                name="finance_allocation_amount_positive",
            ),
        ]


class Cheque(models.Model):
    """Post-dated cheque register.

    Models the PDC rent-collection practice common in Gulf/ME markets: cheques are held,
    deposited on their date, and may clear, bounce, or be replaced.
    """

    class Status(models.TextChoices):
        HELD_IN_SAFE = "HELD_IN_SAFE", "Held in safe"
        DEPOSITED = "DEPOSITED", "Deposited"
        CLEARED = "CLEARED", "Cleared"
        BOUNCED = "BOUNCED", "Bounced"
        REPLACED = "REPLACED", "Replaced"
        RETURNED = "RETURNED", "Returned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cheques",
    )
    payer = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="cheques"
    )
    drawer_name = models.CharField(max_length=200)
    bank_name = models.CharField(max_length=200)
    cheque_number = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    cheque_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.HELD_IN_SAFE
    )
    deposit_account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="cheques"
    )
    cleared_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    bounce_reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_cheque"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="finance_cheque_amount_positive"
            )
        ]


class Commission(models.Model):
    """Agent commission for a sale **or** a letting — never both, never neither.

    The XOR is the point: sale commissions hang off a `crm.Transaction`, letting commissions
    off a `property_ops.Lease`. §11 is explicit that a rental-only commission must never be
    forced to invent a fake transaction to hang from.
    """

    class Status(models.TextChoices):
        CALCULATED = "CALCULATED", "Calculated"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        "crm.Transaction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="commissions",
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="commissions",
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="commissions"
    )
    commission_plan = models.ForeignKey(
        CommissionPlan, on_delete=models.PROTECT, related_name="commissions"
    )
    gross_commission = models.DecimalField(max_digits=15, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    net_commission = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CALCULATED
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_commission"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(transaction__isnull=False, lease__isnull=True)
                    | models.Q(transaction__isnull=True, lease__isnull=False)
                ),
                name="finance_commission_source_xor",
            )
        ]


class CommissionSplit(models.Model):
    """Distribution of a commission across agents, brokers, and external referrers.

    Percentages across one commission sum to 100 — enforced in the approval service, since a
    CHECK cannot see sibling rows.
    """

    class RecipientType(models.TextChoices):
        AGENT = "AGENT", "Agent"
        BROKER = "BROKER", "Broker"
        REFERRAL_EXTERNAL = "REFERRAL_EXTERNAL", "External referral"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    commission = models.ForeignKey(
        Commission, on_delete=models.CASCADE, related_name="splits"
    )
    recipient_type = models.CharField(max_length=20, choices=RecipientType.choices)
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="commission_splits",
    )
    recipient_contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="commission_splits",
    )
    percentage = models.DecimalField(max_digits=7, decimal_places=4)
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "finance_commission_split"
        constraints = [
            # Exactly one recipient. An internal split names a user, an external referral
            # names a contact; a split naming both or neither cannot be paid.
            models.CheckConstraint(
                condition=(
                    models.Q(recipient_user__isnull=False, recipient_contact__isnull=True)
                    | models.Q(recipient_user__isnull=True, recipient_contact__isnull=False)
                ),
                name="finance_commission_split_recipient_xor",
            ),
        ]


class InstallmentPlan(BaseModel):
    """Off-plan / milestone payment schedule for a sales transaction (SRS 3.6.3/3.6.8).

    Created only via `finance.services.create_installment_plan`.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    transaction = models.ForeignKey(
        "crm.Transaction", on_delete=models.PROTECT, related_name="installment_plans"
    )
    name = models.CharField(max_length=200)
    currency = models.CharField(max_length=3)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        db_table = "finance_installment_plan"


class InstallmentMilestone(models.Model):
    """One milestone of an installment plan.

    When `status` becomes INVOICED, `invoice` must be set — the milestone and the invoice that
    bills it are two halves of the same fact.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        INVOICED = "INVOICED", "Invoiced"
        PAID = "PAID", "Paid"
        WAIVED = "WAIVED", "Waived"
        OVERDUE = "OVERDUE", "Overdue"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        InstallmentPlan, on_delete=models.CASCADE, related_name="milestones"
    )
    label = models.CharField(max_length=200)
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    percentage = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    sort_order = models.IntegerField(default=0)
    invoice = models.ForeignKey(
        Invoice,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="installment_milestones",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_installment_milestone"
        ordering = ["plan", "sort_order"]
        constraints = [
            # The spec states this as a service-layer invariant; it is expressible as a CHECK
            # because both columns live on this row, so it costs nothing to make it absolute.
            models.CheckConstraint(
                condition=~models.Q(status="INVOICED") | models.Q(invoice__isnull=False),
                name="finance_milestone_invoiced_has_invoice",
            )
        ]


class Expense(BaseModel):
    """An operational or property expense.

    A billable expense rolls onto the landlord's owner statement, which is how a maintenance
    cost reaches the person who ultimately pays it (SRS 3.14.4).
    """

    class Category(models.TextChoices):
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        UTILITIES = "UTILITIES", "Utilities"
        MANAGEMENT = "MANAGEMENT", "Management"
        TAX = "TAX", "Tax"
        INSURANCE = "INSURANCE", "Insurance"
        MARKETING = "MARKETING", "Marketing"
        PAYROLL = "PAYROLL", "Payroll"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        PAID = "PAID", "Paid"
        VOID = "VOID", "Void"

    expense_number = models.CharField(max_length=50, unique=True)
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    unit = models.ForeignKey(
        "inventory.Unit",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    maintenance_request = models.ForeignKey(
        "property_ops.MaintenanceRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
        db_column="property_ops_maintenance_request_id",
    )
    vendor_contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="vendor_expenses",
    )
    account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="expenses"
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    currency = models.CharField(max_length=3)
    expense_date = models.DateField()
    is_billable_to_owner = models.BooleanField(default=False)
    owner_statement = models.ForeignKey(
        "finance.OwnerStatement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Added by finance/0003 — the receipt or invoice scan backing this expense.
    document = models.ForeignKey(
        "collaboration.Document",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )

    class Meta:
        db_table = "finance_expense"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="finance_expense_amount_non_negative"
            )
        ]

    def __str__(self):
        return self.expense_number


class OwnerStatement(BaseModel):
    """Landlord remittance: rent collected minus fees and expenses = net payable (SRS 3.5.6).

    Powers the Property Manager and Landlord-portal dashboards.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"
        PAID = "PAID", "Paid"

    statement_number = models.CharField(max_length=50, unique=True)
    owner_contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="owner_statements"
    )
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="owner_statements",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    gross_rent_collected = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    management_fees = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    expenses_total = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    other_deductions = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    net_payable = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Added by finance/0003 — the rendered PDF sent to the landlord.
    document = models.ForeignKey(
        "collaboration.Document",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owner_statements",
    )

    class Meta:
        db_table = "finance_owner_statement"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gt=models.F("period_start")),
                name="finance_owner_statement_period_valid",
            )
        ]

    def __str__(self):
        return self.statement_number


class OwnerStatementLine(models.Model):
    """An itemized row behind a statement total.

    `reference_type`/`reference_id` link back to the invoice, payment, or expense that
    produced the line, so a landlord querying a number can be shown its source.
    """

    class LineType(models.TextChoices):
        RENT = "RENT", "Rent"
        MANAGEMENT_FEE = "MANAGEMENT_FEE", "Management fee"
        EXPENSE = "EXPENSE", "Expense"
        DEDUCTION = "DEDUCTION", "Deduction"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    statement = models.ForeignKey(
        OwnerStatement, on_delete=models.CASCADE, related_name="lines"
    )
    line_type = models.CharField(max_length=20, choices=LineType.choices)
    description = models.CharField(max_length=300)
    reference_type = models.CharField(max_length=30, null=True, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    occurred_on = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "finance_owner_statement_line"


class Reconciliation(models.Model):
    """Bank/trust/clearing-account reconciliation performed by the Finance role.

    `closing_balance_system` is derived from the sum of `AccountEntry` rows for the account.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RECONCILED = "RECONCILED", "Reconciled"
        DISCREPANCY = "DISCREPANCY", "Discrepancy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="reconciliations"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2)
    closing_balance_statement = models.DecimalField(max_digits=15, decimal_places=2)
    closing_balance_system = models.DecimalField(max_digits=15, decimal_places=2)
    difference = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_reconciliation"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gt=models.F("period_start")),
                name="finance_reconciliation_period_valid",
            ),
            # difference is defined as statement - system, so it is not free-form.
            models.CheckConstraint(
                condition=models.Q(
                    difference=models.F("closing_balance_statement")
                    - models.F("closing_balance_system")
                ),
                name="finance_reconciliation_difference_consistent",
            ),
            # "RECONCILED requires difference = 0" (§11). A reconciliation that claims to
            # balance while showing a discrepancy is exactly the state audits look for.
            models.CheckConstraint(
                condition=~models.Q(status="RECONCILED") | models.Q(difference=0),
                name="finance_reconciliation_balanced_when_reconciled",
            ),
        ]
