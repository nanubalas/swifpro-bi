"""Nettable on-hand availability for MRP.

Reuses the existing InventoryBalance / InventoryLotBalance tables - it does not
introduce a parallel stock model. "Nettable" availability is on-hand minus
reserved, restricted to stock-holding locations whose type is not a hold area
(quarantine / damaged / transit / returns), and minus expired lot quantity for
expiry-tracked products.
"""
from decimal import Decimal

# Location types whose stock is NOT available to net against demand.
NON_NETTABLE_LOCATION_TYPES = {"QUARANTINE", "DAMAGED", "TRANSIT", "RETURNS"}

ZERO = Decimal("0.00")


def nettable_on_hand(tenant, product, site, as_of=None):
    """Return ``(available, excluded)`` base-unit quantities for ``product`` at
    ``site``: ``available`` is nettable on-hand minus reserved minus expired;
    ``excluded`` is the on-hand sitting in non-nettable locations (for an
    informational exception). Never raises.
    """
    from core.models import InventoryBalance, InventoryLotBalance

    balances = (InventoryBalance.objects
                .filter(tenant=tenant, product=product, location__site=site,
                        location__is_active=True, location__holds_stock=True)
                .select_related("location"))

    available = ZERO
    excluded = ZERO
    nettable_location_ids = []
    for b in balances:
        if b.location.type in NON_NETTABLE_LOCATION_TYPES:
            excluded += (b.on_hand or ZERO)
            continue
        net = (b.on_hand or ZERO) - (b.reserved or ZERO)
        if net > ZERO:
            available += net
        nettable_location_ids.append(b.location_id)

    # Drop expired lot quantity held in nettable locations.
    expired = ZERO
    if getattr(product, "track_expiry", False) and nettable_location_ids and as_of is not None:
        for lb in InventoryLotBalance.objects.filter(
                tenant=tenant, product=product, location_id__in=nettable_location_ids,
                expiry_date__lt=as_of):
            expired += (lb.on_hand or ZERO) - (lb.reserved or ZERO)
        if expired < ZERO:
            expired = ZERO

    available = available - expired
    if available < ZERO:
        available = ZERO
    return available, excluded, expired
