"""CSV import for products, customers and suppliers.

Each import is upsert-by-key and per-row resilient: valid rows are saved, bad
rows are skipped and reported (with their line number) so a single bad row
never blocks the whole file. All records are tenant-scoped.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from core.models import Product, ProductBarcode, Customer, Supplier


def read_rows(uploaded_file):
    """Return (fieldnames, rows) from an uploaded CSV (utf-8, BOM tolerant)."""
    raw = uploaded_file.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = [{(k or "").strip(): (v or "") for k, v in r.items()} for r in reader]
    return reader.fieldnames or [], rows


def _summary(created, updated, errors, total):
    return {"created": created, "updated": updated, "errors": errors, "total": total}


def import_products(tenant, rows):
    created = updated = 0
    errors = []
    for n, row in enumerate(rows, start=2):  # row 1 = header
        sku = (row.get("sku") or "").strip()
        name = (row.get("name") or "").strip()
        if not sku or not name:
            errors.append((n, "sku and name are required"))
            continue
        try:
            cost = Decimal((row.get("standard_cost") or "0").strip() or "0")
        except InvalidOperation:
            errors.append((n, f"invalid standard_cost '{row.get('standard_cost')}'"))
            continue
        method = (row.get("cost_method") or "AVERAGE").strip().upper()
        if method not in dict(Product.CostMethod.choices):
            method = "AVERAGE"
        ptype = (row.get("product_type") or "STOCK").strip().upper()
        if ptype not in dict(Product.Type.choices):
            ptype = "STOCK"
        try:
            sales_price = Decimal((row.get("sales_price") or "0").strip() or "0")
        except InvalidOperation:
            sales_price = Decimal("0")
        defaults = {"name": name, "uom": (row.get("uom") or "each").strip() or "each",
                    "cost_method": method, "standard_cost": cost, "product_type": ptype,
                    "brand": (row.get("brand") or "").strip() or None,
                    "description": (row.get("description") or "").strip() or None,
                    "sales_price": sales_price}
        active = (row.get("is_active") or "").strip().lower()
        if active in ("0", "false", "no", "inactive"):
            defaults["is_active"] = False
        # Resolve / create category (and optional "Parent / Child" subcategory).
        cat_name = (row.get("category") or "").strip()
        if cat_name:
            from core.models import ProductCategory
            if "/" in cat_name:
                pname, _, cname = cat_name.partition("/")
                parent, _ = ProductCategory.objects.get_or_create(tenant=tenant, name=pname.strip(), parent=None)
                cat, _ = ProductCategory.objects.get_or_create(tenant=tenant, name=cname.strip(), parent=parent)
            else:
                cat, _ = ProductCategory.objects.get_or_create(tenant=tenant, name=cat_name, parent=None)
            defaults["category"] = cat
        obj, was_created = Product.objects.update_or_create(tenant=tenant, sku=sku, defaults=defaults)
        barcode = (row.get("barcode") or "").strip()
        if barcode:
            ProductBarcode.objects.get_or_create(tenant=tenant, code=barcode, defaults={"product": obj})
        created += was_created
        updated += (0 if was_created else 1)
    return _summary(created, updated, errors, len(rows))


def import_customers(tenant, rows):
    created = updated = 0
    errors = []
    for n, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            errors.append((n, "name is required"))
            continue
        ctype = (row.get("customer_type") or "COMPANY").strip().upper()
        if ctype not in dict(Customer.Type.choices):
            ctype = "COMPANY"
        terms = (row.get("payment_terms_days") or "").strip()
        defaults = {
            "customer_type": ctype,
            "contact_person": (row.get("contact_person") or "").strip() or None,
            "email": (row.get("email") or "").strip() or None,
            "phone": (row.get("phone") or "").strip() or None,
            "vat_number": (row.get("vat_number") or "").strip() or None,
            "company_number": (row.get("company_number") or "").strip() or None,
            "billing_address": (row.get("billing_address") or "").strip() or None,
            "shipping_address": (row.get("shipping_address") or "").strip() or None,
            "tags": (row.get("tags") or "").strip() or None,
        }
        if terms.isdigit():
            defaults["payment_terms_days"] = int(terms)
        _, was_created = Customer.objects.update_or_create(tenant=tenant, name=name, defaults=defaults)
        created += was_created
        updated += (0 if was_created else 1)
    return _summary(created, updated, errors, len(rows))


def import_suppliers(tenant, rows):
    created = updated = 0
    errors = []
    for n, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            errors.append((n, "name is required"))
            continue
        terms = (row.get("payment_terms_days") or "").strip()
        defaults = {
            "contact_person": (row.get("contact_person") or "").strip() or None,
            "email": (row.get("email") or "").strip() or None,
            "phone": (row.get("phone") or "").strip() or None,
            "vat_number": (row.get("vat_number") or "").strip() or None,
            "company_number": (row.get("company_number") or "").strip() or None,
            "address": (row.get("address") or "").strip() or None,
            "currency_code": (row.get("currency_code") or "GBP").strip() or "GBP",
            "categories": (row.get("categories") or "").strip() or None,
        }
        if terms.isdigit():
            defaults["payment_terms_days"] = int(terms)
        _, was_created = Supplier.objects.update_or_create(tenant=tenant, name=name, defaults=defaults)
        created += was_created
        updated += (0 if was_created else 1)
    return _summary(created, updated, errors, len(rows))


def export_rows(tenant, kind):
    """Return (columns, [row-lists]) for a tenant's records in `kind`.

    Mirrors the import column order so an export can be re-imported as-is.
    """
    cfg = CONFIG.get(kind)
    if not cfg:
        return [], []
    cols = cfg["columns"]
    out = []
    if kind == "products":
        for p in Product.objects.filter(tenant=tenant).select_related("category").order_by("sku"):
            barcode = ProductBarcode.objects.filter(tenant=tenant, product=p).values_list("code", flat=True).first()
            out.append([p.sku, p.name, p.product_type, str(p.category) if p.category_id else "",
                        p.brand or "", p.description or "", p.uom, p.cost_method, p.standard_cost,
                        p.sales_price, "yes" if p.is_active else "no", barcode or ""])
    elif kind == "customers":
        for c in Customer.objects.filter(tenant=tenant).order_by("name"):
            out.append([c.name, c.customer_type, c.contact_person or "", c.email or "", c.phone or "",
                        c.vat_number or "", c.company_number or "", c.billing_address or "",
                        c.shipping_address or "", (c.payment_terms_days if c.payment_terms_days is not None else ""),
                        c.tags or ""])
    elif kind == "suppliers":
        for s in Supplier.objects.filter(tenant=tenant).order_by("name"):
            out.append([s.name, s.contact_person or "", s.email or "", s.phone or "",
                        s.vat_number or "", s.company_number or "", s.address or "",
                        s.currency_code or "GBP",
                        (s.payment_terms_days if s.payment_terms_days is not None else ""),
                        s.categories or ""])
    return cols, out


CONFIG = {
    "products":  {"label": "Products",  "key": "sku",
                  "columns": ["sku", "name", "product_type", "category", "brand", "description",
                              "uom", "cost_method", "standard_cost", "sales_price", "is_active", "barcode"],
                  "sample": ["SKU-100", "Sample Widget", "STOCK", "Electronics / Webcams", "Acme",
                             "1080p HD webcam", "each", "AVERAGE", "9.99", "19.99", "yes", "5012345678900"],
                  "fn": import_products, "list_url": "/products/"},
    "customers": {"label": "Customers", "key": "name",
                  "columns": ["name", "customer_type", "contact_person", "email", "phone",
                              "vat_number", "company_number", "billing_address", "shipping_address",
                              "payment_terms_days", "tags"],
                  "sample": ["Bright Retail Ltd", "COMPANY", "Jane Doe", "ap@bright.example",
                             "+44 20 7946 0000", "GB987654321", "12345678", "10 High St, Manchester",
                             "Unit 5, Trade Park, Manchester", "30", "VIP, Reseller"],
                  "fn": import_customers, "list_url": "/customers/"},
    "suppliers": {"label": "Suppliers", "key": "name",
                  "columns": ["name", "contact_person", "email", "phone", "vat_number",
                              "company_number", "address", "currency_code", "payment_terms_days", "categories"],
                  "sample": ["Globex Supplies", "Pat Lee", "sales@globex.example", "+44 161 555 0100",
                             "GB444555666", "11223344", "Globex House, Salford", "GBP", "30", "Raw materials"],
                  "fn": import_suppliers, "list_url": "/suppliers/"},
}
