"""MRP (Material Requirements Planning) service layer.

Business logic for MRP lives here, never in views. Phase 1 ships only the
numbering helpers; the demand/supply collectors, lot sizing, BOM explosion,
pegging, exception generation, the engine, and conversion arrive in later
phases (see numbering for the document-number conventions they will use).
"""
from core.services.mrp.numbering import (
    next_run_number,
    next_planned_order_number,
)

__all__ = ["next_run_number", "next_planned_order_number"]
