"""
Seed two clinics for quarterly billing UI tests:

  qwarn  — paid quarter ending soon (warning, full clinic access)
  qgrace — paid quarter ended (grace, manager payment portal only)

Run: python manage.py seed_quarterly_test_tenants
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tenants.billing import PERIOD_DAYS
from tenants.models import TenantAccount, UserProfile

User = get_user_model()

WARNING_DAYS = 10
GRACE_DAYS = 10

DEFAULT_PASSWORD = "test1234"

SEEDS = (
    {
        "tin": "3111111111",
        "name": "Quarter Warn Test Clinic",
        "username": "qwarn",
        "billing_notes": "QA seed: quarterly renewal approaching (warning window).",
        "paid_until_offset_days": 7,
    },
    {
        "tin": "3222222222",
        "name": "Quarter Grace Test Clinic",
        "username": "qgrace",
        "billing_notes": "QA seed: quarterly period ended (grace — payment portal).",
        "paid_until_offset_days": -5,
    },
)


def _period_status(tenant: TenantAccount, *, now) -> str:
    """Mirror mediflow tenants.billing._quarterly_period_status for QA output."""
    quarterly_fee = int(tenant.quarterly_fee_etb or 0)
    if quarterly_fee <= 0:
        return "active"
    paid_until = tenant.subscription_paid_until
    if not paid_until:
        return "expired"
    if now.date() < paid_until.date():
        if paid_until - now <= timedelta(days=WARNING_DAYS):
            return "warning"
        return "active"
    overdue = now - paid_until
    if overdue.total_seconds() < 0:
        overdue = timedelta(0)
    if overdue <= timedelta(days=GRACE_DAYS):
        return "grace"
    return "expired"


def _upsert_tenant(seed: dict, *, now) -> TenantAccount:
    tin = seed["tin"]
    paid_until = now + timedelta(days=seed["paid_until_offset_days"])
    billing_started_at = paid_until - timedelta(days=PERIOD_DAYS)

    tenant, created = TenantAccount.objects.get_or_create(
        pharmacy_tin=tin,
        defaults={
            "pharmacy_name": seed["name"],
            "account_status": TenantAccount.STATUS_ACTIVE,
            "setup_fee_etb": 15000,
            "quarterly_fee_etb": 5000,
            "setup_fee_approved": True,
            "subscription_payment_approved": True,
            "subscription_paid_until": paid_until,
            "billing_started_at": billing_started_at,
            "paid_quarters_count": 1,
            "provisioned_by_apex": True,
            "free_trial_ends_at": None,
            "billing_hold": False,
            "billing_notes": seed["billing_notes"],
            "payment_channel": "",
            "payment_transaction_ref": "",
            "fees_manually_set": True,
            "ops_mode": "online",
        },
    )
    if not created:
        tenant.pharmacy_name = seed["name"]
        tenant.account_status = TenantAccount.STATUS_ACTIVE
        tenant.setup_fee_approved = True
        tenant.subscription_payment_approved = True
        tenant.subscription_paid_until = paid_until
        tenant.billing_started_at = billing_started_at
        tenant.paid_quarters_count = 1
        tenant.provisioned_by_apex = True
        tenant.free_trial_ends_at = None
        tenant.billing_hold = False
        tenant.billing_notes = seed["billing_notes"]
        tenant.payment_channel = ""
        tenant.payment_transaction_ref = ""
        tenant.fees_manually_set = True
        tenant.save()
    return tenant


def _upsert_manager(seed: dict, tenant: TenantAccount) -> User:
    username = seed["username"]
    user = User.objects.filter(username__iexact=username).first()
    if not user:
        user = User(username=username)
        user.set_password(DEFAULT_PASSWORD)
        user.save()
    else:
        user.set_password(DEFAULT_PASSWORD)
        user.is_active = True
        user.save()

    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            "pharmacy_name": tenant.pharmacy_name,
            "role": "manager",
            "pharmacy_tin": tenant.pharmacy_tin,
            "branch_name": "Main",
            "is_active": True,
        },
    )
    profile.pharmacy_name = tenant.pharmacy_name
    profile.role = "manager"
    profile.pharmacy_tin = tenant.pharmacy_tin
    profile.branch_name = "Main"
    profile.is_active = True
    profile.save()
    return user


class Command(BaseCommand):
    help = "Seed quarterly warning + grace test clinics (qwarn, qgrace)."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        self.stdout.write(self.style.MIGRATE_HEADING("Seeding quarterly billing test tenants…"))

        for seed in SEEDS:
            tenant = _upsert_tenant(seed, now=now)
            user = _upsert_manager(seed, tenant)
            period = _period_status(tenant, now=now)
            days_left = (tenant.subscription_paid_until.date() - now.date()).days

            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"  {tenant.pharmacy_name}"))
            self.stdout.write(f"    TIN:      {tenant.pharmacy_tin}")
            self.stdout.write(f"    Manager:  {user.username} / {DEFAULT_PASSWORD}")
            self.stdout.write(f"    Status:   {period}")
            self.stdout.write(f"    Paid until: {tenant.subscription_paid_until.isoformat()}")
            self.stdout.write(f"    Days until due: {days_left}")

        self.stdout.write("")
        self.stdout.write("Expected MediFlow behaviour:")
        self.stdout.write("  qwarn  — subscription alert bell, full clinic access, /billing shows quarterly due soon")
        self.stdout.write("  qgrace — staff login blocked; manager lands on /billing (payment portal)")
