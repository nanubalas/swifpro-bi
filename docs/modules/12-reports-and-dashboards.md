# 12. Reports and Dashboards

### Purpose
Read-only reporting layer that turns the General Ledger, AR/AP sub-ledgers and inventory balances into the financial, sales and operational reports a UK SME needs to run day to day. It also drives the role-based landing dashboards that surface headline KPIs and a per-role launcher of accessible modules. All figures are computed live from posted data (no separate reporting tables) and are tenant-scoped, with stock/inventory reports further scoped to the locations a user may access.

### Roles involved
- Admin: every report and dashboard.
- Accountant / Finance: financial statements (Trial Balance, P&L, Balance Sheet, Cash Flow), Aged Debtors/Creditors, Consolidated, Supplier Scorecard, all sales reports, stock/inventory reports.
- Manager: Stock Valuation, Inventory Analytics, Profitability, Supplier Scorecard, sales reports, operations dashboard.
- Sales: sales reports (history, by product/customer/channel, profitability) and sales dashboard.
- Warehouse: Stock Valuation, Inventory Analytics, warehouse dashboard.
- Purchasing: Stock Valuation, Inventory Analytics, Supplier Scorecard, purchasing dashboard.
- Read-only: all financial reports, stock reports, profitability and consolidated (read-only dashboard).

### Workflow
1. User logs in and hits `/dashboard/`, which routes to their role dashboard (`landing`/`_make_dashboard`); the dashboard renders role KPIs via `_dashboard_kpis` plus a launcher built from `sidebar_for_role`.
2. User opens a report index: `/reports/` (financial), `/sales/reports/` (sales), or a direct report link.
3. User optionally sets a period (`?from=`/`?to=`) or point-in-time (`?as_of=`); financial period reports default to the tenant's current financial year (`current_financial_year`, default April start).
4. The view calls the matching service function in `services/reports.py`, `services/sales_reports.py` or `services/purchasing.py`.
5. Stock/inventory reports resolve the user's accessible locations via `accessible_location_ids` and pass `location_ids` to scope valuation and lot detail.
6. The report renders to its template; a header export link points at `finance_export` with the report's `export_kind` and current query string.
7. User clicks export and downloads CSV (default) or XLSX via `?format=xlsx` (`_export_response`).
8. Group/consolidated reporting (`/reports/consolidated/`) aggregates across the companies in the user's group (`group_companies`) and applies inter-company eliminations.

### Input data
- Posted `JournalEntry` / `JournalLine` rows (the GL is the source for all financial statements).
- `CustomerInvoice` (statuses Issued/Sent/Paid) and `SupplierInvoice` (Posted) with their lines, payment allocations and credit notes.
- `InventoryBalance`, `InventoryLotBalance`, `InventoryCostLayer` (FIFO layers) and `Product` cost fields (average/standard cost, cost method).
- `InventoryMovement` SALE rows (ref_type `AR_INVOICE`) for COGS in profitability.
- `SalesOrder` (Posted channel/ecommerce orders) for sales-by-channel.
- `GoodsReceipt` and `PurchaseOrderLine` for supplier scorecard OTD and price variance.
- `InterCompanyTransaction` for consolidated eliminations.
- Query params: `from`, `to`, `as_of`, `format`.

### Output generated
- Financial statements: Trial Balance, Profit & Loss, Balance Sheet (with retained earnings rolled into equity), Cash Flow Summary.
- Aged Debtors and Aged Creditors bucketed current / 1-30 / 31-60 / 61-90 / 90+.
- Stock Valuation (qty x cost) and Inventory Analytics (value per location, lot/serial/expiry detail, period COGS, turnover, days-of-inventory).
- Sales reports: history, by product, by customer, by channel, profitability (revenue - COGS with margin %).
- Supplier Scorecard: spend, on-time-delivery %, purchase price variance.
- Consolidated group P&L / balance sheet / stock totals with inter-company eliminations.
- Dashboard KPI cards per role.
- CSV/XLSX downloads. No PDF export and no scheduled/emailed reports are implemented.

### Related modules
- General Ledger (all financial statements read `JournalLine`).
- Accounts Receivable / Accounts Payable (aged reports, KPIs, cash flow).
- Inventory (valuation, analytics, lot/cost layers, location access).
- Sales / Orders (sales analytics, channel orders, profitability COGS movements).
- Purchasing (supplier scorecard from bills, GRNs, PO lines).
- Multi-company / Inter-company (consolidated reporting and eliminations).
- VAT (export endpoint shares `finance_export` for vat-return/transactions).

### Validations & rules
- All queries filter by `tenant`; stock/inventory reports additionally filter by `location_ids` from `accessible_location_ids`.
- Reports are read-only; views are `@role_required` with read groups wider than write groups (e.g. financial reports allow Finance/Admin/Read-only).
- Sign conventions enforced in `account_balances`: ASSET/EXPENSE/COGS are debit-normal, LIABILITY/EQUITY/INCOME credit-normal.
- Trial Balance and Balance Sheet expose a `balanced` flag (debits == credits; assets == liabilities + equity-with-earnings).
- Aging only counts open invoices with positive `outstanding`; AR uses statuses ISSUED/SENT, AP uses POSTED.
- FIFO-costed products are valued from remaining `InventoryCostLayer` rows; others at moving-average/standard cost.
- P&L/Cash-Flow default to the tenant's financial year (`financial_year_start_month`, default 4).
- Consolidated only includes companies in the user's group and eliminates `InterCompanyTransaction` amounts between in-scope companies.
- A failing KPI card is swallowed so it never breaks the dashboard; users may only view their own role's dashboard (Admin may view any).
- Bulk data export (`data_export`) is separately gated by the `EXPORT_DATA` permission.

### Database entities
- `JournalEntry`, `JournalLine`, `GLAccount`
- `CustomerInvoice`, `SupplierInvoice`, `CustomerOrder`, `SalesQuote`, `SalesOrder`
- `InventoryBalance`, `InventoryLotBalance`, `InventoryCostLayer`, `InventoryMovement`, `Product`, `StockAdjustment`
- `PurchaseOrder`, `PurchaseOrderLine`, `PurchaseRequisition`, `GoodsReceipt`
- `InterCompanyTransaction`, `AuditLog`
- No dedicated report/snapshot model exists; reports are computed on demand.

### API / page requirements
- Dashboards: `/dashboard/` plus `/dashboard/{admin,accountant,manager,sales,warehouse,purchasing,finance,read-only}`.
- Financial reports: `/reports/`, `/reports/trial-balance/`, `/reports/profit-and-loss/`, `/reports/balance-sheet/`, `/reports/cash-flow/`, `/reports/aged-receivables/`, `/reports/aged-payables/`, `/reports/stock-valuation/`, `/reports/inventory-analytics/`, `/reports/consolidated/`.
- Sales reports: `/sales/reports/`, `/sales/reports/history/`, `/sales/reports/by-product/`, `/sales/reports/by-customer/`, `/sales/reports/by-channel/`, `/sales/reports/profitability/`.
- Procurement report: `/reports/supplier-scorecard/`.
- Exports: `/finance/export/<kind>.csv` (`finance_export`, supports `?format=xlsx`), `/export/<kind>.csv` (`data_export`), `/audit/export.csv`.
- These are server-rendered Django pages, not a JSON API.

### Flow diagram
```mermaid
flowchart TD
    A[User login] --> B[/dashboard/ landing]
    B --> C[Role dashboard via _make_dashboard]
    C --> D[_dashboard_kpis KPI cards]
    C --> E[sidebar_for_role launcher]
    E --> F{Report category}
    F -->|Financial| G[/reports/* views/]
    F -->|Sales| H[/sales/reports/* views/]
    F -->|Procurement| I[/reports/supplier-scorecard/]
    F -->|Group| J[/reports/consolidated/]
    G --> K[services/reports.py]
    H --> L[services/sales_reports.py]
    I --> M[purchasing.supplier_scorecard]
    J --> N[group_companies + reports.consolidated]
    K --> O[GL JournalLine + AR/AP + Inventory]
    O --> P{Location-scoped?}
    P -->|stock/analytics| Q[accessible_location_ids]
    K --> R[Render template]
    L --> R
    M --> R
    N --> R
    R --> S[finance_export CSV / XLSX]
```

---

[← Back to module index](README.md)
