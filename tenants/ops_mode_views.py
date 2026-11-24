"""Apex review queue for clinic ops_mode change requests."""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SyncNode, SyncState, TenantAccount, TenantOpsModeChangeRequest
from .pricing import write_audit


def awaiting_step(req: TenantOpsModeChangeRequest) -> str | None:
    """What an approved change is waiting for: the initial "seed" (going offline) or a final "sync" (going online)."""
    if req.status != TenantOpsModeChangeRequest.STATUS_APPROVED:
        return None
    return "seed" if (req.requested_ops_mode or "").strip().lower() == TenantAccount.OPS_MODE_OFFLINE else "sync"


def _server_summary(tins) -> dict:
    """Per clinic: how many active clinic-server tokens exist, and whether the server has seeded / synced."""
    summary = {tin: {"active_nodes": 0, "seeded_at": None, "last_successful_sync_at": None} for tin in tins}
    for row in SyncNode.objects.filter(clinic_tin__in=tins, is_active=True).values("clinic_tin"):
        summary[row["clinic_tin"]]["active_nodes"] += 1
    for state in SyncState.objects.filter(clinic_tin__in=tins):
        info = summary[state.clinic_tin]
        if state.seeded_at and (info["seeded_at"] is None or state.seeded_at > info["seeded_at"]):
            info["seeded_at"] = state.seeded_at
        last = state.last_successful_sync_at
        if last and (info["last_successful_sync_at"] is None or last > info["last_successful_sync_at"]):
            info["last_successful_sync_at"] = last
    return summary


def _serialize(req: TenantOpsModeChangeRequest, *, clinic_name: str = "", server: dict | None = None) -> dict:
    return {
        "id": req.id,
        "pharmacy_tin": req.clinic_tin,
        "pharmacy_name": clinic_name,
        "current_ops_mode": req.current_ops_mode,
        "requested_ops_mode": req.requested_ops_mode,
        "request_note": req.request_note,
        "status": req.status,
        "requested_by_username": req.requested_by_username,
        "review_note": req.review_note,
        "reviewed_at": req.reviewed_at,
        "applied_at": req.applied_at,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        # No change applies on approval alone any more: going offline waits for the seed, going online for a sync.
        "applies_immediately": False,
        "awaiting_step": awaiting_step(req),
        "server": server,
    }


def _clinic_name(tin: str) -> str:
    tenant = TenantAccount.objects.filter(pharmacy_tin=tin).first()
    return tenant.pharmacy_name if tenant else ""


class OpsModeRequestsListView(APIView):
    def get(self, request):
        status_filter = (request.query_params.get("status") or "pending").strip().lower()
        qs = TenantOpsModeChangeRequest.objects.all().order_by("-created_at")
        if status_filter and status_filter != "all":
            qs = qs.filter(status=status_filter)
        requests_ = list(qs[:200])
        servers = _server_summary({r.clinic_tin for r in requests_})
        return Response(
            [
                _serialize(r, clinic_name=_clinic_name(r.clinic_tin), server=servers.get(r.clinic_tin))
                for r in requests_
            ]
        )


class ApproveOpsModeRequestView(APIView):
    def post(self, request):
        request_id = request.data.get("request_id")
        if not request_id:
            return Response({"detail": "request_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        req = TenantOpsModeChangeRequest.objects.filter(pk=request_id).first()
        if not req:
            return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        if req.status != TenantOpsModeChangeRequest.STATUS_PENDING:
            return Response({"detail": "Only pending requests can be approved."}, status=status.HTTP_400_BAD_REQUEST)

        tenant = TenantAccount.objects.filter(pharmacy_tin=req.clinic_tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        if tenant.account_status != TenantAccount.STATUS_ACTIVE:
            return Response(
                {"detail": "Tenant must be active to approve ops mode changes."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        req.status = TenantOpsModeChangeRequest.STATUS_APPROVED
        req.reviewed_at = now
        req.reviewed_by = request.user.user
        req.review_note = (request.data.get("review_note") or "").strip()
        req.save(update_fields=["status", "reviewed_at", "reviewed_by", "review_note", "updated_at"])
        step = awaiting_step(req)
        if step == "seed":
            detail = (
                "Approved. The clinic stays online until its server is set up and the initial seed completes. "
                "Issue the clinic server's token from the Clinic server panel."
            )
        else:
            detail = "Approved. The clinic keeps offline mode until the manager completes a successful sync."

        write_audit(
            action="approve_ops_mode_request",
            pharmacy_tin=req.clinic_tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=f"{req.current_ops_mode} → {req.requested_ops_mode} (effective after the clinic's {step})",
            metadata={"request_id": req.id, "awaiting_step": step},
        )

        return Response(
            {
                "detail": detail,
                "request": _serialize(
                    req, clinic_name=tenant.pharmacy_name, server=_server_summary({req.clinic_tin}).get(req.clinic_tin)
                ),
            }
        )


class RejectOpsModeRequestView(APIView):
    def post(self, request):
        request_id = request.data.get("request_id")
        reason = (request.data.get("reason") or request.data.get("review_note") or "").strip()
        if not request_id:
            return Response({"detail": "request_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not reason:
            return Response({"detail": "reason is required."}, status=status.HTTP_400_BAD_REQUEST)

        req = TenantOpsModeChangeRequest.objects.filter(pk=request_id).first()
        if not req:
            return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        if req.status != TenantOpsModeChangeRequest.STATUS_PENDING:
            return Response({"detail": "Only pending requests can be rejected."}, status=status.HTTP_400_BAD_REQUEST)

        req.status = TenantOpsModeChangeRequest.STATUS_REJECTED
        req.reviewed_at = timezone.now()
        req.reviewed_by = request.user.user
        req.review_note = reason
        req.save(
            update_fields=["status", "reviewed_at", "reviewed_by", "review_note", "updated_at"]
        )

        write_audit(
            action="reject_ops_mode_request",
            pharmacy_tin=req.clinic_tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=reason[:500],
            metadata={"request_id": req.id},
        )

        return Response(
            {
                "detail": "Request rejected.",
                "request": _serialize(req, clinic_name=_clinic_name(req.clinic_tin)),
            }
        )
