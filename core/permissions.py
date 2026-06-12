"""Permission catalog and the role -> permission matrix.

This is the declarative source of truth for what each organisation role can do.
Module-level RBAC is still enforced by core.auth.role_required (per active org);
this catalog provides a named-permission layer for fine-grained checks, the
admin "Roles & Permissions" matrix, and conditional UI.
"""
from core import roles

# --- Permission codes ---
VIEW_DASHBOARD = "view_dashboard"
MANAGE_COMPANY_SETTINGS = "manage_company_settings"
MANAGE_USERS = "manage_users"
MANAGE_CUSTOMERS = "manage_customers"
MANAGE_SUPPLIERS = "manage_suppliers"
MANAGE_PRODUCTS = "manage_products"
MANAGE_INVENTORY = "manage_inventory"
MANAGE_PURCHASE_ORDERS = "manage_purchase_orders"
MANAGE_INVOICES = "manage_invoices"
MANAGE_PAYMENTS = "manage_payments"
VIEW_FINANCE_REPORTS = "view_finance_reports"
EXPORT_DATA = "export_data"
DELETE_RECORDS = "delete_records"
APPROVE_TRANSACTIONS = "approve_transactions"
VIEW_FIELD_TECHNICAL_METADATA = "can_view_field_technical_metadata"

# Catalog: (code, label, category) - drives the matrix UI.
PERMISSIONS = [
    (VIEW_DASHBOARD, "View dashboard", "General"),
    (MANAGE_COMPANY_SETTINGS, "Manage company settings", "Administration"),
    (MANAGE_USERS, "Manage users", "Administration"),
    (MANAGE_CUSTOMERS, "Manage customers", "Sales"),
    (MANAGE_SUPPLIERS, "Manage suppliers", "Procurement"),
    (MANAGE_PRODUCTS, "Manage products", "Inventory"),
    (MANAGE_INVENTORY, "Manage inventory", "Inventory"),
    (MANAGE_PURCHASE_ORDERS, "Manage purchase orders", "Procurement"),
    (MANAGE_INVOICES, "Manage invoices", "Finance"),
    (MANAGE_PAYMENTS, "Manage payments", "Finance"),
    (VIEW_FINANCE_REPORTS, "View finance reports", "Finance"),
    (EXPORT_DATA, "Export data", "Data"),
    (DELETE_RECORDS, "Delete records", "Data"),
    (APPROVE_TRANSACTIONS, "Approve transactions", "Workflow"),
    (VIEW_FIELD_TECHNICAL_METADATA, "View field technical (database) metadata", "Administration"),
]
ALL_PERMISSIONS = {code for code, _, _ in PERMISSIONS}
PERMISSION_LABELS = {code: label for code, label, _ in PERMISSIONS}

# --- Role -> permissions matrix ---
ROLE_PERMISSIONS = {
    roles.ADMIN: set(ALL_PERMISSIONS),
    roles.ACCOUNTANT: {
        VIEW_DASHBOARD, MANAGE_CUSTOMERS, MANAGE_SUPPLIERS, MANAGE_INVOICES,
        MANAGE_PAYMENTS, VIEW_FINANCE_REPORTS, EXPORT_DATA, APPROVE_TRANSACTIONS,
    },
    roles.MANAGER: {
        VIEW_DASHBOARD, MANAGE_CUSTOMERS, MANAGE_SUPPLIERS, MANAGE_PRODUCTS,
        MANAGE_INVENTORY, MANAGE_PURCHASE_ORDERS, VIEW_FINANCE_REPORTS,
        APPROVE_TRANSACTIONS, EXPORT_DATA,
    },
    roles.SALES: {VIEW_DASHBOARD, MANAGE_CUSTOMERS, MANAGE_INVOICES},
    roles.WAREHOUSE: {VIEW_DASHBOARD, MANAGE_INVENTORY, MANAGE_PRODUCTS},
    roles.PURCHASING: {VIEW_DASHBOARD, MANAGE_PURCHASE_ORDERS, MANAGE_SUPPLIERS, MANAGE_INVENTORY},
    roles.FINANCE: {
        VIEW_DASHBOARD, MANAGE_CUSTOMERS, MANAGE_INVOICES, MANAGE_PAYMENTS,
        VIEW_FINANCE_REPORTS, EXPORT_DATA,
    },
    roles.READONLY: {VIEW_DASHBOARD, VIEW_FINANCE_REPORTS},
}


def role_permissions(role):
    return ROLE_PERMISSIONS.get(role, set())


def role_has_permission(role, perm):
    return role == roles.ADMIN or perm in role_permissions(role)


# --- Per-user overrides on top of the role baseline ---
GRANT = "GRANT"
REVOKE = "REVOKE"


def effective_permissions(role, overrides=None):
    """Resolve a user's permissions: the role baseline with per-user overrides
    applied. `overrides` is a {permission_code: 'GRANT'|'REVOKE'} mapping.
    Owners/Admins always have the full set (overrides do not apply)."""
    if role == roles.ADMIN:
        return set(ALL_PERMISSIONS)
    perms = set(role_permissions(role))
    for perm, effect in (overrides or {}).items():
        if effect == GRANT:
            perms.add(perm)
        elif effect == REVOKE:
            perms.discard(perm)
    return perms & ALL_PERMISSIONS
