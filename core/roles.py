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
        ("Sales Orders", "/sales-orders/", "cart-check", {ADMIN, MANAGER, SALES}),
        ("Customers", "/customers/", "people", {ADMIN, MANAGER, SALES, ACCOUNTANT, FINANCE}),
        ("Customer Invoices", "/ar/invoices/", "receipt", {ADMIN, MANAGER, SALES, ACCOUNTANT, FINANCE}),
        ("Returns (RMA)", "/returns/", "arrow-return-left", {ADMIN, MANAGER, SALES, WAREHOUSE}),
    ]),
    ("Procurement", [
        ("Purchase Orders", "/po/", "file-earmark-text", {ADMIN, MANAGER, PURCHASING}),
        ("Suppliers", "/suppliers/", "shop", {ADMIN, MANAGER, PURCHASING, ACCOUNTANT, FINANCE}),
        ("Shipments", "/shipments/", "truck", {ADMIN, MANAGER, PURCHASING, WAREHOUSE}),
        ("Supplier Invoices", "/invoices/", "receipt-cutoff", {ADMIN, ACCOUNTANT, FINANCE}),
    ]),
    ("Inventory", [
        ("Inventory", "/inventory/", "boxes", {ADMIN, MANAGER, WAREHOUSE, PURCHASING}),
        ("Transfers", "/transfers/", "arrow-left-right", {ADMIN, MANAGER, WAREHOUSE}),
        ("Cycle Counts", "/cycle-counts/", "clipboard-check", {ADMIN, MANAGER, WAREHOUSE}),
        ("Locations", "/locations/", "geo-alt", {ADMIN, MANAGER, WAREHOUSE}),
        ("Products", "/products/", "box-seam", {ADMIN, MANAGER, WAREHOUSE, PURCHASING, SALES}),
        ("BOMs / Kits", "/boms/", "diagram-3", {ADMIN, MANAGER, PURCHASING}),
    ]),
    ("Finance", [
        ("Payments", "/payments/", "cash-stack", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Bank Reconciliation", "/bank/reconcile/", "check2-square", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Tax Codes (VAT)", "/tax-codes/", "percent", {ADMIN, ACCOUNTANT, FINANCE}),
        ("VAT Returns (MTD)", "/vat/", "file-earmark-spreadsheet", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Journal", "/gl/journal/", "journal-text", {ADMIN, ACCOUNTANT, FINANCE}),
        ("Chart of Accounts", "/gl/accounts/", "bank", {ADMIN, ACCOUNTANT, FINANCE}),
    ]),
    ("Reports", [
        ("All Reports", "/reports/", "bar-chart-line", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Profit & Loss", "/reports/profit-and-loss/", "graph-up-arrow", {ADMIN, ACCOUNTANT, READONLY}),
        ("Balance Sheet", "/reports/balance-sheet/", "bank2", {ADMIN, ACCOUNTANT, READONLY}),
        ("Stock Valuation", "/reports/stock-valuation/", "box-seam", {ADMIN, ACCOUNTANT, MANAGER, WAREHOUSE, PURCHASING, READONLY}),
        ("Aged Debtors", "/reports/aged-receivables/", "cash-coin", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
        ("Aged Creditors", "/reports/aged-payables/", "credit-card", {ADMIN, ACCOUNTANT, FINANCE, READONLY}),
    ]),
    ("Administration", [
        ("Setup &amp; Onboarding", "/onboarding/", "rocket-takeoff", {ADMIN}),
        ("Company Profile", "/settings/tenant/", "building-gear", {ADMIN}),
        ("Users & Roles", "/admin/", "people-fill", {ADMIN}),
        ("Access Requests", "/access-requests/", "person-plus", {ADMIN}),
        ("Audit Log", "/audit/", "shield-lock", {ADMIN}),
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
