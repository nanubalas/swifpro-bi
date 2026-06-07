# UK Retail Group — seed/test metadata (Company → Site → Inventory Location)

Realistic UK-based seed data proving the ERP keeps three tiers strictly separate:

```text
Company / Organisation     UK Retail Group Ltd          legal/business entity
        ↓
Site / Branch / Region     London · Leicester ·         operating & reporting tier
                           Manchester · Birmingham
        ↓
Inventory Location         Main Warehouse · Shop Floor · physical stock storage
                           Back Room · Returns Area
```

**Run it:** `python manage.py seed_uk_retail_demo` (idempotent — safe to re-run).
Source: [core/management/commands/seed_uk_retail_demo.py](../core/management/commands/seed_uk_retail_demo.py).
Tests: `UkRetailDemoScenarioTests` in [core/tests.py](../core/tests.py) (13 tests, all passing).

> Field-name note: the spec's `site_code`/`address`/`city`/`postcode`/`barcode`/`READ_ONLY`
> map to the real models as `Site.code`, a single `Site.address` text field, the
> separate `ProductBarcode` model, and role code `READONLY`. Stock is keyed by
> `(tenant, product, location)` with `site` auto-synced from the location — it is
> **not** stored on the product. These are reflected below.

---

## 1. Seed data structure

### Company (Tenant)
| Field | Value |
|---|---|
| name | UK Retail Group Ltd |
| legal_name | UK Retail Group Limited |
| country | United Kingdom |
| currency_code | GBP |
| vat_registered | true (`vat_number` GB123456789) |

### Sites (4 — `London` is `is_default`)
| code | name | site_type | region | address | is_default | is_active |
|---|---|---|---|---|---|---|
| LON | London | city_branch | Greater London | 1 Oxford Street, London, W1D 1AN | ✅ | ✅ |
| LEI | Leicester | city_branch | East Midlands | 14 Gallowtree Gate, Leicester, LE1 5AD | — | ✅ |
| MAN | Manchester | city_branch | North West | 20 Market Street, Manchester, M1 1WR | — | ✅ |
| BIR | Birmingham | city_branch | West Midlands | 50 New Street, Birmingham, B2 4DU | — | ✅ |

### Inventory Locations (4 per site = 16)
For **each** site: `<Site> Main Warehouse` (WAREHOUSE), `<Site> Shop Floor` (SHOP_FLOOR),
`<Site> Back Room` (BACK_ROOM), `<Site> Returns Area` (RETURNS).
Inventory locations are **never** treated as sites — they each carry a `site_id` FK.

### Users & roles (8 — explicit per-site access, **no "all sites"**)
| username | role | site access (`UserSiteAccess`) |
|---|---|---|
| admin@ukretail.demo | Owner/Admin | London, Leicester, Manchester, Birmingham |
| accountant@ukretail.demo | Accountant | London, Leicester, Manchester, Birmingham |
| manager@ukretail.demo | Manager | London, Leicester |
| sales@ukretail.demo | Sales Staff | London |
| warehouse@ukretail.demo | Warehouse Staff | Manchester *(+ location grant: Manchester Main Warehouse)* |
| purchasing@ukretail.demo | Purchasing Staff | Birmingham |
| finance@ukretail.demo | Finance Staff | London, Leicester, Manchester, Birmingham |
| readonly@ukretail.demo | Read-only User | London, Manchester |

Password for all demo users: `Demo!2026`. Admin/Accountant/Finance get rows for every
site **individually** (the system never offers an "All Sites" toggle).

### Products (company level — no site)
| sku | name | category | brand | sales_price | standard_cost | tax | barcode |
|---|---|---|---|---|---|---|---|
| UKR-001 | LED Desk Lamp | Lighting | Lumos | 24.99 | 9.50 | STD 20% | 5012345000018 |
| UKR-002 | Wireless Mouse | Electronics | Clikr | 14.99 | 4.20 | STD 20% | 5012345000025 |

### Stock balances (`InventoryBalance`, keyed by tenant + product + location; site synced)
Opening 100 units of each product at every site's **Main Warehouse**. After the sample
London invoice (−5 of UKR-001) and a manual −2 adjustment:

| product | location | site | on_hand |
|---|---|---|---|
| UKR-001 | London Main Warehouse | London | 93 |
| UKR-001 | Manchester Main Warehouse | Manchester | 100 |
| UKR-002 | each Main Warehouse | (its site) | 100 |

### Sample transactions (each carries company + site; inventory ones add a location)
| document | number | site | inventory location |
|---|---|---|---|
| Sales order | SO-0001 | London | London Main Warehouse |
| Customer invoice (posted) | INV-0001 | London | London Main Warehouse |
| Purchase order | PO-0001 | London | London Main Warehouse (receiving) |
| Goods receipt | GRN-0001 | London | London Main Warehouse (`received_to`) |
| Expense | EXP-0001 | London (default) | — (expenses have no location) |
| Stock movement | SEED-ADJ | London | London Main Warehouse |

---

## 2. JSON seed representation (illustrative)

The executable seed is the management command; this JSON shows the same shape.

```jsonc
{
  "company": { "name": "UK Retail Group Ltd", "country": "United Kingdom",
               "currency_code": "GBP", "vat_registered": true },
  "sites": [
    { "code": "LON", "name": "London", "site_type": "city_branch",
      "region": "Greater London", "is_default": true, "is_active": true,
      "inventory_locations": [
        { "name": "London Main Warehouse", "type": "WAREHOUSE" },
        { "name": "London Shop Floor",     "type": "SHOP_FLOOR" },
        { "name": "London Back Room",      "type": "BACK_ROOM" },
        { "name": "London Returns Area",   "type": "RETURNS" }
      ] }
    /* … Leicester (LEI), Manchester (MAN), Birmingham (BIR) — same 4 locations … */
  ],
  "users": [
    { "username": "sales@ukretail.demo", "role": "SALES", "sites": ["London"] },
    { "username": "warehouse@ukretail.demo", "role": "WAREHOUSE", "sites": ["Manchester"],
      "locations": ["Manchester Main Warehouse"] }
    /* … 6 more, each with explicit "sites": [...]  — never "all" … */
  ],
  "products": [
    { "sku": "UKR-001", "name": "LED Desk Lamp", "sales_price": "24.99",
      "standard_cost": "9.50", "vat": "STD", "barcode": "5012345000018" }
  ],
  "stock_balances": [
    { "company": "UK Retail Group Ltd", "site": "London",
      "inventory_location": "London Main Warehouse", "product": "UKR-001", "on_hand": 93 }
  ],
  "transactions": [
    { "type": "sales_invoice", "number": "INV-0001",
      "company": "UK Retail Group Ltd", "site": "London",
      "inventory_location": "London Main Warehouse" }
  ]
}
```

---

## 3. Relationships

```text
Tenant (UK Retail Group Ltd)
 ├─ Site (London, default) ── Location ×4 ── (Bin*)
 │     ├─ InventoryBalance (per product, per location; site = location.site)
 │     └─ documents stamped site=London (SO/INV/PO/GRN/Expense)
 ├─ Site (Leicester) ── Location ×4
 ├─ Site (Manchester) ── Location ×4
 ├─ Site (Birmingham) ── Location ×4
 ├─ Product ×2 ……………………………… company-level (NO site FK)
 ├─ OrgMembership ×8 (user ↔ tenant ↔ role)
 └─ UserSiteAccess (user ↔ site, explicit)   UserLocationAccess (user ↔ location)
```

- **Company → Site**: `Site.tenant` (CASCADE). One default site per company.
- **Site → Inventory Location**: `Location.site` (SET_NULL; auto-pinned in seed).
- **Stock**: `InventoryBalance`/`InventoryMovement` key on `location`; `site` mirrors `location.site`.
- **Documents**: `CustomerOrder.location`, `CustomerInvoice.location`, `PurchaseOrder.receiving_location`,
  `GoodsReceipt.received_to` → `site` auto-derived via `_derive_doc_site`. `Expense` has site only.
- **Products/customers/suppliers/VAT/chart-of-accounts**: company-level (no site dimension).
- **Access**: company via `OrgMembership`; site via `UserSiteAccess`; inventory location via `UserLocationAccess`.

---

## 4. Test scenarios (`UkRetailDemoScenarioTests`)

| # | Test | Proves |
|---|---|---|
| — | `test_company_sites_and_locations_structure` | 1 company, 4 sites (London default), 4 typed locations each, no re-homing |
| — | `test_eight_role_users_with_explicit_site_access` | 8 users, explicit site grants (SALES=London, WAREHOUSE=Manchester, …) |
| 1 | `test_scenario_01_login_selects_company_and_site` | login resolves to one Company + one Site |
| 2 | `test_scenario_02_switch_leicester_to_london` | switching site via `/switch-workspace/` |
| 3 | `test_scenario_03_products_are_company_level` | products have no site dimension |
| 4 | `test_scenario_04_stock_scoped_to_site_and_location` | stock changes only at the affected site+location |
| 5 | `test_scenario_05_sales_filtered_by_site` | AR invoices filtered by active site |
| 6 | `test_scenario_06_purchasing_filtered_by_site` | POs filtered by active site |
| 7 | `test_scenario_07_reports_filtered_by_site` | site-dimensioned P&L (London > 0, Manchester = 0) |
| 8 | `test_scenario_08_warehouse_user_cannot_access_other_site` | 403 + audit on cross-site access |
| 9 | `test_scenario_09_inventory_location_only_in_inventory_workflows` | global context = Company+Site only (no location) |
| 10 | `test_scenario_10_no_all_sites_option` | concrete sites only; no "All Sites" anywhere |
| 10b | `test_scenario_10b_inventory_scoped_for_warehouse_user` | restricted user sees only their location's stock |

Run: `python manage.py test core.tests.UkRetailDemoScenarioTests`

---

## 5. Validation checklist

- [x] One company; legal entity attributes (GBP, VAT registered).
- [x] Four sites with `code`, `site_type`, `region`, `address`, `is_default`, `is_active`.
- [x] London is the single default operating site.
- [x] Four inventory locations per site (16), each pinned to its own site.
- [x] Inventory locations are not sites (distinct model, each with `site_id`).
- [x] Eight role users, each linked to the company and to one or more sites.
- [x] Every user has explicit `UserSiteAccess` — **no "All Sites"** option exists.
- [x] Products are company-level (no per-site product rows / no site FK).
- [x] Stock keyed by company + location (+ synced site), never only on the product.
- [x] Each transaction carries company + site; inventory ones carry an inventory location.
- [x] Sales, purchasing, reports and dashboards filter by the selected site.
- [x] Cross-site access is blocked (403) and audited.
- [x] Seed is idempotent.
