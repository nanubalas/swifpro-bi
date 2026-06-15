"""MRP (Material Requirements Planning) service layer.

Business logic for MRP lives here, never in views.

Phase 1: numbering helpers.
Phase 2: the BUY-item engine and its collaborators (demand/supply collectors,
inventory snapshot, lot sizing, lead time, pegging, exceptions).
Later phases add BOM/MAKE explosion, transfer planning and conversion.
"""
from core.services.mrp.numbering import (
    next_run_number,
    next_planned_order_number,
)
from core.services.mrp.engine import run_mrp

__all__ = ["next_run_number", "next_planned_order_number", "run_mrp"]
