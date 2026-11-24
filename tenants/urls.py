from django.urls import path

from .ops_mode_views import (
    ApproveOpsModeRequestView,
    OpsModeRequestsListView,
    RejectOpsModeRequestView,
)
from .ops_views import (
    AnalyticsSummaryView,
    AuditLogView,
    BroadcastChatView,
    CloseThreadView,
    FeedbackDirectoryView,
    FeedbackThreadView,
    PricingPreviewView,
    PricingRuleDetailView,
    PricingRulesView,
    SendFeedbackMessageView,
    SetUserLoginDisabledView,
    SignupsPipelineView,
    StartChatView,
    TenantUsersView,
)
from .sales_agents import ApexSalesAgentDetailView, ApexSalesAgentListView
from .sync_nodes import IssueSyncNodeView, RevokeSyncNodeView, TenantSyncView
from .branch_views import TenantBranchDetailView, TenantBranchesView
from .views import (
    ApproveQuarterlyPaymentView,
    ApproveSetupPaymentView,
    ApproveYearlyPaymentView,
    DashboardSummaryView,
    PendingPaymentsView,
    RejectPaymentView,
    ReleaseBillingHoldView,
    TenantDetailView,
    TenantListCreateView,
    TenantStatusView,
)

urlpatterns = [
    path("sales-agents/", ApexSalesAgentListView.as_view(), name="sales-agents"),
    path("sales-agents/<int:pk>/", ApexSalesAgentDetailView.as_view(), name="sales-agent-detail"),
    path("summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("analytics/", AnalyticsSummaryView.as_view(), name="analytics-summary"),
    path("signups/", SignupsPipelineView.as_view(), name="signups-pipeline"),
    path("users/", TenantUsersView.as_view(), name="tenant-users"),
    path("users/set-login-disabled/", SetUserLoginDisabledView.as_view(), name="set-login-disabled"),
    path("audit/", AuditLogView.as_view(), name="audit-log"),
    path("pricing/", PricingRulesView.as_view(), name="pricing-rules"),
    path("pricing/preview/", PricingPreviewView.as_view(), name="pricing-preview"),
    path("pricing/<int:pk>/", PricingRuleDetailView.as_view(), name="pricing-rule-detail"),
    path("feedback/", FeedbackDirectoryView.as_view(), name="feedback-directory"),
    path("feedback/start/", StartChatView.as_view(), name="feedback-start"),
    path("feedback/broadcast/", BroadcastChatView.as_view(), name="feedback-broadcast"),
    path("feedback/<str:tin>/", FeedbackThreadView.as_view(), name="feedback-thread"),
    path("feedback/<str:tin>/send/", SendFeedbackMessageView.as_view(), name="feedback-send"),
    path("feedback/<str:tin>/close/", CloseThreadView.as_view(), name="feedback-close"),
    path("payments/pending/", PendingPaymentsView.as_view(), name="payments-pending"),
    path("payments/approve-setup/", ApproveSetupPaymentView.as_view(), name="payments-approve-setup"),
    path(
        "payments/approve-quarterly/",
        ApproveQuarterlyPaymentView.as_view(),
        name="payments-approve-quarterly",
    ),
    path(
        "payments/approve-yearly/",
        ApproveYearlyPaymentView.as_view(),
        name="payments-approve-yearly",
    ),
    path("payments/reject/", RejectPaymentView.as_view(), name="payments-reject"),
    path("ops-mode-requests/", OpsModeRequestsListView.as_view(), name="ops-mode-requests"),
    path(
        "ops-mode-requests/approve/",
        ApproveOpsModeRequestView.as_view(),
        name="ops-mode-requests-approve",
    ),
    path(
        "ops-mode-requests/reject/",
        RejectOpsModeRequestView.as_view(),
        name="ops-mode-requests-reject",
    ),
    path("sync-nodes/issue/", IssueSyncNodeView.as_view(), name="sync-node-issue"),
    path("sync-nodes/revoke/", RevokeSyncNodeView.as_view(), name="sync-node-revoke"),
    path("", TenantListCreateView.as_view(), name="tenant-list-create"),
    path("<str:tin>/sync/", TenantSyncView.as_view(), name="tenant-sync"),
    path("<str:tin>/branches/", TenantBranchesView.as_view(), name="tenant-branches"),
    path(
        "<str:tin>/branches/<int:pk>/",
        TenantBranchDetailView.as_view(),
        name="tenant-branch-detail",
    ),
    path("<str:tin>/", TenantDetailView.as_view(), name="tenant-detail"),
    path("<str:tin>/release-hold/", ReleaseBillingHoldView.as_view(), name="tenant-release-hold"),
    path("<str:tin>/<str:action>/", TenantStatusView.as_view(), name="tenant-status"),
]
