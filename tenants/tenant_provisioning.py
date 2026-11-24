"""Helpers for distinguishing Apex-provisioned clinics from self-signup tenants."""


def is_self_signup_tenant(tenant) -> bool:
    if tenant.is_illustration:
        return False
    if getattr(tenant, "provisioned_by_apex", False):
        return False
    if tenant.free_trial_ends_at and not tenant.setup_fee_approved:
        return True
    return not tenant.setup_fee_approved


def is_apex_provisioned_tenant(tenant) -> bool:
    if tenant.is_illustration:
        return True
    return bool(getattr(tenant, "provisioned_by_apex", False))
