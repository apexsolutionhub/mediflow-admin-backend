"""Apex: clinic server credentials and sync status (docs/OFFLINE_SYNC_CONTRACT.md, sections 8 and 15).

A clinic server syncs with the cloud using a token issued here, one per branch. The token is shown
once; only its hash is stored (the clinic side checks the same SHA-256).
"""
from __future__ import annotations

import hashlib
import secrets

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SyncConflict, SyncNode, SyncRun, SyncState, TenantAccount
from .pricing import write_audit

TOKEN_PREFIX = "mfn_"


def _node(n: SyncNode) -> dict:
    return {
        "id": n.id,
        "branch_name": n.branch_name,
        "label": n.label,
        "token_prefix": n.token_prefix,
        "is_active": n.is_active,
        "last_seen_at": n.last_seen_at,
        "created_at": n.created_at,
    }


def _tenant(tin: str):
    return TenantAccount.objects.filter(pharmacy_tin=tin).first()


class TenantSyncView(APIView):
    """Everything Apex needs to see about one clinic server: tokens, whether it has seeded, recent syncs."""

    def get(self, request, tin):
        tenant = _tenant(tin)
        if tenant is None:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        nodes = SyncNode.objects.filter(clinic_tin=tin)
        states = SyncState.objects.filter(clinic_tin=tin)
        runs = SyncRun.objects.filter(clinic_tin=tin)[:10]
        return Response(
            {
                "tin": tin,
                "clinic_name": tenant.pharmacy_name,
                "ops_mode": tenant.ops_mode,
                "default_branch": tenant.branch_name or "Main",
                "nodes": [_node(n) for n in nodes],
                "states": [
                    {
                        "branch_name": s.branch_name,
                        "seeded_at": s.seeded_at,
                        "last_successful_sync_at": s.last_successful_sync_at,
                        "restore_pending": s.restore_pending,
                    }
                    for s in states
                ],
                "open_conflicts": SyncConflict.objects.filter(clinic_tin=tin, resolved_at__isnull=True).count(),
                "runs": [
                    {
                        "id": str(r.sync_uuid),
                        "branch_name": r.branch_name,
                        "kind": r.kind,
                        "push_status": r.push_status,
                        "pull_status": r.pull_status,
                        "error_message": r.error_message,
                        "started_at": r.started_at,
                        "finished_at": r.finished_at,
                    }
                    for r in runs
                ],
            }
        )


class IssueSyncNodeView(APIView):
    def post(self, request):
        tin = (request.data.get("tin") or "").strip()
        tenant = _tenant(tin)
        if tenant is None:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)
        if tenant.account_status != TenantAccount.STATUS_ACTIVE:
            return Response(
                {"detail": "Tenant must be active to issue a clinic server token."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        branch = (request.data.get("branch_name") or tenant.branch_name or "Main").strip()[:120]
        label = (request.data.get("label") or "").strip()[:120]
        replace = bool(request.data.get("replace"))

        active = SyncNode.objects.filter(clinic_tin=tin, branch_name__iexact=branch, is_active=True)
        if active.exists() and not replace:
            return Response(
                {
                    "detail": "This branch already has an active clinic server token. "
                    "Revoke it first, or issue a replacement (the old server will stop syncing).",
                    "code": "node_exists",
                },
                status=status.HTTP_409_CONFLICT,
            )
        replaced = active.update(is_active=False) if replace else 0

        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        node = SyncNode.objects.create(
            clinic_tin=tin,
            branch_name=branch,
            label=label,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            token_prefix=token[:8],
        )
        write_audit(
            action="issue_sync_node",
            pharmacy_tin=tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=f"Clinic server token issued for branch {branch}" + (" (replacing the previous one)" if replaced else ""),
            metadata={"node_id": node.id, "branch": branch, "replaced": replaced},  # never the token itself
        )
        return Response(
            {"detail": "Token issued. Copy it now; it cannot be shown again.", "token": token, "node": _node(node)},
            status=status.HTTP_201_CREATED,
        )


class RevokeSyncNodeView(APIView):
    def post(self, request):
        node = SyncNode.objects.filter(pk=request.data.get("node_id")).first()
        if node is None:
            return Response({"detail": "Clinic server token not found."}, status=status.HTTP_404_NOT_FOUND)
        if not node.is_active:
            return Response({"detail": "This token is already revoked."}, status=status.HTTP_400_BAD_REQUEST)
        node.is_active = False
        node.save(update_fields=["is_active"])
        write_audit(
            action="revoke_sync_node",
            pharmacy_tin=node.clinic_tin,
            actor_username=getattr(request.user.user, "username", ""),
            detail=f"Clinic server token revoked for branch {node.branch_name}",
            metadata={"node_id": node.id, "branch": node.branch_name},
        )
        return Response({"detail": "Token revoked. That clinic server can no longer sync.", "node": _node(node)})
