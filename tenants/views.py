from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .billing import (
    approve_quarterly_payment,
    approve_setup_payment,
    approve_yearly_payment,
    reject_payment_submission,
    release_billing_hold,
)
from .clinic_identity import normalize_clinic_identity
from .tenant_provisioning import is_apex_provisioned_tenant
from .tenant_fees import apply_catalog_fees_to_tenant, clinic_catalog_fees, effective_tenant_fees
from .models import TenantAccount, TenantPaymentSubmission, UserProfile
from .pricing import write_audit

User = get_user_model()

DEFAULT_SETUP_FEE_ETB = 15000
DEFAULT_QUARTERLY_FEE_ETB = 5000
DEFAULT_YEARLY_FEE_ETB = 18000
DEFAULT_MODULES = ["Inventory", "Sales", "Reports"]


class TenantSerializer(serializers.ModelSerializer):
    manager_count = serializers.IntegerField(read_only=True, required=False)
    pharmacist_count = serializers.IntegerField(read_only=True, required=False)
    staff_count = serializers.IntegerField(read_only=True, required=False)
    manager_username = serializers.CharField(read_only=True, required=False, default="")
    sales_agent_id = serializers.IntegerField(read_only=True)
    sales_agent_name = serializers.SerializerMethodField()
    setup_review_status = serializers.CharField(read_only=True, required=False)

    class Meta:
        model = TenantAccount
        fields = [
            "id",
            "pharmacy_tin",
            "pharmacy_name",
            "phone",
            "logo_url",
            "account_status",
            "status_reason",
            "status_changed_at",
            "setup_fee_etb",
            "quarterly_fee_etb",
            "yearly_fee_etb",
            "payment_channel",
            "payment_transaction_ref",
            "setup_fee_approved",
            "subscription_payment_approved",
            "subscription_paid_until",
            "paid_quarters_count",
            "billing_hold",
            "billing_started_at",
            "free_trial_ends_at",
            "is_illustration",
            "provisioned_by_apex",
            "billing_notes",
            "modules",
            "fees_manually_set",
            "ops_mode",
            "created_at",
            "updated_at",
            "manager_count",
            "pharmacist_count",
            "staff_count",
            "manager_username",
            "sales_agent_id",
            "sales_agent_name",
            "setup_review_status",
        ]
        read_only_fields = [
            "id",
            "account_status",
            "status_reason",
            "status_changed_at",
            "payment_channel",
            "payment_transaction_ref",
            "setup_fee_approved",
            "subscription_payment_approved",
            "subscription_paid_until",
            "paid_quarters_count",
            "billing_started_at",
            "created_at",
            "updated_at",
            "manager_count",
            "pharmacist_count",
            "staff_count",
            "manager_username",
            "sales_agent_id",
            "sales_agent_name",
            "setup_review_status",
        ]

    def get_sales_agent_name(self, obj):
        agent = getattr(obj, "sales_agent", None)
        return getattr(agent, "display_name", "") if agent else ""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        tin, name = normalize_clinic_identity(
            data.get("pharmacy_tin", ""),
            data.get("pharmacy_name", ""),
        )
        data["pharmacy_tin"] = tin
        data["pharmacy_name"] = name
        setup_fee, quarterly_fee = effective_tenant_fees(instance)
        data["setup_fee_etb"] = setup_fee
        data["quarterly_fee_etb"] = quarterly_fee
        return data


class PaymentSubmissionSerializer(serializers.ModelSerializer):
    pharmacy_name = serializers.SerializerMethodField()

    class Meta:
        model = TenantPaymentSubmission
        fields = [
            "id",
            "pharmacy_tin",
            "pharmacy_name",
            "payment_kind",
            "amount_etb",
            "payment_channel",
            "transaction_ref",
            "status",
            "submitted_at",
            "approved_at",
            "rejected_at",
            "rejection_reason",
            "quarter_number",
        ]

    def get_pharmacy_name(self, obj):
        tenant = TenantAccount.objects.filter(pharmacy_tin=obj.pharmacy_tin).first()
        if tenant:
            _, name = normalize_clinic_identity(tenant.pharmacy_tin, tenant.pharmacy_name)
            return name
        return ""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        tin, name = normalize_clinic_identity(
            data.get("pharmacy_tin", ""),
            data.get("pharmacy_name", ""),
        )
        data["pharmacy_tin"] = tin
        data["pharmacy_name"] = name
        return data


class TenantCreateSerializer(serializers.Serializer):
    pharmacy_tin = serializers.CharField(max_length=50)
    pharmacy_name = serializers.CharField(max_length=255)
    logo_url = serializers.URLField(required=False, allow_blank=True, default="")
    manager_username = serializers.CharField(max_length=150)
    manager_password = serializers.CharField(write_only=True, min_length=6)
    branch_name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="Main")
    branch_address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    is_main_branch = serializers.BooleanField(required=False, default=True)
    setup_fee_etb = serializers.IntegerField(required=False, min_value=0, default=DEFAULT_SETUP_FEE_ETB)
    quarterly_fee_etb = serializers.IntegerField(
        required=False, min_value=0, default=DEFAULT_QUARTERLY_FEE_ETB
    )
    yearly_fee_etb = serializers.IntegerField(
        required=False, min_value=0, default=DEFAULT_YEARLY_FEE_ETB
    )
    modules = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=DEFAULT_MODULES,
    )
    waive_setup_fee = serializers.BooleanField(required=False, default=False)
    sales_agent_id = serializers.IntegerField(required=False, allow_null=True)
    ops_mode = serializers.ChoiceField(
        choices=["online", "offline"],
        required=False,
        default="online",
    )

    def validate_pharmacy_tin(self, value):
        tin = (value or "").strip()
        if not tin:
            raise serializers.ValidationError("Clinic TIN is required.")
        if TenantAccount.objects.filter(pharmacy_tin=tin).exists():
            raise serializers.ValidationError("A tenant with this TIN already exists.")
        if UserProfile.objects.filter(pharmacy_tin=tin).exists():
            raise serializers.ValidationError("This TIN is already used by clinic users.")
        return tin

    def validate_manager_username(self, value):
        username = (value or "").strip()
        if not username:
            raise serializers.ValidationError("Manager username is required.")
        if User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError("This username is already taken.")
        return username

    def validate(self, attrs):
        tin, name = normalize_clinic_identity(
            attrs.get("pharmacy_tin", ""),
            attrs.get("pharmacy_name", ""),
        )
        attrs["pharmacy_tin"] = tin
        attrs["pharmacy_name"] = name
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        tin = validated_data["pharmacy_tin"]
        pharmacy_name = validated_data["pharmacy_name"].strip()
        logo_url = (validated_data.get("logo_url") or "").strip()
        modules = validated_data.get("modules") or list(DEFAULT_MODULES)
        catalog = clinic_catalog_fees(modules)
        setup_fee = int(validated_data.get("setup_fee_etb", catalog["setup_fee_etb"]))
        quarterly_fee = int(validated_data.get("quarterly_fee_etb", catalog["quarterly_fee_etb"]))
        yearly_fee = int(validated_data.get("yearly_fee_etb", catalog["yearly_fee_etb"]))
        fees_manually_set = (
            "setup_fee_etb" in validated_data
            or "quarterly_fee_etb" in validated_data
            or "yearly_fee_etb" in validated_data
        ) and (
            setup_fee != catalog["setup_fee_etb"]
            or quarterly_fee != catalog["quarterly_fee_etb"]
            or yearly_fee != catalog["yearly_fee_etb"]
        )
        if not fees_manually_set:
            setup_fee = catalog["setup_fee_etb"]
            quarterly_fee = catalog["quarterly_fee_etb"]
            yearly_fee = catalog["yearly_fee_etb"]
        waive = bool(validated_data.get("waive_setup_fee"))
        from .sales_agents import resolve_sales_agent

        sales_agent = resolve_sales_agent(validated_data.get("sales_agent_id"))
        ops_mode = (validated_data.get("ops_mode") or "online").strip().lower()
        if ops_mode not in {"online", "offline"}:
            ops_mode = "online"

        user = User(username=validated_data["manager_username"])
        user.set_password(validated_data["manager_password"])
        user.save()

        branch_name = (validated_data.get("branch_name") or "Main").strip() or "Main"
        branch_address = (validated_data.get("branch_address") or "").strip()
        is_main_branch = validated_data.get("is_main_branch", True)
        if is_main_branch is None:
            is_main_branch = True

        UserProfile.objects.create(
            user=user,
            pharmacy_name=pharmacy_name,
            role="manager",
            logoUrl=logo_url,
            pharmacy_tin=tin,
            branch_name=branch_name,
        )

        now = timezone.now()
        tenant = TenantAccount.objects.create(
            pharmacy_tin=tin,
            pharmacy_name=pharmacy_name,
            logo_url=logo_url,
            branch_name=branch_name,
            account_status=TenantAccount.STATUS_ACTIVE,
            setup_fee_etb=setup_fee,
            quarterly_fee_etb=quarterly_fee,
            yearly_fee_etb=yearly_fee,
            modules=modules,
            ops_mode=ops_mode,
            provisioned_by_apex=True,
            fees_manually_set=fees_manually_set,
            setup_fee_approved=waive or setup_fee <= 0,
            subscription_payment_approved=(waive or setup_fee <= 0) and quarterly_fee > 0,
            paid_quarters_count=1 if ((waive or setup_fee <= 0) and quarterly_fee > 0) else 0,
            billing_started_at=now if ((waive or setup_fee <= 0) and quarterly_fee > 0) else None,
            subscription_paid_until=(
                now + timedelta(days=90)
                if ((waive or setup_fee <= 0) and quarterly_fee > 0)
                else None
            ),
            free_trial_ends_at=None,
            sales_agent=sales_agent,
        )

        # Shared clinic_clinicbranch table (same DB as MediFlow clinic app).
        from .branch_views import ClinicBranch

        want_main = bool(is_main_branch)
        if want_main or not ClinicBranch.objects.filter(clinic_tin=tin).exists():
            ClinicBranch.objects.filter(clinic_tin=tin, is_main=True).update(is_main=False)
            want_main = True
        ClinicBranch.objects.create(
            clinic_tin=tin,
            branch_tin=tin,
            name=branch_name,
            address=branch_address,
            is_main=want_main,
            is_active=True,
            created_at=timezone.now(),
        )
        from .branch_views import seed_branch_catalog

        seed_branch_catalog(clinic_tin=tin, branch_name=branch_name)
        return tenant


class TenantUpdateSerializer(serializers.Serializer):
    pharmacy_name = serializers.CharField(max_length=255, required=False)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    logo_url = serializers.URLField(required=False, allow_blank=True)
    setup_fee_etb = serializers.IntegerField(required=False, min_value=0)
    quarterly_fee_etb = serializers.IntegerField(required=False, min_value=0)
    yearly_fee_etb = serializers.IntegerField(required=False, min_value=0)
    fees_manually_set = serializers.BooleanField(required=False)
    modules = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False
    )
    billing_hold = serializers.BooleanField(required=False)
    billing_notes = serializers.CharField(required=False, allow_blank=True)
    is_illustration = serializers.BooleanField(required=False)
    free_trial_ends_at = serializers.DateTimeField(required=False, allow_null=True)
    ops_mode = serializers.ChoiceField(
        choices=["online", "offline"], required=False
    )

    def update(self, instance, validated_data):
        for field in (
            "pharmacy_name",
            "phone",
            "logo_url",
            "setup_fee_etb",
            "quarterly_fee_etb",
            "yearly_fee_etb",
            "fees_manually_set",
            "modules",
            "billing_hold",
            "billing_notes",
            "is_illustration",
            "free_trial_ends_at",
            "ops_mode",
        ):
            if field in validated_data:
                value = validated_data[field]
                if isinstance(value, str):
                    value = value.strip()
                setattr(instance, field, value)
        if any(
            f in validated_data
            for f in ("setup_fee_etb", "quarterly_fee_etb", "yearly_fee_etb")
        ):
            instance.fees_manually_set = True
        elif "modules" in validated_data and not instance.fees_manually_set:
            apply_catalog_fees_to_tenant(instance, modules=validated_data.get("modules"))
        instance.save()

        UserProfile.objects.filter(pharmacy_tin=instance.pharmacy_tin).update(
            pharmacy_name=instance.pharmacy_name,
            logoUrl=instance.logo_url,
        )
        return instance


def annotate_staff_counts(queryset):
    tenants = list(queryset)
    tins = [t.pharmacy_tin for t in tenants]
    profiles = UserProfile.objects.filter(pharmacy_tin__in=tins).select_related("user")
    by_tin: dict[str, dict[str, int]] = {}
    managers_by_tin: dict[str, str] = {}
    for profile in profiles:
        tin = profile.pharmacy_tin
        role = (profile.role or "").strip().lower()
        bucket = by_tin.setdefault(tin, {"manager": 0, "pharmacist": 0, "staff": 0})
        bucket["staff"] += 1
        if role == "manager":
            bucket["manager"] += 1
            if tin not in managers_by_tin:
                managers_by_tin[tin] = profile.user.username
        elif role == "pharmacist":
            bucket["pharmacist"] += 1

    for tenant in tenants:
        counts = by_tin.get(tenant.pharmacy_tin, {"manager": 0, "pharmacist": 0, "staff": 0})
        tenant.manager_count = counts["manager"]
        tenant.pharmacist_count = counts["pharmacist"]
        tenant.staff_count = counts["staff"]
        tenant.manager_username = managers_by_tin.get(tenant.pharmacy_tin, "")
    return tenants


def annotate_setup_review_status(tenants):
    if not tenants:
        return tenants

    tins = [t.pharmacy_tin for t in tenants]
    pending_by_tin: dict[str, TenantPaymentSubmission] = {}
    for submission in (
        TenantPaymentSubmission.objects.filter(
            pharmacy_tin__in=tins,
            payment_kind=TenantPaymentSubmission.KIND_SETUP,
            status=TenantPaymentSubmission.STATUS_PENDING,
        ).order_by("-submitted_at")
    ):
        pending_by_tin.setdefault(submission.pharmacy_tin, submission)

    latest_by_tin: dict[str, TenantPaymentSubmission] = {}
    for submission in (
        TenantPaymentSubmission.objects.filter(
            pharmacy_tin__in=tins,
            payment_kind=TenantPaymentSubmission.KIND_SETUP,
        ).order_by("-submitted_at")
    ):
        latest_by_tin.setdefault(submission.pharmacy_tin, submission)

    for tenant in tenants:
        if is_apex_provisioned_tenant(tenant):
            tenant.setup_review_status = "approved"
            continue
        if tenant.setup_fee_approved:
            tenant.setup_review_status = "approved"
        elif tenant.pharmacy_tin in pending_by_tin:
            tenant.setup_review_status = "pending"
        else:
            latest = latest_by_tin.get(tenant.pharmacy_tin)
            if latest and latest.status == TenantPaymentSubmission.STATUS_REJECTED:
                tenant.setup_review_status = "rejected"
            else:
                tenant.setup_review_status = "pending"
    return tenants


def annotate_tenant_rows(queryset):
    return annotate_setup_review_status(annotate_staff_counts(queryset))


def _branch_index():
    """Map org TIN → branch rows; also return satellite operational TINs to hide from org list."""
    from .branch_views import ClinicBranch

    by_org: dict[str, list[dict]] = {}
    satellite_tins: set[str] = set()
    for branch in ClinicBranch.objects.all().order_by("-is_main", "name"):
        org = (branch.clinic_tin or "").strip()
        if not org:
            continue
        op = branch.operational_tin()
        row = {
            "id": branch.id,
            "name": branch.name,
            "address": branch.address or "",
            "branch_tin": op,
            "is_main": bool(branch.is_main),
            "is_active": bool(branch.is_active),
        }
        by_org.setdefault(org.lower(), []).append(row)
        if op and op.lower() != org.lower():
            satellite_tins.add(op.lower())
    return by_org, satellite_tins


def serialize_tenants_with_branches(tenants):
    by_org, _ = _branch_index()
    payload = []
    for tenant in tenants:
        data = TenantSerializer(tenant).data
        tin_key = str(data.get("pharmacy_tin") or "").strip().lower()
        branches = by_org.get(tin_key, [])
        data["branches"] = branches
        data["branch_count"] = len(branches)
        data["branch_name"] = getattr(tenant, "branch_name", "") or ""
        payload.append(data)
    return payload


class TenantListCreateView(APIView):
    def get(self, request):
        qs = TenantAccount.objects.all()
        search = (request.query_params.get("search") or "").strip()
        status_filter = (request.query_params.get("status") or "").strip().lower()
        include_satellites = str(
            request.query_params.get("include_satellites") or ""
        ).strip().lower() in {"1", "true", "yes"}

        if search:
            qs = qs.filter(
                Q(pharmacy_tin__icontains=search) | Q(pharmacy_name__icontains=search)
            )
        if status_filter:
            qs = qs.filter(account_status=status_filter)

        qs = qs.order_by("-created_at")
        tenants = annotate_tenant_rows(qs)
        if not include_satellites:
            _, satellite_tins = _branch_index()
            if satellite_tins:
                tenants = [
                    t
                    for t in tenants
                    if str(t.pharmacy_tin or "").strip().lower() not in satellite_tins
                ]
        return Response(serialize_tenants_with_branches(tenants))

    def post(self, request):
        serializer = TenantCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        write_audit(
            action="create_tenant",
            pharmacy_tin=tenant.pharmacy_tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=tenant.pharmacy_name or tenant.pharmacy_tin,
        )
        tenants = annotate_tenant_rows(TenantAccount.objects.filter(pk=tenant.pk))
        return Response(TenantSerializer(tenants[0]).data, status=status.HTTP_201_CREATED)


class TenantDetailView(APIView):
    def get_object(self, tin: str):
        try:
            return TenantAccount.objects.get(pharmacy_tin=tin)
        except TenantAccount.DoesNotExist:
            # Satellite branch TINs may need a billing row on first open in Apex Billing.
            from .branch_views import ClinicBranch, provision_branch_billing_account

            branch = ClinicBranch.objects.filter(branch_tin__iexact=tin).first()
            if not branch:
                return None
            org = TenantAccount.objects.filter(pharmacy_tin=branch.clinic_tin).first()
            if not org:
                return None
            return provision_branch_billing_account(
                org_tenant=org,
                branch_tin=tin,
                branch_name=branch.name,
            )

    def get(self, request, tin):
        tenant = self.get_object(tin)
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)

        tenants = annotate_tenant_rows([tenant])
        # Satellite branch accounts should never require setup approval.
        from .branch_views import ClinicBranch, heal_satellite_billing_account

        branch = ClinicBranch.objects.filter(branch_tin__iexact=tin).first()
        if (
            branch
            and (branch.clinic_tin or "").strip().lower() != (tin or "").strip().lower()
        ):
            org = TenantAccount.objects.filter(pharmacy_tin=branch.clinic_tin).first()
            tenant = heal_satellite_billing_account(account=tenant, org_tenant=org)
            tenants = annotate_tenant_rows([tenant])

        staff = (
            UserProfile.objects.filter(pharmacy_tin=tin)
            .select_related("user")
            .order_by("role", "user__username")
        )
        staff_payload = [
            {
                "id": p.user_id,
                "username": p.user.username,
                "role": p.role,
                "pharmacy_name": p.pharmacy_name,
                "logoUrl": p.logoUrl,
                "pharmacy_tin": p.pharmacy_tin,
                "branch_name": getattr(p, "branch_name", "") or "",
                "login_disabled": not p.user.is_active,
            }
            for p in staff
        ]
        payments = TenantPaymentSubmission.objects.filter(pharmacy_tin=tin).order_by("-submitted_at")[:20]
        data = TenantSerializer(tenants[0]).data
        data["staff"] = staff_payload
        data["payments"] = PaymentSubmissionSerializer(payments, many=True).data
        return Response(data)

    def patch(self, request, tin):
        tenant = self.get_object(tin)
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TenantUpdateSerializer(tenant, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        write_audit(
            action="update_tenant_billing",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail="Updated pharmacy billing/profile settings",
            metadata={
                key: request.data.get(key)
                for key in (
                    "setup_fee_etb",
                    "quarterly_fee_etb",
                    "billing_hold",
                    "is_illustration",
                    "free_trial_ends_at",
                    "billing_notes",
                )
                if key in request.data
            },
        )
        tenants = annotate_tenant_rows([tenant])
        return Response(TenantSerializer(tenants[0]).data)


class TenantStatusView(APIView):
    ACTION_MAP = {
        "suspend": TenantAccount.STATUS_SUSPENDED,
        "unsuspend": TenantAccount.STATUS_ACTIVE,
        "ban": TenantAccount.STATUS_BANNED,
        "unban": TenantAccount.STATUS_ACTIVE,
        "delete": TenantAccount.STATUS_DELETED,
        "restore": TenantAccount.STATUS_ACTIVE,
    }

    def post(self, request, tin, action):
        try:
            tenant = TenantAccount.objects.get(pharmacy_tin=tin)
        except TenantAccount.DoesNotExist:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)

        new_status = self.ACTION_MAP.get(action)
        if not new_status:
            return Response({"detail": "Unknown action."}, status=status.HTTP_400_BAD_REQUEST)

        reason = (request.data.get("reason") or "").strip()
        tenant.account_status = new_status
        tenant.status_reason = reason
        tenant.status_changed_at = timezone.now()
        tenant.save(
            update_fields=["account_status", "status_reason", "status_changed_at", "updated_at"]
        )
        write_audit(
            action=f"{action}_tenant",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=reason or f"Marked {new_status}",
            metadata={"account_status": new_status},
        )
        tenants = annotate_tenant_rows([tenant])
        return Response(TenantSerializer(tenants[0]).data)


class PendingPaymentsView(APIView):
    def get(self, request):
        kind = (request.query_params.get("kind") or "").strip().lower()
        qs = TenantPaymentSubmission.objects.filter(
            status=TenantPaymentSubmission.STATUS_PENDING
        ).order_by("submitted_at")
        if kind in {
            TenantPaymentSubmission.KIND_SETUP,
            TenantPaymentSubmission.KIND_QUARTERLY,
            TenantPaymentSubmission.KIND_YEARLY,
        }:
            qs = qs.filter(payment_kind=kind)
        return Response(PaymentSubmissionSerializer(qs, many=True).data)


class ApproveSetupPaymentView(APIView):
    def post(self, request):
        tin = (request.data.get("pharmacy_tin") or "").strip()
        tenant = TenantAccount.objects.filter(pharmacy_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        tenant = approve_setup_payment(tenant=tenant, approved_by=request.user.user)
        write_audit(
            action="approve_setup_payment",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail="Approved setup payment",
        )
        return Response(TenantSerializer(annotate_tenant_rows([tenant])[0]).data)


class ApproveQuarterlyPaymentView(APIView):
    def post(self, request):
        tin = (request.data.get("pharmacy_tin") or "").strip()
        tenant = TenantAccount.objects.filter(pharmacy_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            tenant = approve_quarterly_payment(tenant=tenant, approved_by=request.user.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="approve_quarterly_payment",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail="Approved quarterly payment",
        )
        return Response(TenantSerializer(annotate_tenant_rows([tenant])[0]).data)


class ApproveYearlyPaymentView(APIView):
    def post(self, request):
        tin = (request.data.get("pharmacy_tin") or "").strip()
        tenant = TenantAccount.objects.filter(pharmacy_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            tenant = approve_yearly_payment(tenant=tenant, approved_by=request.user.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="approve_yearly_payment",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail="Approved yearly payment",
        )
        return Response(TenantSerializer(annotate_tenant_rows([tenant])[0]).data)


class RejectPaymentView(APIView):
    def post(self, request):
        submission_id = request.data.get("submission_id")
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "Rejection reason is required."}, status=status.HTTP_400_BAD_REQUEST)
        submission = TenantPaymentSubmission.objects.filter(pk=submission_id).first()
        if not submission:
            return Response({"detail": "Submission not found."}, status=status.HTTP_404_NOT_FOUND)
        if submission.status != TenantPaymentSubmission.STATUS_PENDING:
            return Response({"detail": "Only pending submissions can be rejected."}, status=status.HTTP_400_BAD_REQUEST)
        reject_payment_submission(
            submission=submission,
            reason=reason,
            rejected_by=request.user.user,
        )
        write_audit(
            action="reject_payment",
            pharmacy_tin=submission.pharmacy_tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=reason,
            metadata={
                "submission_id": submission.id,
                "payment_kind": submission.payment_kind,
            },
        )
        return Response(PaymentSubmissionSerializer(submission).data)


class ReleaseBillingHoldView(APIView):
    def post(self, request, tin):
        tenant = TenantAccount.objects.filter(pharmacy_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        tenant = release_billing_hold(tenant=tenant)
        write_audit(
            action="release_billing_hold",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail="Released billing hold",
        )
        return Response(TenantSerializer(annotate_tenant_rows([tenant])[0]).data)


class DashboardSummaryView(APIView):
    def get(self, request):
        from .models import (
            TenantFeedbackMessage,
            TenantOpsModeChangeRequest,
        )

        inactive = TenantAccount.objects.filter(
            account_status__in=[
                TenantAccount.STATUS_SUSPENDED,
                TenantAccount.STATUS_BANNED,
                TenantAccount.STATUS_DELETED,
            ]
        ).count()
        payload = {
            "tenants_total": TenantAccount.objects.count(),
            "tenants_active": TenantAccount.objects.filter(
                account_status=TenantAccount.STATUS_ACTIVE
            ).count(),
            "suspended_tenants": TenantAccount.objects.filter(
                account_status=TenantAccount.STATUS_SUSPENDED
            ).count(),
            "banned_tenants": TenantAccount.objects.filter(
                account_status=TenantAccount.STATUS_BANNED
            ).count(),
            "inactive_tenants": inactive,
            "pending_setup_payments": TenantPaymentSubmission.objects.filter(
                status=TenantPaymentSubmission.STATUS_PENDING,
                payment_kind=TenantPaymentSubmission.KIND_SETUP,
            ).count(),
            "pending_quarterly_payments": TenantPaymentSubmission.objects.filter(
                status=TenantPaymentSubmission.STATUS_PENDING,
                payment_kind=TenantPaymentSubmission.KIND_QUARTERLY,
            ).count(),
            "pending_yearly_payments": TenantPaymentSubmission.objects.filter(
                status=TenantPaymentSubmission.STATUS_PENDING,
                payment_kind=TenantPaymentSubmission.KIND_YEARLY,
            ).count(),
            "setup_pending_tenants": TenantAccount.objects.filter(
                setup_fee_approved=False
            ).count(),
            "unread_feedback": TenantFeedbackMessage.objects.filter(
                sender_side=TenantFeedbackMessage.SIDE_TENANT,
                read_by_apex=False,
            ).count(),
            "disabled_users": User.objects.filter(
                is_active=False, is_staff=False, is_superuser=False
            ).count(),
        }
        try:
            payload["pending_ops_mode_requests"] = TenantOpsModeChangeRequest.objects.filter(
                status=TenantOpsModeChangeRequest.STATUS_PENDING,
            ).count()
        except Exception:
            payload["pending_ops_mode_requests"] = 0
        return Response(payload)
