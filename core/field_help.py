"""Reusable field-help + field-metadata system.

Every authenticated user can see a plain-business explanation of a form field.
Technical database metadata (table/column/type/FK/on_delete...) is derived from
Django's own model introspection (`_meta`) - never hardcoded - and is shown only
to users allowed to see it (superusers, app ADMIN, or anyone granted the
`can_view_field_technical_metadata` permission).

This module is pure read-only introspection + a small text registry. It changes
no business logic, models or schema.
"""
from django.db.models.fields import NOT_PROVIDED

from core import permissions


# --- Sensitive fields: never expose secret-like values or risky metadata ------
SENSITIVE_TOKENS = (
    "password", "passwd", "token", "secret", "api_key", "apikey",
    "access_token", "refresh_token", "private_key", "client_secret",
)


def is_sensitive(field_name):
    n = (field_name or "").lower()
    return any(tok in n for tok in SENSITIVE_TOKENS)


# --- Business-help registry ---------------------------------------------------
# Keyed by "ModelName.field_name". Every key is optional: when a field is missing
# here we fall back to the model field's help_text / verbose_name, so the system
# works broadly without perfecting every field. Keep entries plain-English and
# business-focused (no database jargon).
FIELD_HELP = {
    # Product
    "Product.sku": {"desc": "Unique product code used across inventory, sales, purchasing and reporting.",
                    "expected": "Short alphanumeric code, unique within your company.", "example": "SKU-001"},
    "Product.name": {"desc": "The product's display name as it appears on documents and lists.",
                     "example": "A4 Copier Paper 80gsm"},
    "Product.barcode": {"desc": "Scannable barcode (EAN/UPC) for the product, if any.",
                        "expected": "8-13 digit barcode.", "example": "5012345678900"},
    "Product.sales_price": {"desc": "Default selling price before VAT.", "expected": "A positive amount.", "example": "12.50"},
    "Product.cost_method": {"desc": "How this product's stock is valued: moving Average, FIFO layers, or a fixed Standard cost."},
    "Product.standard_cost": {"desc": "Fixed unit cost used when the cost method is Standard; the basis for purchase price variance.", "example": "7.20"},
    "Product.reorder_level": {"desc": "Stock level at which the product is flagged as low and suggested for reorder.", "example": "10"},
    "Product.tax_code": {"desc": "The VAT rate applied when this product is sold."},
    "Product.category": {"desc": "Product category used for grouping, reporting and filtering."},
    "Product.preferred_supplier": {"desc": "Default supplier suggested when raising purchase orders for this product."},
    "Product.is_active": {"desc": "Inactive products are hidden from new transactions but kept for history."},
    # Customer
    "Customer.name": {"desc": "Customer's business or trading name.", "example": "Acme Retail Ltd"},
    "Customer.email": {"desc": "Primary contact email for invoices and correspondence.", "example": "accounts@acme.co.uk"},
    "Customer.credit_limit": {"desc": "Maximum outstanding balance allowed for this customer.", "example": "5000.00"},
    "Customer.payment_terms_days": {"desc": "Days the customer has to pay an issued invoice.", "example": "30"},
    # Supplier
    "Supplier.name": {"desc": "Supplier's business or trading name.", "example": "Global Parts Co"},
    "Supplier.email": {"desc": "Primary contact email for purchase orders and remittances."},
    # GL account
    "GLAccount.code": {"desc": "Nominal account code in your chart of accounts.", "expected": "Numeric account code.", "example": "1000"},
    "GLAccount.name": {"desc": "Human-readable name of the nominal account.", "example": "Inventory"},
    "GLAccount.type": {"desc": "Accounting classification (Asset, Liability, Equity, Income, Expense) that drives the financial statements."},
    # Tax code
    "TaxCode.code": {"desc": "Short VAT code referenced on products and lines.", "example": "STD"},
    "TaxCode.rate": {"desc": "VAT rate this code applies, stored as a decimal fraction.", "expected": "Decimal fraction, e.g. 0.20 for 20%.", "example": "0.20"},
    # Expense
    "Expense.amount": {"desc": "Gross amount of the expense.", "example": "48.00"},
    "Expense.category": {"desc": "What the spend was for - drives the nominal account it posts to."},
    "Expense.tax_code": {"desc": "VAT treatment of the expense, used to split net and VAT."},
}


def _model_and_field(bound_field):
    """Return (model class, model field) for a bound form field, or (model, None)
    for a form-only field, or (None, None) when the form isn't a ModelForm."""
    form = getattr(bound_field, "form", None)
    model = getattr(getattr(form, "_meta", None), "model", None)
    if model is None:
        return None, None
    try:
        return model, model._meta.get_field(bound_field.name)
    except Exception:
        return model, None


def business_help(bound_field):
    """Plain-business help for any bound form field. Registry first, then the
    model field's help_text / verbose_name, then the form field. Safe for all."""
    model, mf = _model_and_field(bound_field)
    model_name = model.__name__ if model is not None else ""
    entry = FIELD_HELP.get(f"{model_name}.{bound_field.name}", {})

    desc = (entry.get("desc")
            or (str(getattr(mf, "help_text", "")) if mf is not None else "")
            or (str(bound_field.help_text) if bound_field.help_text else ""))
    if mf is not None and getattr(mf, "verbose_name", None):
        label = entry.get("label") or str(mf.verbose_name)
    else:
        label = entry.get("label") or bound_field.label or bound_field.name
    label = label[:1].upper() + label[1:] if label else label

    # Only static (enum/TextChoices) choices - never enumerate FK querysets.
    choices = []
    if mf is not None and getattr(mf, "choices", None):
        choices = [str(lbl) for _val, lbl in mf.choices][:12]

    default = ""
    if mf is not None:
        d = getattr(mf, "default", NOT_PROVIDED)
        if d is not NOT_PROVIDED and not callable(d) and d not in (None, ""):
            default = str(d)

    return {
        "label": label,
        "desc": desc,
        "expected": entry.get("expected", ""),
        "example": entry.get("example", ""),
        "warning": entry.get("warning", ""),
        "required": bool(bound_field.field.required),
        "default": default,
        "choices": choices,
    }


def technical_metadata(bound_field):
    """Database-level metadata for a bound field, derived entirely from Django's
    model introspection. Returns None for non-model forms. Sensitive fields get a
    redacted stub (name only, no column/value details)."""
    model, mf = _model_and_field(bound_field)
    if model is None:
        return None
    base = {"model": model.__name__, "table": model._meta.db_table}
    if is_sensitive(bound_field.name):
        base["sensitive"] = True
        return base
    if mf is None:
        return base  # form-only field: no column

    base["column"] = getattr(mf, "column", bound_field.name)
    base["type"] = type(mf).__name__
    ml = getattr(mf, "max_length", None)
    if ml:
        base["max_length"] = ml
    base["nullable"] = bool(getattr(mf, "null", False))
    if getattr(mf, "unique", False):
        base["unique"] = True
    if getattr(mf, "db_index", False) or getattr(mf, "unique", False):
        base["indexed"] = True
    for grp in getattr(model._meta, "unique_together", ()) or ():
        if bound_field.name in grp:
            base["unique_together"] = " + ".join(grp)
            break
    if getattr(mf, "is_relation", False) and getattr(mf, "related_model", None) is not None:
        rel = mf.related_model
        base["fk_table"] = rel._meta.db_table
        try:
            base["fk_column"] = rel._meta.pk.column
        except Exception:
            base["fk_column"] = "id"
        on_delete = getattr(getattr(mf, "remote_field", None), "on_delete", None)
        if on_delete is not None:
            base["on_delete"] = getattr(on_delete, "__name__", str(on_delete))
    return base


def can_view_technical(context):
    """Whether the current request's user may see technical DB metadata:
    superuser, app ADMIN (always has every permission), or a user granted
    `can_view_field_technical_metadata`."""
    request = context.get("request") if hasattr(context, "get") else None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    perms = context.get("perms") if hasattr(context, "get") else None
    if perms is None:
        try:
            from core.access import get_effective_permissions
            perms = get_effective_permissions(request)
        except Exception:
            perms = set()
    return permissions.VIEW_FIELD_TECHNICAL_METADATA in (perms or set())
