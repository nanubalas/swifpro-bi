# Feature Coverage Inferred from Schema (core app)

_Schema facts are CONFIRMED via Django ORM introspection. Coverage judgments are
INFERRED from schema + a code-wiring spot-check (views/services references) + demo
row counts. A table existing does NOT prove the feature is complete - see caveats._

Totals: **84 models / tables**, 1 app (`core`), **0 ManyToMany**, **2 OneToOne**,
**268 ForeignKeys** (120 nullable, ~45%), **22 composite indexes**, **49
unique_together**, **1 conditional unique constraint**, **0 CheckConstraints**.

Relationship hubs (inbound FK count): Tenant 63, User 37, Product 29, Location 24,
Site 12, UnitOfMeasure 11, Supplier 11, TaxCode 10, PurchaseOrder 8, Customer 6.

---

## F. Features that appear COMPLETE (schema + code + test-backed)

| Area | Tables | Evidence |
|---|---|---|
| Inventory ledger / subledger | InventoryMovement, InventoryBalance, InventoryLotBalance, InventoryCostLayer, InventoryIssueCost, InventoryReservation | Single chokepoint `apply_movement`; rich indexes; serial guard; 600+ tests |
| Costing (FIFO / average / standard + PPV) | InventoryCostLayer, Product.cost_method | FIFO layer consumption, moving avg, standard+PPV all posted to GL |
| General ledger | JournalEntry, JournalLine, GLAccount | Idempotent posters, balanced-journal guard, conditional unique constraint |
| Procure-to-pay | PurchaseRequisition(+Line), PurchaseOrder(+Line, Amendment), GoodsReceipt(+Line), LandedCostCharge, SupplierInvoice(+Line), SupplierPriceHistory | Full req→PO→GRN→invoice w/ GRNI+PPV+landed-cost accrual clearing |
| Order-to-cash (manual) | CustomerOrder(+Line), SalesQuote(+Line), CustomerInvoice(+Line), CreditNote(+Line) | Quote→order→invoice→COGS; credit notes |
| Payments & settlement | Payment(+Allocation) | AR receipts / AP payments / refunds, allocations, reversal |
| Returns / RMA | ReturnAuthorization(+Line) | Disposition (restock/quarantine/scrap/repair/RTS), hold resolution, GL |
| Transfers (two-step) | InventoryTransfer(+Line) | Dispatch→partial receive→close-short, in-transit GL |
| Stock counts | StockAdjustment, CycleCount(+Line), StockTakeSession(+Line) | Approval workflow, snapshot/staleness, GL valuation |
| Replenishment | ReplenishmentPolicy | Min/max/ROP/EOQ/MOQ/pack, projected availability |
| Master data | Product(+Barcode, Category), Supplier, Customer, Site, Location, GLAccount, TaxCode, UnitOfMeasure, UOMConversion, Department | Mature; consistent tenant + number uniqueness |
| Access control | OrgMembership, UserProfile, UserSiteAccess, UserLocationAccess, UserPermissionOverride | Role + site/location scoping + per-permission overrides |
| Audit & comms | AuditLog, EmailLog, Notification, NotificationPreference | Append-only audit, email log, in-app notifications |
| Tax/VAT chart | TaxCode, VatReturn (partial - see G) | Input/output VAT posted |
| Recurring billing | RecurringInvoice(+Line) | Frequency-based generation |

## G. Features that appear PARTIAL / WEAK / SCAFFOLD (verify before relying)

| Area | Tables | Why flagged (INFERRED) |
|---|---|---|
| Marketplace channel sync | ChannelConnection, ChannelOrder, ChannelSnapshot, SyncRun, SalesOrder(+Line) | `ChannelOrder`/`SyncRun` have **no view references** (service/background only); demo has 1 row each. `SalesOrder` (channel order) overlaps conceptually with `CustomerOrder` (manual order) - **two "sales order" tables**. Shopify/external sync was explicitly out of release scope. Treat as integration scaffold, not user-complete. |
| Multi-company / inter-company | CompanyGroup, InterCompanyTransaction | **0 rows**; `CompanyGroup` has no service references; `InterCompanyTransaction` referenced across services but unexercised. Multi-company appears **scaffolded**. |
| Bin-level stock | Bin, Container, InventoryBinBalance | **0 rows** in all three; prior audit found most outbound paths are **not bin-aware**, so bin balances can drift from location balances (advisory only). |
| VAT return workflow | VatReturn | Status enum only `DRAFT` / `SUBMITTED` - no `FILED`/`PAID`/`ACKNOWLEDGED`. Submission-to-HMRC lifecycle is **incomplete**. |
| Manufacturing / kitting | BillOfMaterials(+Line) | Present and lightly used (1 demo BOM); assembly issue exists, but no production-order / work-order model - **basic kitting only**. |

## H. Missing / suspicious schema gaps

1. **No CheckConstraints anywhere** - invariants (qty ≥ 0, serial on_hand ∈ {0,1}, debit/credit ≥ 0, debits == credits) are enforced in application code only. The JournalEntry balance/idempotency are the only DB-level guards (constraint added for idempotency; balance is app-side).
2. **120 nullable FKs (~45%)** - many optional links (site, location, lot, receiving_location, etc.). Some permit incomplete records, e.g. PO with null `receiving_location` (known; surfaced via the replenishment worklist). Nullable `site`/`location` on financial entries means some postings aren't site-attributable.
3. **22 line/child tables have no `tenant` FK** - tenant isolation for `*Line` / `PaymentAllocation` relies on joining the parent. Direct ORM queries on a line table are not tenant-safe by themselves (mitigated because views go through parents).
4. **Two overlapping order models** - `SalesOrder` (channel) vs `CustomerOrder` (manual). Risk of divergent logic / confusion; one may be legacy.
5. **`InventoryBalance` has no composite `Meta.indexes`** beyond its `unique_together(tenant, product, location)` - adequate (leading tenant), but location-scoped aggregates could want `(tenant, location)`.
6. **`CycleCountValuationCorrection`** is an 18-field correction/audit record with a OneToOne to `InventoryMovement` - confirm it's still needed post-fix (looks like a one-off remediation table).
7. **Idempotency coverage** - only `JournalEntry` has a DB uniqueness constraint on a document ref; document numbers rely on `unique_together(tenant, number)` (49 of these, good). Non-journal posting idempotency is app-level.

## Inference legend
- **CONFIRMED**: tables, columns, types, null/default, FK/O2O, on_delete, indexes, constraints, choices (from ORM).
- **INFERRED**: business purpose, data-class (master/transaction/ledger/config/reporting/audit), and the complete/partial judgments above (from names + code spot-check + demo data).
