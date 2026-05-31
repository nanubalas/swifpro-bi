"""Per-role dashboard context builders.

Each builder returns a uniform dict rendered by templates/dashboards/generic.html:
    {title, stats:[...], actions:[...], alerts:[...], panels:[...]}
so all role dashboards share one consistent, responsive layout.
Figures come from the reports services + ORM, scoped to the active tenant.
"""
from decimal import Decimal

from django.utils import timezone

from core.services import reports
from core.models import (
    PurchaseOrder, Shipment, SalesOrder, SalesOrderLine, CustomerInvoice,
    SupplierInvoice, InventoryBalance, Product, Payment, Supplier, Customer,
    GoodsReceipt, InventoryTransfer, InventoryMovement, AuditLog,
)

ZERO = Decimal("0.00")

OPEN_PO = [
    PurchaseOrder.Status.SUBMITTED, PurchaseOrder.Status.APPROVAL_PENDING,
    PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.SENT,
    PurchaseOrder.Status.IN_TRANSIT, PurchaseOrder.Status.PARTIALLY_RECEIVED,
]


def _stat(label, value, icon, money=True):
    return {"label": label, "value": value, "icon": icon, "money": money}


def _fin(tenant):
    balances = reports.account_balances(tenant)
    by_code = {a.code: v["balance"] for a, v in balances.items()}
    pnl = reports.profit_and_loss(tenant)
    return {
        "cash": by_code.get("1050", ZERO),
        "inventory_value": reports.stock_valuation(tenant)["total"],
        "receivables": reports.aged_receivables(tenant)["total"],
        "payables": reports.aged_payables(tenant)["total"],
        "net_profit": pnl["net_profit"],
        "revenue": pnl["income_total"],
        "vat_estimate": by_code.get("2200", ZERO) - by_code.get("1300", ZERO),
    }


def _po_rows(tenant, limit=6):
    rows = []
    for po in PurchaseOrder.objects.filter(tenant=tenant).select_related("supplier").order_by("-created_at")[:limit]:
        rows.append([
            {"text": po.po_number, "url": f"/po/{po.id}/"},
            {"text": po.supplier.name},
            {"text": po.get_status_display(), "badge": "secondary"},
            {"text": po.created_at.strftime("%d %b %Y"), "align": "end", "muted": True},
        ])
    return rows


def _payment_rows(tenant, limit=6):
    rows = []
    for p in Payment.objects.filter(tenant=tenant).select_related("customer", "supplier").order_by("-created_at")[:limit]:
        rows.append([
            {"text": ("In " if p.direction == "RECEIPT" else "Out "), "badge": ("success" if p.direction == "RECEIPT" else "warning")},
            {"text": p.party_name, "url": f"/payments/{p.id}/"},
            {"text": f"{p.amount}", "align": "end"},
        ])
    return rows


def _low_stock_rows(tenant, limit=8):
    rows = []
    qs = InventoryBalance.objects.filter(tenant=tenant, on_hand__lte=ZERO).select_related("product", "location")[:limit]
    for b in qs:
        rows.append([
            {"text": b.product.sku, "url": f"/inventory/"},
            {"text": b.product.name},
            {"text": b.location.name, "muted": True},
            {"text": f"{b.on_hand}", "align": "end", "badge": "danger"},
        ])
    return rows


def _alerts(tenant):
    today = timezone.localdate()
    out = []
    awaiting = PurchaseOrder.objects.filter(tenant=tenant, status=PurchaseOrder.Status.APPROVAL_PENDING).count()
    overdue = CustomerInvoice.objects.filter(tenant=tenant, status=CustomerInvoice.Status.ISSUED, due_date__lt=today).count()
    oos = InventoryBalance.objects.filter(tenant=tenant, on_hand__lte=ZERO).count()
    if awaiting:
        out.append({"text": f"{awaiting} PO(s) awaiting approval", "level": "warning", "icon": "patch-question", "url": "/po/"})
    if overdue:
        out.append({"text": f"{overdue} overdue customer invoice(s)", "level": "danger", "icon": "exclamation-octagon", "url": "/reports/aged-receivables/"})
    if oos:
        out.append({"text": f"{oos} stock line(s) at/below zero", "level": "secondary", "icon": "box", "url": "/inventory/"})
    return out


# --------------------------------------------------------------------------

def build_admin(tenant, request):
    f = _fin(tenant)
    activity = [[
        {"text": a.created_at.strftime("%d %b %H:%M"), "muted": True},
        {"text": a.action, "badge": "secondary"},
        {"text": a.username or "-"},
    ] for a in AuditLog.objects.filter(tenant=tenant)[:8]]
    return {
        "title": "Business Overview",
        "stats": [
            _stat("Cash at bank", f["cash"], "cash-stack"),
            _stat("Revenue", f["revenue"], "graph-up-arrow"),
            _stat("Net profit", f["net_profit"], "wallet2"),
            _stat("Inventory value", f["inventory_value"], "boxes"),
            _stat("Receivables", f["receivables"], "arrow-down-left-circle"),
            _stat("Payables", f["payables"], "arrow-up-right-circle"),
            _stat("VAT estimate", f["vat_estimate"], "percent"),
            _stat("Products", Product.objects.filter(tenant=tenant).count(), "box-seam", money=False),
        ],
        "actions": [
            {"label": "Create invoice", "url": "/ar/invoices/new/", "icon": "receipt"},
            {"label": "Add product", "url": "/products/new/", "icon": "box-seam"},
            {"label": "Add customer", "url": "/customers/new/", "icon": "person-plus"},
            {"label": "Create purchase order", "url": "/po/new/", "icon": "file-earmark-plus"},
            {"label": "Invite user", "url": "/admin/auth/user/add/", "icon": "person-badge"},
            {"label": "View reports", "url": "/reports/", "icon": "bar-chart-line"},
        ],
        "alerts": _alerts(tenant),
        "panels": [
            {"title": "Recent purchase orders", "icon": "inboxes", "url": "/po/", "rows": _po_rows(tenant)},
            {"title": "User activity", "icon": "people", "url": "/audit/", "rows": activity, "empty": "No recent activity."},
        ],
    }


def build_accountant(tenant, request):
    f = _fin(tenant)
    review = [[
        {"text": i.invoice_number, "url": f"/invoices/{i.id}/"},
        {"text": i.supplier.name},
        {"text": i.get_status_display(), "badge": "warning"},
    ] for i in SupplierInvoice.objects.filter(tenant=tenant, status=SupplierInvoice.Status.DRAFT).select_related("supplier")[:8]]
    return {
        "title": "Accounting Dashboard",
        "stats": [
            _stat("Receivables", f["receivables"], "arrow-down-left-circle"),
            _stat("Payables", f["payables"], "arrow-up-right-circle"),
            _stat("Net profit", f["net_profit"], "graph-up-arrow"),
            _stat("VAT estimate", f["vat_estimate"], "percent"),
        ],
        "actions": [
            {"label": "VAT return", "url": "/vat/", "icon": "file-earmark-spreadsheet"},
            {"label": "Profit & Loss", "url": "/reports/profit-and-loss/", "icon": "graph-up-arrow"},
            {"label": "Balance Sheet", "url": "/reports/balance-sheet/", "icon": "bank2"},
            {"label": "Review unpaid invoices", "url": "/reports/aged-receivables/", "icon": "cash-coin"},
            {"label": "Review supplier bills", "url": "/invoices/", "icon": "receipt-cutoff"},
            {"label": "All reports", "url": "/reports/", "icon": "bar-chart-line"},
        ],
        "alerts": _alerts(tenant),
        "panels": [
            {"title": "Bills needing review", "icon": "exclamation-circle", "url": "/invoices/", "rows": review, "empty": "Nothing to review."},
            {"title": "Recent payments", "icon": "cash-stack", "url": "/payments/", "rows": _payment_rows(tenant)},
        ],
    }


def build_manager(tenant, request):
    f = _fin(tenant)
    so_rows = [[
        {"text": o.order_number, "url": f"/sales-orders/{o.id}/"},
        {"text": o.get_status_display(), "badge": "secondary"},
        {"text": o.order_date.strftime("%d %b %Y"), "align": "end", "muted": True},
    ] for o in SalesOrder.objects.filter(tenant=tenant).order_by("-order_date")[:6]]
    moves = [[
        {"text": m.created_at.strftime("%d %b %H:%M"), "muted": True},
        {"text": m.product.sku},
        {"text": m.get_movement_type_display(), "badge": "secondary"},
        {"text": f"{m.qty_delta}", "align": "end"},
    ] for m in InventoryMovement.objects.filter(tenant=tenant).select_related("product").order_by("-created_at")[:8]]
    return {
        "title": "Operations Dashboard",
        "stats": [
            _stat("Revenue", f["revenue"], "graph-up-arrow"),
            _stat("Open POs", PurchaseOrder.objects.filter(tenant=tenant, status__in=OPEN_PO).count(), "file-earmark-text", money=False),
            _stat("Goods in transit", Shipment.objects.filter(tenant=tenant, status__in=[Shipment.Status.IN_TRANSIT, Shipment.Status.PICKED_UP]).count(), "truck", money=False),
            _stat("Sales orders", SalesOrder.objects.filter(tenant=tenant).count(), "cart-check", money=False),
            _stat("Inventory value", f["inventory_value"], "boxes"),
        ],
        "actions": [
            {"label": "Create sales order", "url": "/sales-orders/new/", "icon": "cart-plus"},
            {"label": "Create purchase order", "url": "/po/new/", "icon": "file-earmark-plus"},
            {"label": "Receive stock", "url": "/po/", "icon": "box-arrow-in-down"},
            {"label": "Add customer", "url": "/customers/new/", "icon": "person-plus"},
            {"label": "Add supplier", "url": "/suppliers/new/", "icon": "shop"},
            {"label": "View inventory", "url": "/inventory/", "icon": "boxes"},
        ],
        "alerts": _alerts(tenant),
        "panels": [
            {"title": "Customer orders", "icon": "cart-check", "url": "/sales-orders/", "rows": so_rows, "empty": "No sales orders."},
            {"title": "Recent stock movements", "icon": "arrow-left-right", "url": "/inventory/", "rows": moves, "empty": "No movements."},
        ],
    }


def build_sales(tenant, request):
    so_rows = [[
        {"text": o.order_number, "url": f"/sales-orders/{o.id}/"},
        {"text": o.get_status_display(), "badge": "secondary"},
        {"text": o.order_date.strftime("%d %b %Y"), "align": "end", "muted": True},
    ] for o in SalesOrder.objects.filter(tenant=tenant).order_by("-order_date")[:8]]
    inv_rows = [[
        {"text": i.invoice_number, "url": f"/ar/invoices/{i.id}/"},
        {"text": i.customer.name},
        {"text": i.get_status_display(), "badge": "secondary"},
    ] for i in CustomerInvoice.objects.filter(tenant=tenant).select_related("customer").order_by("-invoice_date")[:8]]
    return {
        "title": "Sales Dashboard",
        "stats": [
            _stat("Customers", Customer.objects.filter(tenant=tenant).count(), "people", money=False),
            _stat("Sales orders", SalesOrder.objects.filter(tenant=tenant).count(), "cart-check", money=False),
            _stat("Open invoices", CustomerInvoice.objects.filter(tenant=tenant, status=CustomerInvoice.Status.ISSUED).count(), "receipt", money=False),
            _stat("Products", Product.objects.filter(tenant=tenant).count(), "box-seam", money=False),
        ],
        "actions": [
            {"label": "Create sales order", "url": "/sales-orders/new/", "icon": "cart-plus"},
            {"label": "Create invoice", "url": "/ar/invoices/new/", "icon": "receipt"},
            {"label": "Add customer", "url": "/customers/new/", "icon": "person-plus"},
            {"label": "Search products", "url": "/products/", "icon": "search"},
            {"label": "Customers", "url": "/customers/", "icon": "people"},
        ],
        "alerts": [],
        "panels": [
            {"title": "Recent sales orders", "icon": "cart-check", "url": "/sales-orders/", "rows": so_rows, "empty": "No sales orders."},
            {"title": "Customer invoices", "icon": "receipt", "url": "/ar/invoices/", "rows": inv_rows, "empty": "No invoices."},
        ],
    }


def build_warehouse(tenant, request):
    ship_rows = [[
        {"text": s.po.po_number, "url": f"/shipments/{s.id}/"},
        {"text": s.destination.name},
        {"text": s.get_status_display(), "badge": "info"},
    ] for s in Shipment.objects.filter(tenant=tenant, status__in=[Shipment.Status.IN_TRANSIT, Shipment.Status.PICKED_UP, Shipment.Status.CREATED]).select_related("po", "destination")[:8]]
    grn_rows = [[
        {"text": g.grn_number, "url": f"/po/{g.po_id}/"},
        {"text": g.received_to.name},
        {"text": g.received_at.strftime("%d %b %Y"), "align": "end", "muted": True},
    ] for g in GoodsReceipt.objects.filter(tenant=tenant, status=GoodsReceipt.Status.POSTED).select_related("received_to").order_by("-received_at")[:6]]
    return {
        "title": "Warehouse Dashboard",
        "stats": [
            _stat("To receive (shipments)", Shipment.objects.filter(tenant=tenant, status__in=[Shipment.Status.IN_TRANSIT, Shipment.Status.PICKED_UP, Shipment.Status.CREATED]).count(), "truck", money=False),
            _stat("Open transfers", InventoryTransfer.objects.filter(tenant=tenant, status=InventoryTransfer.Status.DRAFT).count(), "arrow-left-right", money=False),
            _stat("Low/zero stock", InventoryBalance.objects.filter(tenant=tenant, on_hand__lte=ZERO).count(), "box", money=False),
            _stat("Stock value", reports.stock_valuation(tenant)["total"], "boxes"),
        ],
        "actions": [
            {"label": "Receive stock", "url": "/po/", "icon": "box-arrow-in-down"},
            {"label": "Transfer stock", "url": "/transfers/new/", "icon": "arrow-left-right"},
            {"label": "Cycle count", "url": "/cycle-counts/new/", "icon": "clipboard-check"},
            {"label": "Search SKU", "url": "/products/", "icon": "upc-scan"},
            {"label": "Inventory by location", "url": "/inventory/", "icon": "geo-alt"},
        ],
        "alerts": [a for a in _alerts(tenant) if a["level"] == "secondary"],
        "panels": [
            {"title": "Inbound shipments", "icon": "truck", "url": "/shipments/", "rows": ship_rows, "empty": "Nothing inbound."},
            {"title": "Low / zero stock", "icon": "box", "url": "/inventory/", "rows": _low_stock_rows(tenant), "empty": "Stock healthy."},
            {"title": "Recent goods receipts", "icon": "box-seam", "url": "/po/", "rows": grn_rows, "empty": "No receipts yet."},
        ],
    }


def build_purchasing(tenant, request):
    po_rows = [[
        {"text": po.po_number, "url": f"/po/{po.id}/"},
        {"text": po.supplier.name},
        {"text": po.get_status_display(), "badge": "secondary"},
    ] for po in PurchaseOrder.objects.filter(tenant=tenant, status__in=OPEN_PO).select_related("supplier").order_by("-created_at")[:8]]
    partial_rows = [[
        {"text": po.po_number, "url": f"/po/{po.id}/"},
        {"text": po.supplier.name},
        {"text": "Partially received", "badge": "warning"},
    ] for po in PurchaseOrder.objects.filter(tenant=tenant, status=PurchaseOrder.Status.PARTIALLY_RECEIVED).select_related("supplier")[:6]]
    return {
        "title": "Purchasing Dashboard",
        "stats": [
            _stat("Open POs", PurchaseOrder.objects.filter(tenant=tenant, status__in=OPEN_PO).count(), "file-earmark-text", money=False),
            _stat("Awaiting approval", PurchaseOrder.objects.filter(tenant=tenant, status=PurchaseOrder.Status.APPROVAL_PENDING).count(), "patch-question", money=False),
            _stat("Goods in transit", Shipment.objects.filter(tenant=tenant, status__in=[Shipment.Status.IN_TRANSIT, Shipment.Status.PICKED_UP]).count(), "truck", money=False),
            _stat("Suppliers", Supplier.objects.filter(tenant=tenant).count(), "shop", money=False),
        ],
        "actions": [
            {"label": "Create purchase order", "url": "/po/new/", "icon": "file-earmark-plus"},
            {"label": "Suppliers", "url": "/suppliers/", "icon": "shop"},
            {"label": "Receive goods", "url": "/po/", "icon": "box-arrow-in-down"},
            {"label": "Low stock (reorder)", "url": "/reports/stock-valuation/", "icon": "arrow-repeat"},
        ],
        "alerts": [a for a in _alerts(tenant) if a["level"] in ("warning", "secondary")],
        "panels": [
            {"title": "Open purchase orders", "icon": "file-earmark-text", "url": "/po/", "rows": po_rows, "empty": "No open POs."},
            {"title": "Partially received", "icon": "hourglass-split", "url": "/po/", "rows": partial_rows, "empty": "None."},
            {"title": "Products needing reorder", "icon": "arrow-repeat", "url": "/inventory/", "rows": _low_stock_rows(tenant), "empty": "Stock healthy."},
        ],
    }


def build_finance(tenant, request):
    f = _fin(tenant)
    unpaid = [[
        {"text": i.invoice_number, "url": f"/ar/invoices/{i.id}/"},
        {"text": i.customer.name},
        {"text": f"{i.outstanding}", "align": "end", "badge": "warning"},
    ] for i in CustomerInvoice.objects.filter(tenant=tenant, status=CustomerInvoice.Status.ISSUED).select_related("customer").prefetch_related("lines", "payment_allocations")[:8]]
    unrec = [[
        {"text": p.party_name, "url": f"/payments/{p.id}/"},
        {"text": ("Receipt" if p.direction == "RECEIPT" else "Payment")},
        {"text": f"{p.amount}", "align": "end"},
    ] for p in Payment.objects.filter(tenant=tenant, status=Payment.Status.POSTED, is_reconciled=False).select_related("customer", "supplier")[:8]]
    return {
        "title": "Finance Dashboard",
        "stats": [
            _stat("Cash at bank", f["cash"], "cash-stack"),
            _stat("Receivables", f["receivables"], "arrow-down-left-circle"),
            _stat("Payables", f["payables"], "arrow-up-right-circle"),
            _stat("Unreconciled items", Payment.objects.filter(tenant=tenant, status=Payment.Status.POSTED, is_reconciled=False).count(), "check2-square", money=False),
        ],
        "actions": [
            {"label": "Record receipt", "url": "/payments/receipts/new/", "icon": "arrow-down-left"},
            {"label": "Pay supplier", "url": "/payments/payments/new/", "icon": "arrow-up-right"},
            {"label": "Bank reconciliation", "url": "/bank/reconcile/", "icon": "check2-square"},
            {"label": "Review unpaid invoices", "url": "/reports/aged-receivables/", "icon": "cash-coin"},
            {"label": "Supplier bills", "url": "/invoices/", "icon": "receipt-cutoff"},
        ],
        "alerts": _alerts(tenant),
        "panels": [
            {"title": "Unpaid customer invoices", "icon": "cash-coin", "url": "/ar/invoices/", "rows": unpaid, "empty": "All paid."},
            {"title": "Items to reconcile", "icon": "check2-square", "url": "/bank/reconcile/", "rows": unrec, "empty": "All reconciled."},
        ],
    }


def build_readonly(tenant, request):
    f = _fin(tenant)
    return {
        "title": "Reporting Dashboard",
        "stats": [
            _stat("Revenue", f["revenue"], "graph-up-arrow"),
            _stat("Net profit", f["net_profit"], "wallet2"),
            _stat("Inventory value", f["inventory_value"], "boxes"),
            _stat("Customers", Customer.objects.filter(tenant=tenant).count(), "people", money=False),
            _stat("Suppliers", Supplier.objects.filter(tenant=tenant).count(), "shop", money=False),
            _stat("Products", Product.objects.filter(tenant=tenant).count(), "box-seam", money=False),
        ],
        "actions": [
            {"label": "All reports", "url": "/reports/", "icon": "bar-chart-line"},
            {"label": "Profit & Loss", "url": "/reports/profit-and-loss/", "icon": "graph-up-arrow"},
            {"label": "Stock valuation", "url": "/reports/stock-valuation/", "icon": "box-seam"},
            {"label": "Inventory", "url": "/inventory/", "icon": "boxes"},
        ],
        "alerts": [],
        "panels": [
            {"title": "Recent purchase orders", "icon": "inboxes", "url": "/po/", "rows": _po_rows(tenant)},
        ],
    }


BUILDERS = {
    "ADMIN": build_admin,
    "ACCOUNTANT": build_accountant,
    "MANAGER": build_manager,
    "SALES": build_sales,
    "WAREHOUSE": build_warehouse,
    "PURCHASING": build_purchasing,
    "FINANCE": build_finance,
    "READONLY": build_readonly,
}
