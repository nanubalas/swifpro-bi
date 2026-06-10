# SwifPro BI v0.1.0-rc1

This is the first release candidate for SwifPro BI ERP.

## Readiness

Status: Release Candidate
Test suite: 629 passing
Migrations: clean through 0094
Release blockers: closed
Recommended use: controlled manual QA / pilot testing

## Included in this release

* Inventory ledger and stock balances
* FIFO, moving-average, and standard costing with PPV
* General ledger with idempotent posting and balanced-journal guard
* Procure-to-pay: requisition → purchase order → GRN → supplier invoice
* GRNI, PPV, landed-cost accrual clearing
* Order-to-cash: quote → customer order → invoice → stock issue → COGS
* Credit notes, payments, allocations, and reversals
* Returns/RMA disposition and hold resolution
* Two-step transfers with in-transit stock/accounting
* Stock adjustments, cycle counts, and full stock-take
* Replenishment planning
* Lot, serial, expiry, and near-expiry tracking
* BOM kits/bundles with sell-time component explosion
* BOM line sequencing using 10/20/30 style line numbers
* Multi-site and location support
* UOM conversions
* VAT/tax computation
* Inter-company trading with P&L elimination
* Audit log, notifications, role access, site/location access, and permission overrides
* Database audit documentation under `docs/audits/database/`

## Release blockers fixed before RC

* AccessRequest tenant scoping
* Cross-tenant access request protection
* JournalEntry DB-level idempotency constraint
* Balanced-journal posting guard
* Legacy serial readiness audit
* Inventory worklist for stuck stock
* Feature visibility labels for partial/advisory modules

## Clearly labelled limitations

The following features are intentionally visible but labelled honestly:

* Channel Orders: experimental manual posting tool; not connected to Shopify/Amazon sync yet
* Bins: advisory only; not fully bin-aware for receiving, picking, transfer, ATP
* VAT submit: local-only; does not file to HMRC MTD
* BOM: kits/bundles only; no work orders or build-to-stock manufacturing
* Inter-company consolidation: P&L elimination supported; balance-sheet elimination not yet complete

## Known non-blockers

* Broader list-view pagination
* inventory_analytics N+1 hotspot
* Full bin-aware WMS flow
* Real Shopify/Amazon sync
* HMRC MTD filing
* Production orders/work orders
* Inter-company balance-sheet elimination
* Record-level search

## Production notes

Before using real production data:

1. Run the journal duplicate pre-check.
2. Apply migrations through 0094.
3. Run `audit_serial_readiness` per tenant.
4. Start from clean books or post opening-balance journals.
5. Verify inventory ↔ GL reconciliation shows zero variance.
6. Verify trial balance is balanced.
7. Complete clean-tenant manual QA.

## Rollback notes

Migrations 0081–0094 are additive. A code rollback is safe without un-migrating in normal circumstances. GL is append-only and idempotent; duplicate journal posting is now blocked at database level.
