"""Resolve effective clinic fees from catalog unless Apex locked pricing."""

from __future__ import annotations

from .pricing import resolve_pricing

CLINIC_BUSINESS_TYPE = "Clinic"
DEFAULT_SETUP_FEE_ETB = 15000
DEFAULT_QUARTERLY_FEE_ETB = 5000


def clinic_catalog_fees(modules=None) -> dict:
    fees = resolve_pricing(CLINIC_BUSINESS_TYPE, modules or [])
    return {
        "setup_fee_etb": int(fees.get("setup_fee_etb") or DEFAULT_SETUP_FEE_ETB),
        "quarterly_fee_etb": int(fees.get("quarterly_fee_etb") or DEFAULT_QUARTERLY_FEE_ETB),
        "yearly_fee_etb": int(fees.get("yearly_fee_etb") or 0),
        "source": fees.get("source") or "fallback",
    }


def effective_tenant_fees(tenant) -> tuple[int, int]:
    if getattr(tenant, "fees_manually_set", False):
        return int(tenant.setup_fee_etb or 0), int(tenant.quarterly_fee_etb or 0)
    fees = clinic_catalog_fees(getattr(tenant, "modules", None))
    return fees["setup_fee_etb"], fees["quarterly_fee_etb"]


def apply_catalog_fees_to_tenant(tenant, *, modules=None) -> None:
    """Refresh stored fees from catalog when Apex has not manually overridden."""
    if getattr(tenant, "fees_manually_set", False):
        return
    fees = clinic_catalog_fees(modules if modules is not None else getattr(tenant, "modules", None))
    tenant.setup_fee_etb = fees["setup_fee_etb"]
    tenant.quarterly_fee_etb = fees["quarterly_fee_etb"]
    tenant.yearly_fee_etb = fees["yearly_fee_etb"]
