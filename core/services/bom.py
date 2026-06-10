from decimal import Decimal
from core.models import BillOfMaterials, BillOfMaterialsLine, Product

def explode_product(product: Product, qty: Decimal):
    """Return list of (component_product, component_qty) for a sell qty of product.
    If product has an active BOM, returns its components; otherwise returns itself.
    Uses the first active BOM by created_at.
    NOTE: This implements the recommended kit policy: deduct components only."""
    if qty is None:
        qty = Decimal("0")
    qty = Decimal(qty)
    bom = BillOfMaterials.objects.filter(product=product, is_active=True).order_by("created_at").first()
    if not bom:
        return [(product, qty)]
    out = []
    # Order by the stable BOM line number (then id) for deterministic display;
    # ordering does not affect the component quantities returned.
    for line in BillOfMaterialsLine.objects.select_related("component").filter(bom=bom).order_by("line_no", "id"):
        out.append((line.component, qty * Decimal(line.qty)))
    return out
