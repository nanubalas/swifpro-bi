# 16. Import / Export

### Purpose
Provides bulk CSV import for master data (products, customers, suppliers) using upsert-by-key with per-row validation, plus CSV/Excel export of both master data and finance reports/ledgers. Lets a UK SME bootstrap its catalogue and contacts from spreadsheets and extract accounting data for accountants, HMRC filing prep, or backup. Every export is gated by the `export_data` permission and audited.

### Roles involved
- **Admin** — full import (products, customers, suppliers) and all exports.
- **Purchasing** (Procurement group) — import products and suppliers.
- **Sales** — import customers.
- **Finance / Accountant** — import customers; finance/master-data exports (via `EXPORT_DATA` permission).
- Any authenticated user — download blank import templates (`import_template` is `@login_required` only).
- Export endpoints are permission-gated by `EXPORT_DATA`, not role-gated, so any role granted that permission (including overrides) can export.

### Workflow
1. User downloads a template at `/import/<kind>/template.csv` (header row + one sample row from `CONFIG[kind]["sample"]`).
2. User fills the CSV and uploads it at `/products/import/`, `/customers/import/`, or `/suppliers/import/`.
3. `_run_import` validates the upload is a non-empty `.csv`, then `importer.read_rows` decodes it (UTF-8 BOM-tolerant) into dict rows.
4. The configured import function (`import_products` / `import_customers` / `import_suppliers`) iterates rows starting at line 2, validates each, and upserts via `update_or_create` keyed on the tenant + key column.
5. Bad rows are skipped and collected with their line number; valid rows still save (per-row resilience).
6. A summary (`created`, `updated`, `errors`, `total`) is flashed to the user and rendered on `imports/import.html`.
7. For export, user hits `/export/<kind>.csv` (master data) or `/finance/export/<kind>.csv` (finance), optionally with `?format=xlsx`.
8. `_export_response` picks CSV (default) or `.xlsx` (openpyxl) and streams the file; a `DATA_EXPORTED` audit entry is written.

### Input data
- Uploaded `.csv` file (`request.FILES["file"]`), UTF-8 with optional BOM.
- Products columns: `sku, name, product_type, category, brand, description, uom, cost_method, standard_cost, sales_price, is_active, barcode` (key = `sku`).
- Customers columns: `name, customer_type, contact_person, email, phone, vat_number, company_number, billing_address, shipping_address, payment_terms_days, tags` (key = `name`).
- Suppliers columns: `name, contact_person, email, phone, vat_number, company_number, address, currency_code, payment_terms_days, categories` (key = `name`).
- Export query params: `?format=xlsx|excel`, and for finance: `from`, `to`, `as_of` dates.

### Output generated
- Created/updated `Product` (+ auto-created `ProductCategory`, optional `ProductBarcode`), `Customer`, `Supplier` records.
- Import summary dict: created/updated/errors(line, message)/total.
- Master-data export files: `products.csv`, `customers.csv`, `suppliers.csv` (or `.xlsx`) — column order mirrors import so an export re-imports cleanly.
- Finance export files (CSV/XLSX): trial balance, P&L, balance sheet, cash flow, aged receivables/payables, general ledger (journal), expenses, payments, customer invoices, supplier bills, credit notes, bank transactions, VAT return (9 boxes), VAT transactions, sales-history/by-product/by-customer/by-channel.
- Audit log export: `audit-log.csv` (admin only, capped at 5000 rows).
- `DATA_EXPORTED` audit log entries with `kind (N rows)` detail.

### Related modules
- **Inventory / Products** — product + category + barcode upsert; export feeds stock valuation.
- **Customers / Sales** — customer import; customer-invoice and sales-report exports.
- **Suppliers / Procurement** — supplier import; supplier-bill exports.
- **Finance / GL** — journal, trial balance, P&L, balance sheet, payments, expenses, credit notes exports.
- **VAT (MTD)** — VAT return and VAT-transactions exports.
- **Audit Log** — audit export and `DATA_EXPORTED` logging.
- Bank statement import (`/bank/transactions/import/`) exists as a separate Finance feature, not part of `importer.py`.

### Validations & rules
- Tenant scoping: every import upsert and export query filters by the active tenant (`update_or_create(tenant=..., key=...)`, `.filter(tenant=tenant)`).
- Upload must end in `.csv` and be present, else rejected with a message.
- Per-row resilience: invalid rows are skipped with `(line_no, reason)`; one bad row never blocks the file.
- Products: `sku` and `name` required; invalid `standard_cost` skips the row; invalid `sales_price` falls back to 0; unknown `cost_method` → `AVERAGE`, unknown `product_type` → `STOCK`; `is_active` false-y strings (`0/false/no/inactive`) set inactive; `category` supports `Parent / Child` and is auto-created; barcode added only if not already present.
- Customers/Suppliers: `name` required; unknown `customer_type` → `COMPANY`; non-numeric `payment_terms_days` ignored; supplier `currency_code` defaults `GBP`.
- Export gating: `data_export` and `finance_export` require the `EXPORT_DATA` permission; `audit_log_export` is Admin-only via `role_required`.
- Finance period exports default to the current financial year when no `from`/`to` given.
- Excel sheet title truncated to 31 chars; header row bold + frozen.
- No delete-on-import (upsert only); imports are not transactional across the file (each row commits independently).

### Database entities
- `Product`, `ProductCategory`, `ProductBarcode`
- `Customer`, `Supplier`
- Finance read models: `JournalLine` / `JournalEntry`, `Expense`, `Payment`, `CustomerInvoice`, `SupplierInvoice`, `CreditNote`, `BankTransaction`
- `AuditLog` (for audit export and `DATA_EXPORTED` entries)

### API / page requirements
- `GET/POST /products/import/` → `import_products`
- `GET/POST /customers/import/` → `import_customers`
- `GET/POST /suppliers/import/` → `import_suppliers`
- `GET /import/<kind>/template.csv` → `import_template`
- `GET /export/<kind>.csv` → `data_export` (`kind` ∈ products/customers/suppliers; `?format=xlsx`)
- `GET /finance/export/<kind>.csv` → `finance_export` (`?format=xlsx`, `from`, `to`, `as_of`)
- `GET /audit/export.csv` → `audit_log_export`
- Helpers: `_run_import`, `importer.read_rows`, `importer.export_rows`, `importer.CONFIG`, `_export_response`, `_csv_response`, `_xlsx_response`, `_finance_export_data`.

### Flow diagram
```mermaid
flowchart TD
    A[User] --> B{Import or Export?}
    B -->|Import| C[Download template\n/import/&lt;kind&gt;/template.csv]
    C --> D[Upload CSV\n/products|customers|suppliers/import/]
    D --> E[_run_import: validate .csv]
    E -->|invalid file| F[Flash error]
    E -->|ok| G[read_rows: decode UTF-8 BOM]
    G --> H[CONFIG fn: import_products/customers/suppliers]
    H --> I[Per-row validate + update_or_create\ntenant-scoped]
    I --> J[Skip bad rows with line no]
    I --> K[Auto-create category / barcode]
    J --> L[Summary: created/updated/errors]
    K --> L
    B -->|Export| M{EXPORT_DATA permission}
    M -->|denied| N[403]
    M -->|granted| O[data_export / finance_export]
    O --> P[export_rows / _finance_export_data]
    P --> Q[_export_response\nCSV or xlsx via openpyxl]
    Q --> R[log_audit DATA_EXPORTED]
    R --> S[File download]
```

---

[← Back to module index](README.md)
