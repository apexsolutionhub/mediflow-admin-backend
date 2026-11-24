"""Apex side of offline sync: ops-mode approval waits for the seed or sync, and clinic server tokens."""
import hashlib

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apex.authentication import ApexPrincipal
from tenants.models import (
    ApexAuditLog,
    SyncConflict,
    SyncNode,
    SyncRun,
    SyncState,
    TenantAccount,
    TenantOpsModeChangeRequest,
)

TIN = "ADM-TIN-1"
# Shared tables the admin does not manage; a fresh test database does not have them.
SHARED = [TenantAccount, TenantOpsModeChangeRequest, ApexAuditLog, SyncNode, SyncState, SyncRun, SyncConflict]


class AdminCase(TestCase):
    created_tables = []

    @classmethod
    def setUpClass(cls):
        existing = set(connection.introspection.table_names())
        cls.created_tables = []
        with connection.schema_editor() as editor:
            for model in SHARED:
                if model._meta.db_table not in existing:
                    editor.create_model(model)
                    cls.created_tables.append(model)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        with connection.schema_editor() as editor:
            for model in reversed(cls.created_tables):
                editor.delete_model(model)

    def setUp(self):
        self.user = get_user_model().objects.create(username="apex-tester")
        self.client = APIClient()
        self.client.force_authenticate(ApexPrincipal(user=self.user))
        self.tenant = TenantAccount.objects.create(pharmacy_tin=TIN, pharmacy_name="Test Clinic", branch_name="Main")

    def request(self, current, requested, status="pending"):
        return TenantOpsModeChangeRequest.objects.create(
            clinic_tin=TIN, current_ops_mode=current, requested_ops_mode=requested, status=status,
            requested_by_username="mgr",
        )


class ApprovalTests(AdminCase):
    def approve(self, req, **extra):
        return self.client.post("/api/tenants/ops-mode-requests/approve/", {"request_id": req.id, **extra}, format="json")

    def test_going_offline_waits_for_the_seed_and_changes_nothing_yet(self):
        req = self.request("online", "offline")
        res = self.approve(req)
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db()
        self.tenant.refresh_from_db()
        self.assertEqual((req.status, req.applied_at), ("approved", None))
        self.assertEqual(self.tenant.ops_mode, "online")  # the old behaviour flipped it here
        body = res.json()["request"]
        self.assertEqual((body["awaiting_step"], body["applies_immediately"]), ("seed", False))
        self.assertIn("seed", res.json()["detail"])

    def test_going_online_waits_for_a_sync(self):
        TenantAccount.objects.filter(pk=self.tenant.pk).update(ops_mode="offline")
        req = self.request("offline", "online")
        res = self.approve(req)
        self.assertEqual(res.json()["request"]["awaiting_step"], "sync")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ops_mode, "offline")

    def test_approval_is_audited_with_the_step(self):
        self.approve(self.request("online", "offline"))
        entry = ApexAuditLog.objects.get(action="approve_ops_mode_request")
        self.assertEqual(entry.metadata["awaiting_step"], "seed")
        self.assertEqual(entry.pharmacy_tin, TIN)

    def test_only_pending_requests_can_be_approved(self):
        res = self.approve(self.request("online", "offline", status="approved"))
        self.assertEqual(res.status_code, 400)

    def test_an_inactive_clinic_cannot_be_approved(self):
        TenantAccount.objects.filter(pk=self.tenant.pk).update(account_status="suspended")
        self.assertEqual(self.approve(self.request("online", "offline")).status_code, 400)

    def test_reject_needs_a_reason(self):
        req = self.request("online", "offline")
        bad = self.client.post("/api/tenants/ops-mode-requests/reject/", {"request_id": req.id}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(
            "/api/tenants/ops-mode-requests/reject/", {"request_id": req.id, "reason": "Not ready"}, format="json"
        )
        self.assertEqual(ok.status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.status, "rejected")

    def test_the_list_shows_each_clinic_server_state(self):
        req = self.request("online", "offline", status="approved")
        SyncNode.objects.create(clinic_tin=TIN, branch_name="Main", token_hash="a" * 64, token_prefix="mfn_abcd")
        SyncNode.objects.create(clinic_tin=TIN, branch_name="Main", token_hash="b" * 64, is_active=False)
        seeded = timezone.now()
        SyncState.objects.create(clinic_tin=TIN, branch_name="Main", seeded_at=seeded)
        rows = self.client.get("/api/tenants/ops-mode-requests/", {"status": "approved"}).json()
        self.assertEqual([r["id"] for r in rows], [req.id])
        self.assertEqual(rows[0]["awaiting_step"], "seed")
        self.assertEqual(rows[0]["server"]["active_nodes"], 1)  # the revoked token does not count
        self.assertIsNotNone(rows[0]["server"]["seeded_at"])

    def test_nothing_without_an_apex_login(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get("/api/tenants/ops-mode-requests/").status_code, (401, 403))
        self.assertIn(anonymous.post("/api/tenants/sync-nodes/issue/", {"tin": TIN}, format="json").status_code, (401, 403))


class NodeTokenTests(AdminCase):
    def issue(self, **body):
        return self.client.post("/api/tenants/sync-nodes/issue/", {"tin": TIN, **body}, format="json")

    def test_a_token_is_shown_once_and_only_its_hash_is_kept(self):
        res = self.issue(branch_name="Main", label="Reception PC")
        self.assertEqual(res.status_code, 201, res.content)
        token = res.json()["token"]
        self.assertTrue(token.startswith("mfn_"))
        node = SyncNode.objects.get()
        # The clinic side checks sha256(token); the two must agree.
        self.assertEqual(node.token_hash, hashlib.sha256(token.encode("utf-8")).hexdigest())
        self.assertEqual((node.token_prefix, node.branch_name, node.label), (token[:8], "Main", "Reception PC"))
        self.assertNotIn(token, str(ApexAuditLog.objects.get(action="issue_sync_node").metadata))
        self.assertNotIn("token", self.client.get(f"/api/tenants/{TIN}/sync/").json()["nodes"][0])

    def test_the_branch_defaults_to_the_clinics_branch(self):
        self.assertEqual(self.issue().json()["node"]["branch_name"], "Main")

    def test_one_active_token_per_branch(self):
        self.assertEqual(self.issue(branch_name="Main").status_code, 201)
        again = self.issue(branch_name="main")
        self.assertEqual((again.status_code, again.json()["code"]), (409, "node_exists"))
        self.assertEqual(SyncNode.objects.filter(is_active=True).count(), 1)

    def test_replacing_revokes_the_old_token(self):
        first = self.issue(branch_name="Main").json()["node"]["id"]
        second = self.issue(branch_name="Main", replace=True)
        self.assertEqual(second.status_code, 201)
        self.assertFalse(SyncNode.objects.get(pk=first).is_active)
        self.assertEqual(SyncNode.objects.filter(is_active=True).count(), 1)

    def test_another_branch_can_have_its_own_token(self):
        self.issue(branch_name="Main")
        self.assertEqual(self.issue(branch_name="Bole").status_code, 201)

    def test_unknown_or_inactive_clinics_get_no_token(self):
        self.assertEqual(self.client.post("/api/tenants/sync-nodes/issue/", {"tin": "nope"}, format="json").status_code, 404)
        TenantAccount.objects.filter(pk=self.tenant.pk).update(account_status="banned")
        self.assertEqual(self.issue().status_code, 400)

    def test_revoking_stops_a_token(self):
        node_id = self.issue().json()["node"]["id"]
        res = self.client.post("/api/tenants/sync-nodes/revoke/", {"node_id": node_id}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(SyncNode.objects.get(pk=node_id).is_active)
        again = self.client.post("/api/tenants/sync-nodes/revoke/", {"node_id": node_id}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(
            self.client.post("/api/tenants/sync-nodes/revoke/", {"node_id": 999999}, format="json").status_code, 404
        )

    def test_sync_status_for_a_clinic(self):
        SyncNode.objects.create(clinic_tin=TIN, branch_name="Main", token_hash="c" * 64, token_prefix="mfn_wxyz")
        now = timezone.now()
        SyncState.objects.create(
            clinic_tin=TIN, branch_name="Main", seeded_at=now, last_successful_sync_at=now, restore_pending=True
        )
        SyncRun.objects.create(
            sync_uuid="11111111-1111-1111-1111-111111111111", clinic_tin=TIN, branch_name="Main", kind="sync",
            push_status="ok", pull_status="failed", error_message="The cloud is unreachable.", started_at=now,
        )
        SyncConflict.objects.create(clinic_tin=TIN, branch_name="Main", entity="patients")
        SyncConflict.objects.create(clinic_tin=TIN, branch_name="Main", entity="patients", resolved_at=now)
        body = self.client.get(f"/api/tenants/{TIN}/sync/").json()
        self.assertEqual(body["open_conflicts"], 1)
        self.assertTrue(body["states"][0]["restore_pending"])
        self.assertEqual(body["runs"][0]["pull_status"], "failed")
        self.assertEqual(body["nodes"][0]["token_prefix"], "mfn_wxyz")
        self.assertEqual(self.client.get("/api/tenants/not-a-tin/sync/").status_code, 404)
