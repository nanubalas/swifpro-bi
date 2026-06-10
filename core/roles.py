"""Role taxonomy for role-based dashboards, routing and navigation.

These 8 organisation roles drive the post-login landing page and the sidebar.
They sit on top of the existing Django-group RBAC (core.auth): each role maps
to one or more groups so the established `role_required` enforcement keeps
working. Assigning a membership role syncs the user's groups accordingly.
"""

# Membership role codes
ADMIN = "ADMIN"            # Owner / Admin
ACCOUNTANT = "ACCOUNTANT"
MANAGER = "MANAGER"
SALES = "SALES"
WAREHOUSE = "WAREHOUSE"
PURCHASING = "PURCHASING"
FINANCE = "FINANCE"
READONLY = "READONLY"

ROLE_CHOICES = [
    (ADMIN, "Owner / Admin"),
    (ACCOUNTANT, "Accountant"),
    (MANAGER, "Manager"),
    (SALES, "Sales Staff"),
    (WAREHOUSE, "Warehouse Staff"),
    (PURCHASING, "Purchasing Staff"),
    (FINANCE, "Finance Staff"),
    (READONLY, "Read-only User"),
]

ROLE_LABELS = dict(ROLE_CHOICES)
ALL_ROLE_CODES = [c for c, _ in ROLE_CHOICES]

# Membership role -> existing Django groups (core.auth group names) so the
# existing per-view RBAC enforces the right access.
ROLE_TO_GROUPS = {
    ADMIN: ["Admin"],
    ACCOUNTANT: ["Finance"],
    MANAGER: ["Procurement", "Warehouse", "Sales"],
    SALES: ["Sales"],
    WAREHOUSE: ["Warehouse"],
    PURCHASING: ["Procurement"],
    FINANCE: ["Finance"],
    READONLY: ["Read-only"],
}

# Role -> default dashboard URL name
DASHBOARD_ROUTE = {
    ADMIN: "dashboard_admin",
    ACCOUNTANT: "dashboard_accountant",
    MANAGER: "dashboard_manager",
    SALES: "dashboard_sales",
    WAREHOUSE: "dashboard_warehouse",
    PURCHASING: "dashboard_purchasing",
    FINANCE: "dashboard_finance",
    READONLY: "dashboard_readonly",
}

# URL-name choices an admin may pick as a role's default landing page.
LANDING_CHOICES = [
    ("dashboard_admin", "Admin overview"),
    ("dashboard_accountant", "Accountant dashboard"),
    ("dashboard_manager", "Operations dashboard"),
    ("dashboard_sales", "Sales dashboard"),
    ("dashboard_warehouse", "Warehouse dashboard"),
    ("dashboard_purchasing", "Purchasing dashboard"),
    ("dashboard_finance", "Finance dashboard"),
    ("dashboard_readonly", "Read-only dashboard"),
    ("reports_index", "Reports"),
    ("vat_index", "VAT returns"),
    ("inventory_list", "Inventory"),
]

DASHBOARD_TITLE = {
    ADMIN: "Business Overview",
    ACCOUNTANT: "Accounting Dashboard",
    MANAGER: "Operations Dashboard",
    SALES: "Sales Dashboard",
    WAREHOUSE: "Warehouse Dashboard",
    PURCHASING: "Purchasing Dashboard",
    FINANCE: "Finance Dashboard",
    READONLY: "Reporting Dashboard",
}


# ---------------------------------------------------------------------------
# Sidebar definition. Each item is (label, url path, bootstrap-icon, {roles}).
# A section renders only if it has at least one visible item for the role.
# ADMIN sees everything.
# ---------------------------------------------------------------------------
_ALL = set(ALL_ROLE_CODES)
_OPS = {ADMIN, MANAGER}

NAV = [
    ("Dashboard", [
        ("Dashboard", "/dashboard/", "grid-1x2", _ALL),
    ]),
    ("Sales", [
        ("Quotes", "/quotes/", "file-earmark-text", {ADMIN, MANAGER, SALES}),
        ("Sales Orders", "/customer-orders/", "cart-check", {ADMIN, MANAGER, SALES}),
        ("Customer Invoices", "/ar/invoices/", "receipt", {ADMIN, MANAGER, SALES, ACCOUNTANT, FINANCE}),
        ("Recurring Invoices", "/recurring-invoices/", "arrow-repeat", {ADMIN, MANAGER, SALES, ACCOUNTANT, FINANCE}),
        ("Customers", "/customers/", "people", {ADMIN, MANAGER, SALES, ACCOUNTANT, FINANCE}),
        ("Returns (RMA)", "/returns/", "arrow-return-left", {ADMIN, MANAGER, SALES, WAREHOUSE}),
        ("Channel Orders", "/sales-orders/", "bag", {ADMIN, MANAGER, SALES}),
        ("Sales Reports", "/sales/reports/", "graph-up", {ADMIN, MANAGER, SALES, ACCOUNTANT, FINANCE}),
    ]),
    ("Procurement", [
        ("Purchase Requisitions", "/requisitions/", "card-checklist", {ADMIN, MANAGER, PURCHASING, WAREHOUSE}),
        ("Purchase Orders", "/po/", "file-earmark-text", {ADMIN, MANAGER, PURCHASING}),
        ("Backorders", "/po/backorders/", "hourglass-split", {ADMIN, MANAGER, PURCHASING}),
        ("Suppliers", "/suppliers/", "shop", {ADMIN, MANAGER, PURCHASING, ACCOUNTANT, FINANCE}),
        ("Shipments", "/shipments/", "truck", {ADMIN, MANAGER, PURCHASING, WAREHOUSE}),
        ("Supplier Invoices", "/invoices/", "receipt-cutoff", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Supplier Scorecard", "/reports/supplier-scorecard/", "speedometer2", {ADMIN, MANAGER, PURCHASING, FINANCE, ACCOUNTANT}),
    ]),
    ("Inventory", [
        ("Inventory", "/inventory/", "boxes", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Stock Adjustments", "/inventory/adjustments/", "sliders", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Stock Movements", "/inventory/movements/", "list-columns-reverse", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Serial Availability", "/inventory/serials/", "upc-scan", {ADMIN, MANAGER, WAREHOUSE, PURCHASING, SALES}),
        ("Low Stock", "/inventory/low-stock/", "exclamation-triangle", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Replenishment", "/inventory/replenishment/", "graph-up-arrow", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Inventory Worklist", "/inventory/worklist/", "list-task", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Transfers", "/transfers/", "arrow-left-right", {ADMIN, MANAGER, WAREHOUSE}),
        ("Cycle Counts", "/cycle-counts/", "clipboard-check", {ADMIN, MANAGER, WAREHOUSE}),
        ("Stock Takes", "/stock-takes/", "ui-checks-grid", {ADMIN, MANAGER, WAREHOUSE, FINANCE}),
        ("Sites", "/sites/", "buildings", {ADMIN, MANAGER, WAREHOUSE}),
        ("Site Access", "/sites/access/", "diagram-3-fill", {ADMIN}),
        ("Locations", "/locations/", "geo-alt", {ADMIN, MANAGER, WAREHOUSE}),
        ("Bins", "/bins/", "grid-3x3-gap", {ADMIN, MANAGER, WAREHOUSE}),
        ("Location Access", "/locations/access/", "person-lock", {ADMIN}),
        ("Products", "/products/", "box-seam", {ADMIN, MANAGER, WAREHOUSE, PURCHASING, SALES}),
        ("Product Categories", "/product-categories/", "tags", {ADMIN, MANAGER, PURCHASING}),
        ("BOMs / Kits", "/boms/", "diagram-3", {ADMIN, MANAGER, PURCHASING}),
        ("Units of Measure", "/uoms/", "rulers", {ADMIN}),
        ("UOM Conversions", "/uom-conversions/", "shuffle", {ADMIN}),
    ]),
    ("Finance", [
        ("Payments", "/payments/", "cash-stack", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Expenses", "/expenses/", "wallet2", {ADMIN, ACCOUNTANT, FINANCE, MANAGER, SALES, WAREHOUSE, PURCHASING}),
        ("Credit Notes", "/credit-notes/", "receipt", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Bank Transactions", "/bank/transactions/", "bank", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Bank Reconciliation", "/bank/reconcile/", "check2-square", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Tax Codes (VAT)", "/tax-codes/", "percent", {ADMIN, ACCOUNTANT, FINANCE}),
        ("VAT Returns (MTD)", "/vat/", "file-earmark-spreadsheet", {ADMIN, ACCOUNTANT, FINANCE}),
        ("VAT Records", "/vat/records/", "card-checklist", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Journal", "/gl/journal/", "journal-text", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Chart of Accounts", "/gl/accounts/", "bank", {ADMIN, ACCOUNTANT, FINANCE}),
    ]),
    ("Reports", [
        ("All Reports", "/reports/", "bar-chart-line", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Profit & Loss", "/reports/profit-and-loss/", "graph-up-arrow", {ADMIN, ACCOUNTANT, READONLY}),
        ("Balance Sheet", "/reports/balance-sheet/", "bank2", {ADMIN, ACCOUNTANT, READONLY}),
        ("Trial Balance", "/reports/trial-balance/", "table", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Cash Flow Summary", "/reports/cash-flow/", "arrow-left-right", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Stock Valuation", "/reports/stock-valuation/", "box-seam", {ADMIN, ACCOUNTANT, MANAGER, WAREHOUSE, PURCHASING, READONLY}),
        ("Inventory Analytics", "/reports/inventory-analytics/", "graph-up", {ADMIN, ACCOUNTANT, MANAGER, WAREHOUSE, PURCHASING, READONLY}),
        ("Near-Expiry Stock", "/reports/near-expiry/", "hourglass-split", {ADMIN, ACCOUNTANT, MANAGER, WAREHOUSE, PURCHASING, FINANCE, READONLY}),
        ("Lot Traceability", "/reports/lot-trace/", "diagram-3", {ADMIN, ACCOUNTANT, MANAGER, WAREHOUSE, PURCHASING, FINANCE, READONLY}),
        ("Stock-Take Variances", "/reports/stock-take/", "ui-checks-grid", {ADMIN, ACCOUNTANT, MANAGER, WAREHOUSE, FINANCE}),
        ("Aged Debtors", "/reports/aged-receivables/", "cash-coin", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Aged Creditors", "/reports/aged-payables/", "credit-card", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Profitability", "/sales/reports/profitability/", "percent", {ADMIN, ACCOUNTANT, MANAGER, FINANCE, READONLY}),
        ("Consolidated (Group)", "/reports/consolidated/", "diagram-2", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Inter-company", "/intercompany/", "arrow-left-right", {ADMIN, ACCOUNTANT, FINANCE}),
    ]),
    ("Administration", [
        ("Setup & Onboarding", "/onboarding/", "rocket-takeoff", {ADMIN}),
        ("Company Profile", "/settings/tenant/", "building-gear", {ADMIN}),
        ("Company Group", "/settings/group/", "diagram-2", {ADMIN}),
        ("Users & Roles", "/users/", "people-fill", {ADMIN}),
        ("Departments", "/departments/", "diagram-3", {ADMIN, MANAGER, READONLY}),
        ("Roles & Permissions", "/team/permissions/", "shield-check", {ADMIN}),
        ("Access Requests", "/access-requests/", "person-plus", {ADMIN}),
        ("Audit Log", "/audit/", "shield-lock", {ADMIN}),
        ("Email Log", "/email-log/", "envelope-paper", {ADMIN, FINANCE}),
    ]),
]


def sidebar_for_role(role):
    """Return [(section_title, [(label, url, icon), ...])] visible to `role`."""
    out = []
    for title, items in NAV:
        visible = [(label, url, icon) for (label, url, icon, roles) in items
                   if role == ADMIN or role in roles]
        if visible:
            out.append((title, visible))
    return out


# ---------------------------------------------------------------------------
# Search metadata. Augments the NAV registry (which stays the single source of
# truth for *what* pages exist and *who* may see them) with a short description
# and search keywords/aliases per page, keyed by url path. The global search and
# the hamburger-menu filter are both derived from NAV via the helpers below, so
# there is no separate page list to keep in sync. Keywords are matched as exact
# tokens (so short aliases like "po" / "gl" don't false-match longer words).
# ---------------------------------------------------------------------------
NAV_META = {
    # Sales
    "/quotes/": {"desc": "Create and manage sales quotations.", "keywords": ["quote", "quotation", "estimate"]},
    "/customer-orders/": {"desc": "Customer sales orders.", "keywords": ["so", "sales order", "order"]},
    "/ar/invoices/": {"desc": "Accounts receivable invoices to customers.", "keywords": ["ar", "receivable", "receivables", "invoice", "customer invoice"]},
    "/recurring-invoices/": {"desc": "Automatically repeating customer invoices.", "keywords": ["recurring", "subscription", "repeat", "standing"]},
    "/customers/": {"desc": "Customer master records.", "keywords": ["customer", "client", "debtor", "account"]},
    "/returns/": {"desc": "Customer return authorisations.", "keywords": ["rma", "return", "returns", "refund authorisation"]},
    "/sales-orders/": {"desc": "Marketplace / channel sales orders.", "keywords": ["channel", "marketplace", "ecommerce", "shopify", "amazon"]},
    "/sales/reports/": {"desc": "Sales performance reports.", "keywords": ["sales report", "analytics"]},
    # Procurement
    "/requisitions/": {"desc": "Internal purchase requests for approval.", "keywords": ["pr", "requisition", "purchase request"]},
    "/po/": {"desc": "Purchase orders to suppliers.", "keywords": ["po", "purchase order", "buying", "ordering"]},
    "/po/backorders/": {"desc": "Outstanding / unfulfilled purchase order quantities.", "keywords": ["backorder", "outstanding", "unfulfilled"]},
    "/suppliers/": {"desc": "Supplier master records.", "keywords": ["supplier", "vendor", "creditor"]},
    "/shipments/": {"desc": "Inbound shipments, containers and receiving.", "keywords": ["grn", "goods receipt", "receiving", "inbound", "container", "asn"]},
    "/invoices/": {"desc": "Accounts payable invoices from suppliers.", "keywords": ["ap", "payable", "payables", "supplier invoice", "bill", "grni"]},
    "/reports/supplier-scorecard/": {"desc": "Supplier performance scorecard.", "keywords": ["scorecard", "supplier performance", "otif"]},
    # Inventory
    "/inventory/": {"desc": "On-hand stock balances and availability.", "keywords": ["stock", "on hand", "atp", "available to promise", "reservation", "reservations", "balance"]},
    "/inventory/adjustments/": {"desc": "Manual stock corrections, damage and write-offs.", "keywords": ["adjustment", "write-off", "writeoff", "damage", "correction"]},
    "/inventory/movements/": {"desc": "Append-only stock movement ledger.", "keywords": ["movement", "ledger", "transactions", "history"]},
    "/inventory/serials/": {"desc": "Available serial-tracked units, with cost and source.", "keywords": ["serial", "serial availability", "serial stock", "serial history", "serial number"]},
    "/inventory/low-stock/": {"desc": "Items below their reorder point.", "keywords": ["low stock", "reorder", "shortage"]},
    "/inventory/replenishment/": {"desc": "Reorder planning: projected availability, suggestions and ABC.", "keywords": ["replenishment", "reorder planning", "purchase suggestions", "min max stock", "safety stock", "eoq", "abc analysis", "reorder point"]},
    "/inventory/worklist/": {"desc": "Stock stuck in transit or in hold/quarantine needing action.", "keywords": ["inventory worklist", "stale transfers", "quarantine", "hold stock", "in transit", "unresolved returns", "stuck stock"]},
    "/transfers/": {"desc": "Move stock between locations (incl. in-transit).", "keywords": ["transfer", "in-transit", "in transit", "move stock"]},
    "/cycle-counts/": {"desc": "Targeted spot stock counts.", "keywords": ["cycle count", "spot count"]},
    "/stock-takes/": {"desc": "Full physical count of a location or whole site.", "keywords": ["stock take", "stocktake", "physical count", "stock-take"]},
    "/sites/": {"desc": "Operating sites / branches.", "keywords": ["site", "branch", "division"]},
    "/sites/access/": {"desc": "Per-user site access control.", "keywords": ["site access", "permissions"]},
    "/locations/": {"desc": "Stock-holding locations (warehouses, stores).", "keywords": ["location", "warehouse", "store"]},
    "/bins/": {"desc": "Bin sub-locations within a location.", "keywords": ["bin", "bin balance", "shelf", "aisle"]},
    "/locations/access/": {"desc": "Per-user location access control.", "keywords": ["location access", "permissions"]},
    "/products/": {"desc": "Product / item master records.", "keywords": ["product", "item", "sku", "catalog", "catalogue"]},
    "/product-categories/": {"desc": "Product category hierarchy.", "keywords": ["category", "categories"]},
    "/boms/": {"desc": "Bills of materials and kits.", "keywords": ["bom", "kit", "bill of materials", "assembly"]},
    "/uoms/": {"desc": "Units of measure.", "keywords": ["uom", "unit", "measure", "units of measure"]},
    "/uom-conversions/": {"desc": "Conversion rules between units of measure.", "keywords": ["uom", "conversion", "convert", "units of measure"]},
    # Finance
    "/payments/": {"desc": "Customer and supplier payments.", "keywords": ["payment", "receipt", "remittance"]},
    "/expenses/": {"desc": "Expense capture and approval.", "keywords": ["expense", "claim", "spend"]},
    "/credit-notes/": {"desc": "Sales and purchase credit notes.", "keywords": ["credit note", "refund"]},
    "/bank/transactions/": {"desc": "Bank account transactions.", "keywords": ["bank", "transaction", "statement"]},
    "/bank/reconcile/": {"desc": "Reconcile bank statements to the ledger.", "keywords": ["bank rec", "reconcile", "reconciliation"]},
    "/tax-codes/": {"desc": "VAT / tax codes.", "keywords": ["vat", "tax", "tax code"]},
    "/vat/": {"desc": "Making Tax Digital VAT returns.", "keywords": ["vat", "mtd", "vat return", "hmrc"]},
    "/vat/records/": {"desc": "VAT digital record keeping.", "keywords": ["vat", "records", "digital records"]},
    "/gl/journal/": {"desc": "General ledger journal entries.", "keywords": ["gl", "journal", "general ledger", "je"]},
    "/gl/accounts/": {"desc": "Chart of accounts.", "keywords": ["coa", "chart of accounts", "gl accounts", "nominal"]},
    # Reports
    "/reports/": {"desc": "All financial and operational reports.", "keywords": ["reports", "reporting"]},
    "/reports/profit-and-loss/": {"desc": "Income vs expenses over a period.", "keywords": ["pnl", "p&l", "profit", "income statement"]},
    "/reports/balance-sheet/": {"desc": "Assets, liabilities and equity.", "keywords": ["balance sheet", "bs"]},
    "/reports/trial-balance/": {"desc": "Every account's debit/credit balance.", "keywords": ["trial balance", "tb"]},
    "/reports/cash-flow/": {"desc": "Cash in vs out over a period.", "keywords": ["cash flow", "cashflow"]},
    "/reports/stock-valuation/": {"desc": "On-hand quantity valued at cost.", "keywords": ["stock valuation", "fifo", "inventory value"]},
    "/reports/inventory-analytics/": {"desc": "Inventory KPIs and analytics.", "keywords": ["inventory analytics", "fifo", "abc"]},
    "/reports/near-expiry/": {"desc": "Expired and soon-to-expire lots.", "keywords": ["expiry", "near expiry", "lot", "shelf life"]},
    "/reports/lot-trace/": {"desc": "Full movement and costing trail for a lot/serial.", "keywords": ["lot", "trace", "traceability", "serial", "genealogy"]},
    "/reports/stock-take/": {"desc": "Stock-take variances, high-value and stale lines.", "keywords": ["stock take", "stocktake", "variance", "physical count"]},
    "/reports/aged-receivables/": {"desc": "Outstanding customer invoices by age.", "keywords": ["aged debtors", "ar", "receivables", "ageing", "aging"]},
    "/reports/aged-payables/": {"desc": "Outstanding supplier invoices by age.", "keywords": ["aged creditors", "ap", "payables", "ageing", "aging"]},
    "/sales/reports/profitability/": {"desc": "Margin and profitability analysis.", "keywords": ["profitability", "margin", "gross margin"]},
    "/reports/consolidated/": {"desc": "Consolidated group reporting.", "keywords": ["consolidated", "group", "consolidation"]},
    "/intercompany/": {"desc": "Inter-company transactions.", "keywords": ["intercompany", "inter-company", "ic"]},
    # Administration
    "/onboarding/": {"desc": "Guided setup and onboarding.", "keywords": ["setup", "onboarding", "getting started", "wizard"]},
    "/settings/tenant/": {"desc": "Company profile and settings.", "keywords": ["company profile", "settings", "tenant", "organisation"]},
    "/settings/group/": {"desc": "Company group structure.", "keywords": ["company group", "group"]},
    "/users/": {"desc": "Users and role assignment.", "keywords": ["users", "roles", "team", "members"]},
    "/team/permissions/": {"desc": "Roles and permissions matrix.", "keywords": ["permissions", "roles", "rbac", "access"]},
    "/access-requests/": {"desc": "Pending access requests.", "keywords": ["access request", "approval"]},
    "/departments/": {"desc": "Departments / cost centres.", "keywords": ["department", "cost centre", "cost center"]},
    "/audit/": {"desc": "Audit log of changes.", "keywords": ["audit", "audit log", "history", "trail"]},
    "/email-log/": {"desc": "Sent email log.", "keywords": ["email log", "emails", "sent"]},
    "/dashboard/": {"desc": "Your role's overview dashboard.", "keywords": ["dashboard", "home", "overview"]},
}


def search_groups(role):
    """[(section_title, [entry, ...])] visible to `role`, where each entry is a
    dict {label, url, icon, description, keywords}. Permission-aware (derived
    from `sidebar_for_role`); drives the hamburger menu and menu filter."""
    groups = []
    for title, items in sidebar_for_role(role):
        entries = []
        for label, url, icon in items:
            meta = NAV_META.get(url, {})
            entries.append({
                "label": label, "url": url, "icon": icon,
                "description": meta.get("desc", ""),
                "keywords": [k.lower() for k in meta.get("keywords", [])],
            })
        groups.append((title, entries))
    return groups


def search_index(role):
    """Flat, permission-filtered list of searchable pages for `role` (each entry
    also carries its `section`). The single source for global search."""
    out = []
    for title, entries in search_groups(role):
        for e in entries:
            out.append({**e, "section": title})
    return out


def search_nav(role, query, limit=12):
    """Rank `role`'s accessible pages against `query`. Matches labels, section,
    description and keyword aliases; short tokens only match keywords/word-starts
    (so "po" finds Purchase Orders, not "Reports"). Returns ranked entries."""
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [t for t in q.split() if t]
    results = []
    for e in search_index(role):
        label_l = e["label"].lower()
        section_l = e["section"].lower()
        desc_l = (e["description"] or "").lower()
        label_words = label_l.replace("/", " ").replace("&", " ").split()
        # Match whole keyword phrases AND their individual words, so a multi-word
        # alias like "purchase suggestions" is found by either token.
        kw = set()
        for k in e["keywords"]:
            kw.add(k)
            kw.update(k.split())
        blob = label_l + " " + section_l + " " + desc_l
        total, ok = 0, True
        for tok in tokens:
            s = 0
            if tok in kw:
                s += 10
            if tok == section_l or any(w.startswith(tok) for w in label_words):
                s += 4
            if len(tok) >= 3 and tok in label_l:
                s += 2
            if len(tok) >= 4 and tok in blob:
                s += 1
            if s == 0:
                ok = False
                break
            total += s
        if ok:
            results.append((total, e))
    results.sort(key=lambda x: (-x[0], x[1]["label"]))
    out = [e for _, e in results]
    return out[:limit] if limit else out
