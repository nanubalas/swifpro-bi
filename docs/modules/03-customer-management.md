# 3. Customer Management

### Purpose
Maintains the master record of every customer (sold-to party) for a tenant, including contact details, tax/company identifiers, billing and shipping addresses, payment terms, credit limit and account status. It is the central reference for all sales documents (quotes, orders, invoices, credit notes, receipts) and drives credit control and customer statements.

### Roles involved
- **Admin** — full read/write.
- **Sales** — full read/write (create, edit, statements).
- **Finance** — full read/write (used here as the mapped group for the Accountant membership role; ACCOUNTANT/FINANCE membership roles both resolve to the Finance group).
- **Read-only** — may view the customer list, customer profile, and statements (read), but cannot create or edit.

(All customer views use `@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN, ...], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])`; Read-only is granted view access only on `customer_list`, `customer_detail`, `customer_statement`, and `customer_statement_pdf`.)

### Workflow
1. User opens `/customers/` (`customer_list`) and can filter by free-text query (name, email, phone, VAT number, company number, contact person), customer type, status, or tag.
2. User clicks "New" → `/customers/new/` (`customer_create`) and fills the `CustomerForm`.
3. On save, `_find_customer_duplicates` checks for existing records matching email, phone, VAT number, company number, or name; if matches are found, the form re-renders with a duplicate warning and requires `confirm_duplicate=1` to proceed.
4. Record is saved against the current tenant; a DB `IntegrityError` on the `(tenant, name)` unique constraint surfaces as a "customer with this name already exists" error.
5. User is redirected to `/customers/<id>/` (`customer_detail`) — the profile showing details, outstanding balance, available credit, and a merged activity timeline (quotes, sales orders, invoices, payments, credit notes).
6. Edits go through `/customers/<id>/edit/` (`customer_edit`), which re-runs duplicate detection (excluding the current record).
7. Credit control is enforced downstream: when issuing AR invoices or confirming customer orders, `customer.credit_status(amount)` is called to block sales for ON_HOLD customers or those over their credit limit.
8. Statements: user opens `/customers/<id>/statement/` (`customer_statement`), defaults to the last 12 months (`statements.default_period`), and can download a PDF or email it to the customer.

### Input data
- Name (required; unique per tenant).
- Customer type (Individual, Company, Trade customer, Wholesale customer).
- Status (Active, Inactive, On hold).
- Contact person, email, phone.
- VAT number, company number.
- Billing address, shipping address.
- Payment terms (days) — blank falls back to the company default.
- Credit limit (0 = no limit).
- Tags (comma-separated, e.g. "VIP, Reseller").
- Notes.

### Output generated
- Persisted `Customer` record (scoped to tenant).
- Customer profile page with derived figures: `outstanding_balance`, `available_credit`, `is_over_limit`.
- Customer statement (HTML, PDF via `documents/statement_pdf.html`, or emailed PDF) — a dated ledger of invoices, receipts, refunds and credit notes with opening/closing running balance.
- Audit event `STATEMENT_SENT` when a statement is emailed.
- Credit-control verdicts `(ok, reason)` consumed by AR invoice and customer-order flows.
- No GL postings are produced directly by this module (postings arise from the invoices/payments it references).

### Related modules
- **Sales — Quotes, Customer (Sales) Orders, AR Invoices, Recurring Invoices** — all reference `Customer`; credit checks gate invoice issue and order confirmation.
- **Payments / Receipts & Refunds** — `Payment` records feed `outstanding_balance` and statements.
- **Credit Notes** — sales-kind credit notes reduce balance and appear on statements.
- **Reports — Aged Debtors (Receivables)** at `/reports/aged-receivables/` (`reports_service.aged_receivables`) ages each customer's open invoices.
- **CSV Import** — `/customers/import/` (`import_customers`) for bulk loading.

### Validations & rules
- **Tenant scoping** — every query filters by `tenant`; all detail/edit views use `get_object_or_404(Customer, id=..., tenant=tenant)`.
- **Unique name per tenant** — enforced by `unique_together = ("tenant", "name")`.
- **Duplicate detection** — soft warning on matching email/phone/VAT/company number/name; overridable with `confirm_duplicate=1` (advisory, not a hard block except the unique-name constraint).
- **Credit limit** — `credit_limit = 0` means no limit; `available_credit = credit_limit - outstanding_balance` (None when no limit). `credit_status(additional)` blocks a sale when projected outstanding would exceed the limit.
- **On-hold block** — status `ON_HOLD` causes `credit_status` to return `False` regardless of credit limit, blocking new sales until released.
- **Payment terms fallback** — null `payment_terms_days` defers to the company/tenant default.
- **Outstanding balance** — computed only over `CustomerInvoice.ISSUED_STATES` invoices with positive outstanding amount.
- **Soft-delete** — not implemented for customers; there is no customer delete URL/view. Customers are retired by setting status to Inactive.

### Database entities
- `Customer` (with `Type`, `Status` text choices; properties `tag_list`, `outstanding_balance`, `available_credit`, `is_over_limit`; method `credit_status`).
- `CustomerInvoice` / `CustomerInvoiceLine` (drive balance and statement debits).
- `Payment` (RECEIPT and REFUND directions — statement credits/debits).
- `CreditNote` / `CreditNoteLine` (kind SALES — statement credits).
- `CustomerOrder`, `SalesQuote` (timeline and related sales documents).
- `Tenant` (scoping owner).

### API / page requirements
- `GET /customers/` — `customer_list` (filters: `q`, `type`, `status`, `tag`).
- `GET|POST /customers/new/` — `customer_create`.
- `GET /customers/<id>/` — `customer_detail` (profile + timeline).
- `GET|POST /customers/<id>/edit/` — `customer_edit`.
- `GET /customers/<id>/statement/` — `customer_statement`.
- `GET /customers/<id>/statement/pdf/` — `customer_statement_pdf`.
- `POST /customers/<id>/statement/email/` — `customer_statement_email`.
- `GET|POST /customers/import/` — `import_customers` (CSV).
- `GET /reports/aged-receivables/` — `report_aged_receivables` (related reporting).

### Flow diagram
```mermaid
flowchart TD
    A[/customers/ list] -->|filter q/type/status/tag| A
    A --> B[New customer /customers/new/]
    A --> C[Open profile /customers/&lt;id&gt;/]
    B --> D[CustomerForm valid?]
    D -->|no| B
    D -->|yes| E[_find_customer_duplicates]
    E -->|matches & not confirmed| F[Show duplicate warning]
    F -->|confirm_duplicate=1| G[save tenant-scoped]
    E -->|no matches| G
    G -->|IntegrityError on unique name| B
    G --> C
    C --> H[Edit /customers/&lt;id&gt;/edit/]
    H --> E
    C --> I[Compute outstanding_balance / available_credit]
    C --> J[Statement /customers/&lt;id&gt;/statement/]
    J --> K[PDF / Email statement]
    I --> L{credit_status used by Sales}
    L -->|ON_HOLD or over limit| M[Block AR invoice / order]
    L -->|ok| N[Allow sale]
```

Key files: `d:\swifpro_bi\core\models.py` (Customer at line 1444), `d:\swifpro_bi\core\views.py` (customer views at lines 3726-5068), `d:\swifpro_bi\core\forms.py` (CustomerForm at line 517), `d:\swifpro_bi\core\services\statements.py`, `d:\swifpro_bi\core\urls.py` (lines 208-215), `d:\swifpro_bi\core\roles.py`.

---

[← Back to module index](README.md)
