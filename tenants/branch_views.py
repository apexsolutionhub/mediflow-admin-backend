"""Org branches for a tenant (shared clinic_clinicbranch table)."""

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apex.permissions import IsApexAuthenticated

from .models import TenantAccount, UserProfile

User = get_user_model()

class ClinicBranch(models.Model):
    """Unmanaged mirror of mediflow clinic.ClinicBranch."""

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_tin = models.CharField(max_length=50, blank=True, default="")
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255, blank=True, default="")
    is_main = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = "clinic_clinicbranch"
        ordering = ["-is_main", "name"]
        app_label = "tenants"

    def operational_tin(self) -> str:
        return (self.branch_tin or self.clinic_tin or "").strip()


class Department(models.Model):
    """Unmanaged mirror of mediflow clinic.Department."""

    clinic_tin = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=120)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = "clinic_department"
        app_label = "tenants"


class BillableService(models.Model):
    """Unmanaged mirror of mediflow clinic.BillableService."""

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    department = models.CharField(max_length=120)
    service_type = models.CharField(max_length=32, default="other")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    default_quantity = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    auto_add_on_registration = models.BooleanField(default=False)
    requires_payment_before_work = models.BooleanField(default=True)
    internal_notes = models.TextField(blank=True, default="")

    class Meta:
        managed = False
        db_table = "clinic_billableservice"
        app_label = "tenants"


def seed_branch_catalog(*, clinic_tin: str, branch_name: str) -> None:
    tin = (clinic_tin or "").strip()
    branch = (branch_name or "").strip() or "Main"
    if not tin:
        return
    Department.objects.get_or_create(
        clinic_tin=tin,
        name="General",
        branch_name=branch,
        defaults={"is_active": True, "created_at": timezone.now()},
    )
    defaults = [
        ("CONSULT", "Consultation", "Consultation", "consultation", 300, True),
        ("LAB-CBC", "Complete Blood Count", "Laboratory", "lab", 250, False),
        ("RAD-XRAY", "Chest X-ray", "Radiology", "radiology", 400, False),
        ("RX-DISP", "Pharmacy dispensing", "Pharmacy", "pharmacy", 50, False),
    ]
    for code, name, dept, service_type, price, auto_add in defaults:
        BillableService.objects.get_or_create(
            clinic_tin=tin,
            code=code,
            branch_name=branch,
            defaults={
                "name": name,
                "department": dept,
                "service_type": service_type,
                "unit_price": price,
                "auto_add_on_registration": auto_add,
            },
        )


def provision_branch_billing_account(*, org_tenant: TenantAccount, branch_tin: str, branch_name: str):
    """Each branch TIN gets its own subscription account.

    Satellites do not re-run org setup payment. Fees default from the org; Apex can
    customize (including free trial) per branch in Billing.
    """
    op = (branch_tin or "").strip()
    if not op:
        return None
    existing = TenantAccount.objects.filter(pharmacy_tin=op).first()
    if existing:
        return heal_satellite_billing_account(account=existing, org_tenant=org_tenant)

    label = (branch_name or "").strip() or op
    org_name = (org_tenant.pharmacy_name or "").strip()
    display = f"{org_name} — {label}".strip(" —") if org_name else label
    return TenantAccount.objects.create(
        pharmacy_tin=op,
        pharmacy_name=display,
        branch_name=label,
        logo_url=org_tenant.logo_url or "",
        account_status=TenantAccount.STATUS_ACTIVE,
        setup_fee_etb=org_tenant.setup_fee_etb,
        quarterly_fee_etb=org_tenant.quarterly_fee_etb,
        yearly_fee_etb=getattr(org_tenant, "yearly_fee_etb", 0) or 0,
        # Org already onboarded — satellites skip setup pending.
        setup_fee_approved=True,
        subscription_payment_approved=bool(org_tenant.subscription_payment_approved),
        subscription_paid_until=org_tenant.subscription_paid_until,
        paid_quarters_count=org_tenant.paid_quarters_count or 0,
        billing_started_at=org_tenant.billing_started_at,
        provisioned_by_apex=True,
        is_illustration=bool(org_tenant.is_illustration),
        fees_manually_set=bool(org_tenant.fees_manually_set),
        modules=list(org_tenant.modules or []),
        ops_mode=org_tenant.ops_mode or TenantAccount.OPS_MODE_ONLINE,
        # Free trial is optional and set per branch in Apex Billing — not auto-assigned.
        free_trial_ends_at=None,
        sales_agent=org_tenant.sales_agent,
    )


def heal_satellite_billing_account(*, account: TenantAccount, org_tenant: TenantAccount | None = None):
    """Fix legacy satellite rows that were left in setup-pending / auto-trial state."""
    if not account:
        return account
    org = org_tenant
    if org is None:
        branch = ClinicBranch.objects.filter(branch_tin__iexact=account.pharmacy_tin).first()
        if branch and (branch.clinic_tin or "").strip().lower() != (account.pharmacy_tin or "").strip().lower():
            org = TenantAccount.objects.filter(pharmacy_tin=branch.clinic_tin).first()

    changed: list[str] = []
    was_setup_pending = not account.setup_fee_approved
    if was_setup_pending:
        account.setup_fee_approved = True
        changed.append("setup_fee_approved")
        # Legacy provision put satellites on auto-trial + setup pending.
        # Clear that trial unless the org itself is on trial; Apex can re-add in Billing.
        org_trial = getattr(org, "free_trial_ends_at", None) if org else None
        if account.free_trial_ends_at and not org_trial:
            account.free_trial_ends_at = None
            changed.append("free_trial_ends_at")

    if org and not account.subscription_payment_approved and org.subscription_payment_approved:
        account.subscription_payment_approved = True
        changed.append("subscription_payment_approved")
        if not account.subscription_paid_until and org.subscription_paid_until:
            account.subscription_paid_until = org.subscription_paid_until
            changed.append("subscription_paid_until")
        if not account.paid_quarters_count and org.paid_quarters_count:
            account.paid_quarters_count = org.paid_quarters_count
            changed.append("paid_quarters_count")
        if not account.billing_started_at and org.billing_started_at:
            account.billing_started_at = org.billing_started_at
            changed.append("billing_started_at")

    if org and account.is_illustration != bool(org.is_illustration):
        account.is_illustration = bool(org.is_illustration)
        changed.append("is_illustration")

    if changed:
        changed.append("updated_at")
        account.save(update_fields=changed)
    return account


def heal_org_satellite_billing_accounts(*, org_tin: str) -> None:
    org = (org_tin or "").strip()
    if not org:
        return
    org_tenant = TenantAccount.objects.filter(pharmacy_tin=org).first()
    if not org_tenant:
        return
    for branch in ClinicBranch.objects.filter(clinic_tin__iexact=org):
        op = branch.operational_tin()
        if not op or op.lower() == org.lower():
            continue
        account = TenantAccount.objects.filter(pharmacy_tin=op).first()
        if account:
            heal_satellite_billing_account(account=account, org_tenant=org_tenant)
        else:
            provision_branch_billing_account(
                org_tenant=org_tenant, branch_tin=op, branch_name=branch.name
            )


def _serialize_branch(branch: ClinicBranch) -> dict:
    return {
        "id": branch.id,
        "name": branch.name,
        "address": branch.address,
        "branch_tin": branch.operational_tin(),
        "is_main": branch.is_main,
        "is_active": branch.is_active,
        "created_at": branch.created_at,
    }


def promote_branch_to_main(*, org_tin: str, branch: ClinicBranch) -> list[str]:
    """Ensure exactly one main branch for the org. Returns names demoted to satellite."""
    org = (org_tin or "").strip()
    demoted = list(
        ClinicBranch.objects.filter(clinic_tin__iexact=org, is_main=True)
        .exclude(pk=branch.pk)
        .values_list("name", flat=True)
    )
    ClinicBranch.objects.filter(clinic_tin__iexact=org, is_main=True).exclude(
        pk=branch.pk
    ).update(is_main=False)
    if not branch.is_main:
        branch.is_main = True
        branch.save(update_fields=["is_main"])
    tenant = TenantAccount.objects.filter(pharmacy_tin__iexact=org).first()
    if tenant and (tenant.branch_name or "").strip() != (branch.name or "").strip():
        tenant.branch_name = branch.name
        tenant.save(update_fields=["branch_name", "updated_at"])
    return [str(name) for name in demoted if name]


def generate_branch_tin() -> str:
    """A unique placeholder TIN for a branch created without one (e.g. GEN-4F9A21C7B0).

    It becomes the branch's permanent key: a TIN is never changed once data exists under it.
    """
    import secrets

    while True:
        tin = f"GEN-{secrets.token_hex(5).upper()}"
        if (
            not TenantAccount.objects.filter(pharmacy_tin__iexact=tin).exists()
            and not ClinicBranch.objects.filter(branch_tin__iexact=tin).exists()
            and not UserProfile.objects.filter(pharmacy_tin__iexact=tin).exists()
        ):
            return tin


def _validate_branch_tin(*, org_tin: str, branch_tin: str, exclude_pk=None):
    op = (branch_tin or "").strip()
    org = (org_tin or "").strip()
    if not op:
        return "Branch TIN is required."
    qs = ClinicBranch.objects.filter(branch_tin__iexact=op)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        return "That branch TIN is already used by another branch."
    other_org = TenantAccount.objects.filter(pharmacy_tin__iexact=op).exclude(
        pharmacy_tin__iexact=org
    )
    if other_org.exists():
        return "That TIN belongs to another organization."
    if op.lower() != org.lower() and UserProfile.objects.filter(pharmacy_tin__iexact=op).exists():
        return "That TIN is already assigned to staff."
    return None


class TenantBranchesView(APIView):
    permission_classes = [IsApexAuthenticated]

    def get(self, request, tin: str):
        tin = (tin or "").strip()
        if not TenantAccount.objects.filter(pharmacy_tin=tin).exists():
            return Response({"detail": "Tenant not found."}, status=404)
        rows = list(ClinicBranch.objects.filter(clinic_tin__iexact=tin))
        has_main = any(b.is_main for b in rows)
        # Heal accidental multi-main rows (keep the first by id).
        mains = [b for b in rows if b.is_main]
        if len(mains) > 1:
            keeper = sorted(mains, key=lambda b: b.id)[0]
            promote_branch_to_main(org_tin=tin, branch=keeper)
            rows = list(ClinicBranch.objects.filter(clinic_tin__iexact=tin))
            has_main = any(b.is_main for b in rows)
        # Satellites must not sit in setup-pending; heal legacy rows.
        heal_org_satellite_billing_accounts(org_tin=tin)
        return Response(
            {
                "results": [_serialize_branch(b) for b in rows],
                "has_main": has_main,
            }
        )

    @transaction.atomic
    def post(self, request, tin: str):
        tin = (tin or "").strip()
        tenant = TenantAccount.objects.filter(pharmacy_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=404)
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Branch name is required."}, status=400)
        if ClinicBranch.objects.filter(clinic_tin=tin, name__iexact=name).exists():
            return Response({"detail": "A branch with that name already exists."}, status=400)

        branch_tin = (request.data.get("branch_tin") or "").strip()
        generated_tin = False

        manager_username = (request.data.get("manager_username") or "").strip()
        manager_password = request.data.get("manager_password") or ""
        if not manager_username:
            return Response({"detail": "Branch manager username is required."}, status=400)
        if len(str(manager_password)) < 6:
            return Response(
                {"detail": "Branch manager password must be at least 6 characters."},
                status=400,
            )
        if User.objects.filter(username__iexact=manager_username).exists():
            return Response({"detail": "That manager username is already taken."}, status=400)

        has_main = ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).exists()
        is_main = bool(request.data.get("is_main"))
        demoted: list[str] = []
        if has_main and is_main:
            # Creating as main transfers the flag — previous main becomes a satellite.
            demoted = list(
                ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).values_list(
                    "name", flat=True
                )
            )
            ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).update(is_main=False)
            is_main = True
        elif has_main:
            is_main = False
        elif is_main or not ClinicBranch.objects.filter(clinic_tin__iexact=tin).exists():
            ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).update(is_main=False)
            is_main = True

        if not branch_tin:
            if is_main:
                branch_tin = tin  # the main branch is the organization's own location
            else:
                # No TIN given: this branch gets its own unique one (never the organization's), so it keeps its
                # own billing, staff, mode and clinic-server token.
                branch_tin = generate_branch_tin()
                generated_tin = True

        # Satellite branches must use a TIN different from the org TIN.
        if not is_main and branch_tin.lower() == tin.lower():
            return Response(
                {"detail": "Satellite branches need their own TIN (different from the organization TIN)."},
                status=400,
            )

        tin_error = _validate_branch_tin(org_tin=tin, branch_tin=branch_tin)
        if tin_error:
            return Response({"detail": tin_error}, status=400)

        branch = ClinicBranch.objects.create(
            clinic_tin=tin,
            branch_tin=branch_tin,
            name=name,
            address=(request.data.get("address") or "").strip(),
            is_main=is_main,
            is_active=request.data.get("is_active", True) is not False,
            created_at=timezone.now(),
        )

        user = User(username=manager_username)
        user.set_password(manager_password)
        user.save()
        UserProfile.objects.create(
            user=user,
            pharmacy_name=tenant.pharmacy_name,
            role="manager",
            logoUrl=tenant.logo_url or "",
            pharmacy_tin=branch_tin,
            branch_name=branch.name,
        )

        seed_branch_catalog(clinic_tin=branch_tin, branch_name=branch.name)
        provision_branch_billing_account(
            org_tenant=tenant, branch_tin=branch_tin, branch_name=branch.name
        )

        if is_main and tenant.branch_name != branch.name:
            tenant.branch_name = branch.name
            tenant.save(update_fields=["branch_name", "updated_at"])

        payload = _serialize_branch(branch)
        payload["manager_username"] = manager_username
        payload["tin_generated"] = generated_tin
        if demoted:
            payload["demoted_mains"] = demoted
        return Response(payload, status=status.HTTP_201_CREATED)


class TenantBranchDetailView(APIView):
    permission_classes = [IsApexAuthenticated]

    @transaction.atomic
    def patch(self, request, tin: str, pk: int):
        tin = (tin or "").strip()
        branch = ClinicBranch.objects.filter(clinic_tin__iexact=tin, pk=pk).first()
        if not branch:
            return Response({"detail": "Branch not found."}, status=404)
        if "name" in request.data:
            name = (request.data.get("name") or "").strip()
            if not name:
                return Response({"detail": "Branch name is required."}, status=400)
            if (
                ClinicBranch.objects.filter(clinic_tin__iexact=tin, name__iexact=name)
                .exclude(pk=pk)
                .exists()
            ):
                return Response({"detail": "A branch with that name already exists."}, status=400)
            branch.name = name
        if "address" in request.data:
            branch.address = (request.data.get("address") or "").strip()
        if "branch_tin" in request.data:
            branch_tin = (request.data.get("branch_tin") or "").strip()
            if not branch.is_main and branch_tin.lower() == tin.lower():
                return Response(
                    {
                        "detail": "Satellite branches need their own TIN (different from the organization TIN)."
                    },
                    status=400,
                )
            tin_error = _validate_branch_tin(
                org_tin=tin, branch_tin=branch_tin, exclude_pk=pk
            )
            if tin_error:
                return Response({"detail": tin_error}, status=400)
            old_tin = branch.operational_tin()
            branch.branch_tin = branch_tin
            if old_tin.lower() != branch_tin.lower():
                UserProfile.objects.filter(
                    pharmacy_tin__iexact=old_tin, branch_name__iexact=branch.name
                ).update(pharmacy_tin=branch_tin)
                tenant = TenantAccount.objects.filter(pharmacy_tin__iexact=tin).first()
                if tenant:
                    provision_branch_billing_account(
                        org_tenant=tenant, branch_tin=branch_tin, branch_name=branch.name
                    )
        if "is_active" in request.data:
            branch.is_active = bool(request.data.get("is_active"))

        demoted: list[str] = []
        want_main = request.data.get("is_main")
        if want_main is True or str(want_main).lower() in {"1", "true", "yes"}:
            demoted = promote_branch_to_main(org_tin=tin, branch=branch)

        branch.save()
        payload = _serialize_branch(branch)
        if demoted:
            payload["demoted_mains"] = demoted
        return Response(payload)

    def delete(self, request, tin: str, pk: int):
        tin = (tin or "").strip()
        branch = ClinicBranch.objects.filter(clinic_tin__iexact=tin, pk=pk).first()
        if not branch:
            return Response({"detail": "Branch not found."}, status=404)
        was_main = bool(branch.is_main)
        branch.delete()
        if was_main:
            # Keep org valid: promote the oldest remaining branch if any.
            next_main = (
                ClinicBranch.objects.filter(clinic_tin__iexact=tin)
                .order_by("id")
                .first()
            )
            if next_main:
                promote_branch_to_main(org_tin=tin, branch=next_main)
        return Response(status=status.HTTP_204_NO_CONTENT)
