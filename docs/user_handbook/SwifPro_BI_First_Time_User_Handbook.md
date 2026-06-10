# SwifPro BI — First-Time User Handbook (v0.1.0-rc3)

A friendly guide for business users trying the **SwifPro BI** demo for the first
time. No technical knowledge required.

---

## 1. Welcome / Overview

**SwifPro BI** is a small-business ERP (Enterprise Resource Planning) system. In
plain terms, it helps a company run its day-to-day operations in one place:

- buying stock from suppliers,
- selling to customers,
- tracking inventory,
- and keeping the books (accounting) up to date automatically.

**What this demo includes:** a working version of the core ERP — products,
customers, suppliers, quotes, orders, invoices, purchasing, goods receipts,
inventory, transfers, stock counts, returns, replenishment, kits/bundles, and
full accounting (general ledger, trial balance, VAT, reports).

**What this demo is for:** trying out the screens and workflows, giving feedback,
and manual testing.

**What this demo is _not_ for:**
- It is **not** live production use.
- Do **not** enter real customer, supplier, or financial data.
- Data is **not guaranteed to persist** (see free-tier notes in §8 and §9).

> This is a **controlled demo / release candidate (v0.1.0-rc3)**. It is feature-rich
> and tested, but it is a candidate for release — please treat it as a preview.

---

## 2. Demo Login Details

Five demo accounts are available. **All of them have full application admin
access**, so you can explore everything.

| Username | Type | Access |
|---|---|---|
| `santhosh` | Owner / Superuser | Full app admin **plus** system admin (`/admin/`) |
| `admin1` | Demo admin | Full app admin |
| `admin2` | Demo admin | Full app admin |
| `admin3` | Demo admin | Full app admin |
| `admin4` | Demo admin | Full app admin |

**Passwords will be shared separately** — they are not printed in this handbook.

> All five accounts belong to the demo company **"SwifPro BI Demo Ltd"**.

---

## 3. First Login Steps

1. **Open the app link.** Go to the demo URL you were given
   (typically `https://swifpro-bi-demo.onrender.com`). The exact link is shared
   separately.
2. **Log in.** On the login page, enter your **username** (e.g. `admin1`) and the
   password you were given, then sign in.
3. **Check the Dashboard.** After login you land on the **Dashboard** — a summary
   of sales, money owed, low-stock items, and a "stuck stock" worklist card.
4. **Use the navigation menu.** The left-hand (or hamburger ☰) menu groups
   everything into sections: **Sales, Procurement, Inventory, Finance, Reports,
   Administration**. Click a section, then a page.
5. **Use global search.** Use the search box (top of the page) to jump straight
   to a module by name — try typing "products", "trial balance", or "returns".
6. **Use Back safely.** Each page has a **Back** link (and your browser's Back
   button works too). Use the in-page Back/Cancel links to return without losing
   your place.
7. **Log out when finished.** Use the **Log out** option in the menu/profile area.

---

## 4. Roles & Permissions (what the account types mean)

SwifPro BI supports several user roles. **In this demo, every provided account is
an Owner/Admin**, so you won't hit "access denied" while exploring. The roles
below exist in the system for context — but **no demo accounts are pre-created**
for Sales-only, Warehouse-only, Finance-only, or Read-only users.

| Role | What it can do |
|---|---|
| **Owner / Superuser** (`santhosh`) | Everything in the app, **plus** the technical system admin at `/admin/` (manage users). |
| **App Admin** (`admin1`–`admin4`) | Full access to every business module: sales, purchasing, inventory, finance, reports, settings. Not a system superuser. |
| **Manager** | Broad operational access across sales, purchasing, and inventory. |
| **Sales Staff** | Quotes, orders, customer invoices, customers, returns. |
| **Warehouse Staff** | Inventory, movements, transfers, counts, stock-takes, goods receipt. |
| **Purchasing Staff** | Requisitions, purchase orders, suppliers, replenishment. |
| **Accountant / Finance** | Payments, expenses, VAT, journals, the general ledger, and financial reports. |
| **Read-only User** | Can view reports and lists, but cannot create or change anything. |

> Tip: the menu automatically hides pages a role can't use. Because you're an
> admin, you'll see the full menu.

---

## 5. Main Modules Guide

Only modules that actually exist in the app are listed. Menu section shown in
**bold**.

### Dashboard
**Dashboard** — your home screen: key numbers (sales, receivables, low stock) and
a worklist card for stock that's stuck in transit or on hold.

### Master data (the "who and what")
- **Products** (*Inventory*) — the items you buy/sell/stock (SKU, name, cost,
  tracking options like lot/serial).
- **Product Categories** (*Inventory*) — grouping for products.
- **Customers** (*Sales*) — who you sell to.
- **Suppliers** (*Procurement*) — who you buy from.
- **Units of Measure** & **UOM Conversions** (*Inventory*) — e.g. "1 case = 12 each".
- **Sites**, **Locations**, **Bins** (*Inventory*) — where stock lives (a company
  can have multiple sites and warehouses). *Bins are advisory — see §7.*

### Sell side (Order-to-Cash)
- **Quotes** (*Sales*) — price proposals for a customer.
- **Sales Orders** (*Sales*, `/customer-orders/`) — confirmed customer orders
  (the normal sales order in this app).
- **Customer Invoices** (*Sales*) — bills you send to customers; issuing one
  reduces stock and books revenue + cost of sale automatically.
- **Recurring Invoices** (*Sales*) — invoices that repeat on a schedule.
- **Returns (RMA)** (*Sales*) — customer returns and how they're handled.
- **Channel Orders (Experimental)** (*Sales*, `/sales-orders/`) — see §7.

### Buy side (Procure-to-Pay)
- **Purchase Requisitions** (*Procurement*) — internal "we need to buy this" requests.
- **Purchase Orders** (*Procurement*) — official orders sent to suppliers.
- **Goods Receipts (GRN)** — recorded by opening a Purchase Order and choosing
  **Receive** (there is no separate top-level menu for this); receiving adds stock.
- **Supplier Invoices** (*Procurement/Finance*) — bills you receive from suppliers;
  posting one clears the "goods received" accrual and records what you owe.
- **Shipments** (*Procurement*) — inbound shipment tracking.
- **Backorders** (*Procurement*) — outstanding PO quantities not yet received.

### Inventory & stock control
- **Inventory** (*Inventory*, `/inventory/`) — current **stock balances** by
  product and location.
- **Stock Movements** (*Inventory*) — the full history of every stock change.
- **Serial Availability** (*Inventory*) — which serial-numbered units are available.
- **Low Stock** & **Replenishment** (*Inventory*) — what to reorder and suggested
  quantities.
- **Inventory Worklist** (*Inventory*) — stock stuck in transit or on hold/quarantine.
- **Transfers** (*Inventory*) — move stock between locations (two-step: dispatch → receive).
- **Stock Adjustments** (*Inventory*) — correct stock up/down (with approval).
- **Cycle Counts** (*Inventory*) — count part of a location and post differences.
- **Stock Takes** (*Inventory*) — a full physical count of a whole location/site.
- **BOMs / Kits** (*Inventory*) — kit/bundle definitions. *Kits/bundles only — see §7.*

### Finance / accounting
- **Payments** (*Finance*) — money received from customers / paid to suppliers.
- **Expenses** (*Finance*) — business costs.
- **Credit Notes** (*Finance*) — refunds/credits against invoices.
- **Bank Transactions** & **Bank Reconciliation** (*Finance*).
- **Tax Codes (VAT)** and **VAT Returns / VAT Records** (*Finance*). *VAT submit is
  local-only — see §7.*
- **Journal** (*Finance*, `/gl/journal/`) — the **general ledger** entries.
- **Chart of Accounts** (*Finance*, `/gl/accounts/`) — the list of accounts.

### Reports
**Reports** section includes **Trial Balance**, **Profit & Loss**, **Balance
Sheet**, **Cash Flow Summary**, **Stock Valuation**, **Inventory Analytics**,
**Near-Expiry Stock**, **Lot Traceability**, **Stock-Take Variances**, **Aged
Debtors/Creditors**, **Profitability**, **Consolidated (Group)**, and
**Inter-company**. *Inter-company / consolidation has a limitation — see §7.*

> **Inventory ↔ GL reconciliation:** this is an accounting health check (does the
> stock value match the inventory account in the ledger?). In the demo, verify it
> via the **Trial Balance** report and the **Stock Valuation** report; a deeper
> automated check exists as an admin/management command (`check_inventory_gl`).
> There is no separate demo page for it.

### Administration / settings
**Administration** section: **Setup & Onboarding**, **Company Profile**, **Company
Group**, **Users & Roles**, **Departments**, **Roles & Permissions**, **Access
Requests**, **Audit Log**, **Email Log**. (Admin only.)
- **Audit Log** (`/audit/`) — a trail of who did what, when.

---

## 6. Recommended Demo Walkthrough

Follow these end-to-end flows to see how the modules connect. Use the menu or
global search to reach each page. Save as you go.

### Flow A — Procure-to-Pay (buying stock)
1. **Suppliers** → create a supplier.
2. **Products** → create a product (give it an SKU and a cost).
3. **Purchase Requisitions** → create a requisition for that product, then submit/approve it.
4. **Purchase Orders** → create (or convert from the requisition) a PO to your supplier and approve it.
5. Open the PO → **Receive** to record a **Goods Receipt (GRN)** → stock goes up.
6. **Supplier Invoices** → create/post the supplier's bill against that PO/receipt.
7. Check results: **Inventory** (stock increased) and **Journal / Trial Balance**
   (inventory, GRNI, and payables updated).

### Flow B — Order-to-Cash (selling stock)
1. **Customers** → create a customer.
2. **Quotes** → create a quote for the product.
3. Convert the quote to a **Sales Order** (*Sales Orders* / `/customer-orders/`).
4. Create a **Customer Invoice** from the order.
5. Issue the invoice → stock is reduced and **cost of sale (COGS)** is posted automatically.
6. Check results: **Trial Balance** (revenue, receivables, COGS) and **Inventory**
   (stock reduced).

### Flow C — Returns (RMA)
1. **Returns (RMA)** → create a return for a previously sold item.
2. Choose a disposition: **Restock** (sellable again), **Quarantine** (inspection
   hold), or **Scrap** (write-off).
3. If Quarantine/Repair, later **resolve the hold** (release to sellable or scrap).
4. Check the **Inventory Worklist** — unresolved holds appear there until resolved.

### Flow D — Stock Controls
1. **Stock Adjustments** → adjust a product's quantity up/down (note the approval step).
2. **Cycle Counts** → count one location, enter counted quantities, approve & post.
3. **Stock Takes** → start a full count of a location/site (snapshot → count →
   review → approve → post).

### Flow E — Transfers (two-step)
1. **Transfers** → create a transfer between two locations and **dispatch** it
   (stock leaves the source and is "in transit").
2. **Partially receive** it at the destination (some now, some later).
3. **Close short** if the rest never arrives (the missing amount is written off).

### Flow F — BOM / Kits
1. **BOMs / Kits** → create a kit for a "parent" product.
2. Add component lines using line numbers **10, 20, 30** (so you can insert more later).
3. Sell the kit (via a Sales Order/Invoice).
4. Confirm the **component** stock is relieved (not the kit SKU) and cost is posted.

---

## 7. Experimental / Advisory Features (read before judging these)

These are **intentionally visible but clearly labelled** because they are partial:

- **Channel Orders (Experimental)** — a manual channel-style order tool. It is
  **not** connected to Shopify/Amazon sync yet. (Normal sales use **Sales Orders**.)
- **Bins (Advisory)** — bin balances are **advisory**: they're only accurate for
  stock that was explicitly bin-tagged. The app is **not** fully bin-aware for
  receiving, picking, transfers, or availability yet.
- **VAT submit — local only** — VAT figures are calculated correctly, but
  "submit" only marks the return submitted **locally**; it does **not** file to
  HMRC (no Making Tax Digital integration).
- **BOM = Kits / Bundles only** — supports selling a kit and relieving its
  components. It does **not** include manufacturing work orders or build-to-stock.
- **Inter-company** — inter-company trading and **P&L** elimination work, but
  **balance-sheet** elimination (intra-group receivables/payables/equity) is
  **not complete** yet.

Each of these pages shows an on-screen badge/notice saying the same thing.

---

## 8. What Not To Do In This Demo

- ❌ Do **not** enter real customer, supplier, or employee data.
- ❌ Do **not** enter real financial records or anything confidential.
- ❌ Do **not** reuse real/work passwords.
- ❌ Do **not** rely on the data surviving — the free hosting can reset it.
- ❌ Do **not** treat this as a live production ERP.
- ❌ Do **not** share the demo link publicly without permission.

---

## 9. Troubleshooting

| Symptom | What to do |
|---|---|
| First page is slow (~30–60s) | Normal — the free host **sleeps when idle** and takes a moment to wake. Wait, then retry. |
| A page shows an error | **Refresh once.** If it persists, note the page and copy any error text. |
| Login fails | Re-check the **username** and the **password** you were given (case-sensitive). |
| The page looks unstyled (no colours/layout) | Report it (a styling/static-files issue) with a screenshot. |
| A save/transaction fails | **Copy the exact error message** and which page/step you were on. |
| "Access denied" / 403 | A role/permission limit — but demo accounts are admins, so report it if it happens. |
| Data you entered is gone | Possible after a free-tier restart — re-enter for testing; don't store anything important. |

---

## 10. Feedback Template (copy & fill in)

```
SwifPro BI demo feedback
------------------------
1. What were you trying to do?
2. Which page/module?
3. What worked well?
4. What failed or looked wrong?
5. Screenshot / exact error message:
6. Was anything confusing or unclear?
7. Priority: low / medium / high / blocker
```

---

## 11. Admin Notes (for the demo owner)

This deployment runs on **Render free tier** and uses an **environment-variable
based admin bootstrap** (no Shell needed). Passwords are **never stored in the
code or this handbook** — they live only in Render environment variables.

- **Create / refresh demo users:** the build runs
  `python manage.py bootstrap_demo_admins` automatically. It creates the owner
  (`santhosh`) and `admin1`–`admin4` from env vars, is idempotent, and never
  resets an existing user's password.
- **Enable / disable the four demo admins:** set `DEMO_ADMINS_ENABLED=1` (with
  `DEMO_ADMIN_DEFAULT_PASSWORD`) to enable; set `0` for owner-only. Existing
  users are not auto-deleted.
- **Rotate demo passwords:** update `DJANGO_SUPERUSER_PASSWORD` /
  `DEMO_ADMIN_DEFAULT_PASSWORD` in Render, **delete the affected user(s)** in the
  Django admin (`/admin/auth/user/`), then redeploy to recreate them with the new
  password.
- **Disable / delete demo admins (before any real use):** in `/admin/`, untick
  **Active** to block login, or delete `admin1`–`admin4` (and the "SwifPro BI
  Demo Ltd" tenant), then set `DEMO_ADMINS_ENABLED=0`.
- **Free-tier reminders:** the web service sleeps when idle; the free database is
  time-limited; uploaded files are **not** persistent across restarts.

Full deployment detail: `docs/deployment/render_demo.md`.

---

*SwifPro BI v0.1.0-rc3 — controlled demo. Not for production data.*
