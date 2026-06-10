# SwifPro BI — Quick Start Demo Guide (v0.1.0-rc3)

A 2-minute guide to start testing the **SwifPro BI** ERP demo. (Full details:
*SwifPro BI First-Time User Handbook*.)

> **This is a controlled demo / release candidate — not live production.**
> Don't enter real data. Don't reuse real passwords. Data may reset at any time.

---

## 1. Log in

1. Open the demo link you were given (e.g. `https://swifpro-bi-demo.onrender.com`).
2. Enter your **username** and the **password shared with you**.

**Demo accounts (all have full admin access):**

| Username | Type |
|---|---|
| `santhosh` | Owner / superuser |
| `admin1` – `admin4` | Demo admins |

*Passwords are shared separately — not in this guide.*

> First load can take **30–60 seconds** (the free host wakes from sleep). That's normal.

---

## 2. Find your way around

- **Dashboard** = your home screen (sales, money owed, low stock, stuck-stock worklist).
- **Menu** (left / ☰) groups everything: **Sales · Procurement · Inventory · Finance · Reports · Administration**.
- **Search box** (top) jumps to any page — try "products", "trial balance", "returns".
- **Back** links return you safely; **Log out** when done.

---

## 3. Try these quick flows

**Sell something (Order-to-Cash):**
Customers → create · Quotes → create → convert to **Sales Order** → **Customer
Invoice** → issue. Stock drops and cost-of-sale posts automatically. Check
**Reports → Trial Balance**.

**Buy something (Procure-to-Pay):**
Suppliers → create · Products → create · **Purchase Requisitions** → **Purchase
Orders** → open the PO and **Receive** (records a goods receipt, stock goes up) →
**Supplier Invoices** → post. Check **Inventory**.

**Move / count / return stock:**
- **Transfers** → dispatch → partial receive → close short.
- **Stock Adjustments**, **Cycle Counts**, **Stock Takes**.
- **Returns (RMA)** → restock / quarantine / scrap → see **Inventory Worklist**.

**Kits:** **BOMs / Kits** → create a kit, add component lines (10/20/30), sell the
kit → its components are relieved.

---

## 4. Features labelled experimental / advisory (by design)

- **Channel Orders (Experimental)** — manual tool; **not** connected to Shopify/Amazon.
- **Bins (Advisory)** — bin balances are advisory, not a full bin-aware warehouse yet.
- **VAT submit** — **local only**; does **not** file to HMRC.
- **BOM** — kits/bundles only; no manufacturing work orders.
- **Inter-company** — P&L elimination works; balance-sheet elimination is incomplete.

---

## 5. If something goes wrong

- Slow first page → wait, the host was asleep.
- Page error → **refresh once**; if it persists, note the page + copy the error.
- Login fails → re-check username/password (case-sensitive).
- Save fails or looks broken → copy the **exact error/screenshot** and report it.

---

## 6. Send feedback (copy & fill in)

```
1. What were you trying to do?
2. Which page/module?
3. What worked?
4. What failed (error/screenshot)?
5. Anything confusing?
6. Priority: low / medium / high / blocker
```

**Please don't:** enter real data · reuse real passwords · share the link without
permission · rely on the data being saved.

*Thanks for testing SwifPro BI v0.1.0-rc3!*
