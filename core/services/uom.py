"""Unit-of-measure conversion.

Stock is always stored, costed and valued in a product's BASE unit of measure.
Transaction lines (purchase orders, sales orders/invoices) may be entered in a
different transaction UOM (e.g. buy by the case, stock by the each). These
helpers convert a transaction quantity/unit-cost to the base unit at the moment
it hits the inventory ledger - money (line totals, GL) stays in document units,
only quantities convert.

Resolution order for a conversion: a product-specific rule, then a tenant-global
rule; a reverse rule is inverted if only it exists. With no base_uom or no
transaction UOM the conversion is the identity (1:1), so existing data and
documents that don't use UOMs are unaffected.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

QTY_DP = Decimal("0.01")
COST_DP = Decimal("0.0001")
ONE = Decimal("1")


def _conversion_multiplier(product, from_uom, to_uom):
    """Base-units-per-from_uom via a product-specific then global rule (reverse
    rule inverted). Returns None if no rule exists."""
    from core.models import UOMConversion
    tenant_id = product.tenant_id
    for prod_id in (product.id, None):
        c = UOMConversion.objects.filter(tenant_id=tenant_id, product_id=prod_id,
                                         from_uom=from_uom, to_uom=to_uom).first()
        if c is not None:
            return c.multiplier
        rev = UOMConversion.objects.filter(tenant_id=tenant_id, product_id=prod_id,
                                           from_uom=to_uom, to_uom=from_uom).first()
        if rev is not None and rev.multiplier:
            return (ONE / rev.multiplier)
    return None


def units_per_base(product, uom):
    """How many BASE units equal 1 `uom`. Identity (1) when there's no base UOM,
    no transaction UOM, or they're the same. Raises ValidationError when a
    distinct UOM has no conversion rule (surfaces misconfiguration loudly)."""
    base = getattr(product, "base_uom", None)
    if uom is None or base is None or uom.id == base.id:
        return ONE
    m = _conversion_multiplier(product, uom, base)
    if m is None:
        raise ValidationError(
            f"No UOM conversion from {uom.code} to {base.code} for {product.sku}.")
    return m


def to_base_qty(product, qty, uom):
    """Convert a transaction quantity in `uom` to the product's base unit."""
    if qty is None:
        return Decimal("0.00")
    return (Decimal(qty) * units_per_base(product, uom)).quantize(QTY_DP, rounding=ROUND_HALF_UP)


def base_unit_cost(product, unit_cost, uom):
    """Convert a unit cost expressed per `uom` to a per-base-unit cost, so the
    line's money value is preserved (qty_uom*cost_uom == base_qty*base_cost)."""
    upb = units_per_base(product, uom)
    if not upb:
        return Decimal(unit_cost or "0")
    return (Decimal(unit_cost or "0") / upb).quantize(COST_DP, rounding=ROUND_HALF_UP)
