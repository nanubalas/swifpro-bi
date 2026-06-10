# SwifPro BI

A Django, server-rendered **mini-ERP for UK SMEs** - purchasing, inventory,
sales, and finance in one system of record. Server-rendered with Bootstrap 5
(no build step), SQLite for dev / PostgreSQL for production.

## Hosted Demo

- **Demo app:** https://swifpro-bi-demo.onrender.com
- **Release:** v0.1.0-rc3
- **Quick-start guide:** [docs/user_handbook/Quick_Start_Demo_Guide.md](docs/user_handbook/Quick_Start_Demo_Guide.md)
- **Full user handbook:** [docs/user_handbook/SwifPro_BI_First_Time_User_Handbook.md](docs/user_handbook/SwifPro_BI_First_Time_User_Handbook.md)

Notes for testers:
- **Passwords are shared separately** (not stored in the repo or docs).
- The Render **free instance may sleep** and take **30–60 seconds** to wake on
  the first request — this is normal.
- This is a **controlled demo / release candidate, not production ERP** — don't
  enter real data.

## Features

**Procurement & inbound**
- Purchase Orders with lifecycle (Draft → Submitted → Approval → Sent → In
  Transit → Received), versioned amendments, and threshold-based approvals
- Shipments with containers, tracking and event history
- Goods Receipt (GRN) with partial receipts and over-receipt protection
- Supplier invoices with 3-way match (PO ↔ receipt ↔ invoice)

**Inventory**
- Immutable movement ledger; multi-location balances
- Lot / serial / expiry tracking and reservations
- Transfers between locations; cycle counts with variance posting
- BOM / kits (components deducted on sale)
- **Costing**: moving **AVERAGE**, **FIFO** (cost layers), and **STANDARD**
  (with purchase price variance); **landed-cost** apportionment at receipt

**Sales & channels**
- Sales orders (kit explosion, reservations, negative-stock warnings)
- Returns (RMA) with restock
- Shopify sync *pattern* (mock fetch) + channel reconciliation

**Finance**
- Customer invoices (AR) and supplier invoices (AP), VAT tax codes
- Double-entry General Ledger; **COGS** posted on sale; inventory capitalized
  on receipt
- **Payments** (AR receipts / AP payments) with FIFO allocation + **bank
  reconciliation**
- **UK VAT return (MTD 9-box)** - compute, review, save (see HMRC note below)
- **Reports**: Trial Balance, Profit & Loss, Balance Sheet, Aged Debtors /
  Creditors, Stock Valuation

**Platform**
- Per-user **multi-tenancy** (`UserProfile` → tenant; forms scoped per tenant)
- **Role-based access** (Admin, Procurement, Warehouse, Sales, Finance,
  Read-only)
- Light / dark theme, modern UI

## Quick start

### 1) Create a virtualenv and install deps
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2) Migrate the database
```bash
python manage.py migrate
```

### 3) Create an admin user
```bash
python manage.py createsuperuser
```

### 4) (Recommended) Seed a fully-populated demo tenant
```bash
python manage.py seed_demo
```
This creates the **SwifPro BI Demo Ltd** tenant with products, suppliers,
locations, a PO + shipment, a posted sales order (COGS), an issued AR invoice,
a customer receipt, a Shopify sync, and a draft VAT return - so every page
shows real data. It binds the `admin` user to the tenant and is idempotent
(safe to re-run). The seed also creates the role groups.

### 5) Run the server
```bash
python manage.py runserver
```
Open **http://127.0.0.1:8000/** and sign in. A superuser sees everything; for
non-superusers, assign a `UserProfile` (tenant) and one or more role groups in
`/admin/`.

## Roles

| Role | Typical access |
|------|----------------|
| Admin | Everything |
| Procurement | POs, suppliers, products, shipments |
| Warehouse | Receiving, inventory, transfers, cycle counts |
| Sales | Sales orders, returns, customers |
| Finance | Invoices, payments, GL, VAT, reports |
| Read-only | View-only across modules |

Group membership is managed in `/admin/` (Users). Each user's tenant is set via
their `UserProfile`.

## Costing methods

Set per product (`cost_method`):
- **AVERAGE** - moving weighted-average, recomputed on each costed receipt.
- **FIFO** - receipts create cost layers; sales consume oldest-first.
- **STANDARD** - inventory carried at `standard_cost`; the difference vs actual
  purchase (+landed) cost posts to **Purchase Price Variance**.

Receiving capitalizes stock (**DR Inventory / CR GRNI**, plus **CR Accruals**
for landed costs); sales expense **COGS** (**DR COGS / CR Inventory**).

## VAT / Making Tax Digital

The VAT page (`/vat/`) computes and saves the 9-box return from issued sales and
posted purchase invoices. **Live submission to HMRC's MTD API is not connected**
- it requires an HMRC developer account and credentials. `submit_vat_return`
records a clearly-labelled local stub; replace its body with a real HMRC client
when credentials are available.

## Production configuration

Settings read from environment variables (safe dev defaults otherwise):

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | Required when `DEBUG` is off |
| `DJANGO_DEBUG` | `1` (default) / `0` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated origins |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_HSTS_SECONDS` | HTTPS hardening |
| `EMAIL_*` | SMTP for sending supplier POs |

When `DEBUG=0`, HSTS, secure cookies, and (if installed) WhiteNoise static
serving are enabled. Run `python manage.py collectstatic` for production static
files. Switch the database to PostgreSQL via the standard Django `DATABASES`
settings (a `psycopg2-binary` dependency is included).

## Tests

```bash
python manage.py test core
```

Covers the receiving flow, GL balancing (AR/AP), payments, VAT maths, the
reports, tenant-scoped forms, and all three costing methods.

## Project layout

```
core/
  models.py          # domain models
  views.py           # server-rendered views
  forms.py           # tenant-scoped model forms
  services/          # business logic: inventory, gl, vat, reports, bom, sync
  templates/         # Bootstrap 5 templates
  management/commands/seed_demo.py
swifpro_bi/settings.py   # configuration
```
