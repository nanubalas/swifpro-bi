"""Lot sizing for MRP planned-order quantities.

Phase 2 supports LOT_FOR_LOT and FIXED_QTY. MIN_MAX and PERIOD_ORDER_QTY are
not supported yet (the model lacks the level fields they need); they fall back
to lot-for-lot and report an UNSUPPORTED_LOT_SIZING_METHOD note rather than
failing. MOQ, order multiple and max-order-qty are then applied in that order.
"""
from decimal import Decimal, ROUND_CEILING

ZERO = Decimal("0.00")


def _round_up_to_multiple(qty, multiple):
    if not multiple or multiple <= ZERO:
        return qty
    lots = (qty / multiple).to_integral_value(rounding=ROUND_CEILING)
    return (multiple * lots)


def size_order(profile, net_requirement):
    """Return ``(qty, capped, notes)`` for a single replenishment of
    ``net_requirement`` against ``profile``'s parameters.

    ``notes`` is a list of ``(code, message)`` tuples the caller turns into
    MRPExceptions. ``capped`` is True when max_order_qty limited the quantity
    below what was needed.
    """
    notes = []
    method = profile.lot_sizing_method
    qty = Decimal(net_requirement)

    if method == "LOT_FOR_LOT":
        qty = Decimal(net_requirement)
    elif method == "FIXED_QTY":
        foq = profile.fixed_order_qty or ZERO
        if foq > ZERO:
            lots = (Decimal(net_requirement) / foq).to_integral_value(rounding=ROUND_CEILING)
            qty = foq * lots
        else:
            notes.append(("UNSUPPORTED_LOT_SIZING_METHOD",
                          "Fixed-quantity lot sizing has no fixed order qty set; used lot-for-lot."))
            qty = Decimal(net_requirement)
    else:
        notes.append(("UNSUPPORTED_LOT_SIZING_METHOD",
                      f"Lot sizing method {method} is not supported yet; used lot-for-lot."))
        qty = Decimal(net_requirement)

    # MOQ
    moq = profile.min_order_qty or ZERO
    if moq > ZERO and qty < moq:
        qty = moq

    # Order multiple (round up)
    qty = _round_up_to_multiple(qty, profile.order_multiple or ZERO)

    # Max order quantity (cap down)
    capped = False
    max_qty = profile.max_order_qty or ZERO
    if max_qty > ZERO and qty > max_qty:
        qty = max_qty
        capped = True

    return qty.quantize(Decimal("0.01")), capped, notes
