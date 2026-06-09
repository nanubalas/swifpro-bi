from django import forms
from django.forms import inlineformset_factory
from core.current import get_current_tenant
from core.validators import (
    validate_vat_number, validate_company_number, validate_utr, validate_phone,
)
from core.models import (
    CycleCount, CycleCountLine, InventoryLotBalance, InventoryReservation,
    PurchaseOrder, PurchaseOrderLine, Shipment,
    PurchaseRequisition, PurchaseRequisitionLine,
    Product, Supplier, Location, Site, Bin, Department, OrgMembership, ChannelConnection,
    SalesOrder, SalesOrderLine, Tenant,
    UnitOfMeasure, UOMConversion, BillOfMaterials, BillOfMaterialsLine, ProductBarcode, ProductCategory,
    StockAdjustment,
    InventoryTransfer, InventoryTransferLine,
    GoodsReceipt, GoodsReceiptLine, LandedCostCharge,
    SupplierInvoice, SupplierInvoiceLine,
    TaxCode, Customer, CustomerInvoice, CustomerInvoiceLine, GLAccount,
    Payment, AccessRequest, Expense, CreditNote, CreditNoteLine, BankTransaction,
    SalesQuote, SalesQuoteLine, CustomerOrder, CustomerOrderLine,
    RecurringInvoice, RecurringInvoiceLine
)


class TenantModelForm(forms.ModelForm):
    """Base form that scopes FK choice fields to the request's active tenant.

    Any field whose related model has a `tenant` column is filtered, so
    dropdowns (suppliers, products, locations, customers, tax codes, …) never
    expose another tenant's records.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = get_current_tenant()
        if not tenant:
            return
        for field in self.fields.values():
            qs = getattr(field, "queryset", None)
            if qs is None:
                continue
            if any(f.name == "tenant" for f in qs.model._meta.fields):
                field.queryset = qs.filter(tenant=tenant)

    def _limit_stock_locations(self, *field_names):
        """Restrict Location dropdowns to active, stock-holding locations, while
        keeping whatever value an existing record already points at."""
        from django.db.models import Q
        for name in field_names:
            field = self.fields.get(name)
            if field is None or getattr(field, "queryset", None) is None:
                continue
            current_id = getattr(self.instance, f"{name}_id", None)
            cond = Q(is_active=True, holds_stock=True)
            if current_id:
                cond = cond | Q(pk=current_id)
            field.queryset = field.queryset.filter(cond)


class PurchaseOrderForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("submit", "Submit PO")),
        required=False
    )

    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "receiving_location", "expected_date", "delivery_address", "notes"]
        widgets = {
            "expected_date": forms.DateInput(attrs={"type": "date"}),
            "delivery_address": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {"receiving_location": "Warehouse / store goods are received into."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._limit_stock_locations("receiving_location")
        self.fields["receiving_location"].required = False

PurchaseOrderLineFormSet = inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderLine,
    form=TenantModelForm,
    fields=("product", "ordered_qty", "uom", "unit_cost", "tax_code"),
    extra=1,
    can_delete=True
)

class PurchaseRequisitionForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("submit", "Submit for Approval")),
        required=False
    )

    class Meta:
        model = PurchaseRequisition
        fields = ["department", "preferred_supplier", "needed_by", "justification"]
        widgets = {
            "needed_by": forms.DateInput(attrs={"type": "date"}),
            "justification": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # department is an optional FK auto-scoped to the tenant by TenantModelForm.
        self.fields["department"].required = False


PurchaseRequisitionLineFormSet = inlineformset_factory(
    PurchaseRequisition,
    PurchaseRequisitionLine,
    form=TenantModelForm,
    fields=("product", "quantity", "estimated_unit_cost", "notes"),
    extra=1,
    can_delete=True
)


class ShipmentUpdateForm(TenantModelForm):
    class Meta:
        model = Shipment
        fields = ["carrier", "tracking_number", "status"]

class ProductForm(TenantModelForm):
    barcode = forms.CharField(required=False, help_text="Optional EAN/UPC/Barcode; must be unique if provided.")
    opening_stock = forms.DecimalField(required=False, min_value=0, help_text="Initial quantity on hand (created once).")
    opening_location = forms.ModelChoiceField(queryset=Location.objects.none(), required=False,
                                              help_text="Where the opening stock is held.")

    class Meta:
        model = Product
        fields = ["sku", "name", "product_type", "category", "brand", "description", "image",
                  "is_active", "parent", "variant_name", "option1", "option2", "option3", "pack_size",
                  "base_uom", "uom", "sales_price", "tax_code", "cost_method", "standard_cost",
                  "reorder_level", "preferred_supplier", "track_lots", "track_expiry", "track_serial"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = get_current_tenant()
        if tenant:
            self.fields["opening_location"].queryset = Location.objects.filter(tenant=tenant)

    def clean_sku(self):
        sku = (self.cleaned_data.get("sku") or "").strip()
        tenant = get_current_tenant()
        if sku and tenant:
            qs = Product.objects.filter(tenant=tenant, sku__iexact=sku)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A product with this SKU already exists.")
        return sku

    def clean_barcode(self):
        code = (self.cleaned_data.get("barcode") or "").strip()
        tenant = get_current_tenant()
        if code and tenant:
            qs = ProductBarcode.objects.filter(tenant=tenant, code__iexact=code)
            if self.instance.pk:
                qs = qs.exclude(product=self.instance)
            if qs.exists():
                raise forms.ValidationError("This barcode is already assigned to another product.")
        return code

class StockAdjustmentForm(TenantModelForm):
    class Meta:
        model = StockAdjustment
        fields = ["product", "location", "bin", "reason", "supplier", "qty_delta", "notes"]
        help_texts = {
            "qty_delta": "Negative to remove stock (damage / loss / return to supplier); positive to add found stock.",
            "supplier": "For 'Return to supplier' only — raises a purchase credit note that reduces Accounts Payable.",
            "bin": "Optional bin (must belong to the chosen location).",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._limit_stock_locations("location")
        self.fields["bin"].required = False
        if "bin" in self.fields:
            self.fields["bin"].queryset = self.fields["bin"].queryset.filter(is_active=True)

    def clean_qty_delta(self):
        qty = self.cleaned_data.get("qty_delta")
        if qty is not None and qty == 0:
            raise forms.ValidationError("Quantity change cannot be zero.")
        return qty

    def clean(self):
        cleaned = super().clean()
        reason = cleaned.get("reason")
        supplier = cleaned.get("supplier")
        qty = cleaned.get("qty_delta")
        bin_ = cleaned.get("bin")
        location = cleaned.get("location")
        if reason == StockAdjustment.Reason.RETURN_SUPPLIER:
            if not supplier:
                self.add_error("supplier", "Choose the supplier the goods are returned to.")
            if qty is not None and qty > 0:
                self.add_error("qty_delta", "A return to supplier must remove stock (negative quantity).")
        if bin_ and location and bin_.location_id != location.id:
            self.add_error("bin", "Bin must belong to the chosen location.")
        return cleaned


class ProductCategoryForm(TenantModelForm):
    class Meta:
        model = ProductCategory
        fields = ["name", "parent"]
        help_texts = {"parent": "Leave blank for a top-level category, or pick a parent for a subcategory."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only top-level categories can be parents (one level of nesting).
        qs = self.fields["parent"].queryset.filter(parent__isnull=True)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].required = False


class SupplierForm(TenantModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "status", "contact_person", "email", "phone", "vat_number",
                  "company_number", "address", "currency_code", "payment_terms_days",
                  "bank_name", "bank_account_name", "bank_sort_code", "bank_account_number",
                  "categories", "notes"]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "payment_terms_days": "Days to pay. Leave blank to use the company default.",
            "categories": "Comma-separated, e.g. Raw materials, Logistics.",
        }

class SiteForm(TenantModelForm):
    class Meta:
        model = Site
        fields = ["name", "code", "address", "contact_person", "phone", "email", "is_active"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}


class DepartmentForm(TenantModelForm):
    class Meta:
        model = Department
        fields = ["name", "code", "site", "manager", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # `manager` is a User FK (no tenant field) so TenantModelForm can't scope
        # it automatically; limit it to people who belong to this organisation.
        tenant = get_current_tenant()
        if tenant is not None and "manager" in self.fields:
            from django.contrib.auth.models import User
            member_ids = OrgMembership.objects.filter(tenant=tenant).values_list("user_id", flat=True)
            self.fields["manager"].queryset = User.objects.filter(id__in=member_ids).order_by("username")
            self.fields["manager"].required = False


class BinForm(TenantModelForm):
    class Meta:
        model = Bin
        fields = ["location", "code", "description", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._limit_stock_locations("location")


class LocationForm(TenantModelForm):
    class Meta:
        model = Location
        fields = ["name", "site", "type", "address", "contact_person", "phone", "email",
                  "opening_hours", "is_active", "holds_stock"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}
        labels = {"holds_stock": "Holds stock (show in inventory)",
                  "is_active": "Active", "opening_hours": "Opening hours (optional)"}

class ChannelConnectionForm(TenantModelForm):
    class Meta:
        model = ChannelConnection
        fields = ["channel", "name", "shop_domain", "access_token"]
        widgets = {
            "access_token": forms.Textarea(attrs={"rows": 3}),
        }

class SalesOrderForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("post", "Post (deduct inventory)")),
        required=False
    )
    class Meta:
        model = SalesOrder
        fields = ["channel", "order_number", "order_date", "ship_from_location"]  # header default; lines can override

SalesOrderLineFormSet = inlineformset_factory(
    SalesOrder,
    SalesOrderLine,
    form=TenantModelForm,
    fields=("product", "ship_from_location", "qty", "uom", "unit_price", "lot_code", "serial_number", "expiry_date"),
    extra=1,
    can_delete=True
)

class TenantSettingsForm(TenantModelForm):
    CURRENCY_CHOICES = (("GBP", "GBP (£)"), ("USD", "USD ($)"), ("EUR", "EUR (€)"))
    MONTHS = [(i, m) for i, m in enumerate(
        ["", "January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]) if i]

    currency_code = forms.ChoiceField(choices=CURRENCY_CHOICES)
    financial_year_start_month = forms.TypedChoiceField(choices=MONTHS, coerce=int, label="Financial year starts")

    REQUIRED = ["name", "legal_name", "business_type", "email",
                "address_line1", "address_city", "address_postcode"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for fname in self.REQUIRED:
            if fname in self.fields:
                self.fields[fname].required = True
        # Optional credit-control fields: fall back to current/default on save.
        if "dunning_interval_days" in self.fields:
            self.fields["dunning_interval_days"].required = False
        if "expense_approval_threshold" in self.fields:
            self.fields["expense_approval_threshold"].required = False

    def clean_dunning_interval_days(self):
        val = self.cleaned_data.get("dunning_interval_days")
        if val:
            return val
        return getattr(self.instance, "dunning_interval_days", None) or 7

    def clean_expense_approval_threshold(self):
        val = self.cleaned_data.get("expense_approval_threshold")
        if val is not None:
            return val
        from decimal import Decimal as _D
        return getattr(self.instance, "expense_approval_threshold", None) or _D("0.00")

    class Meta:
        model = Tenant
        fields = [
            # Identity
            "name", "legal_name", "trading_name", "business_type",
            # Registration & tax
            "company_number", "utr_number", "vat_registered", "vat_number",
            # Business address
            "address_line1", "address_line2", "address_city", "address_postcode", "address_country",
            # Billing address
            "billing_same_as_business", "billing_line1", "billing_line2", "billing_city", "billing_postcode", "billing_country",
            # Contact
            "email", "phone", "website",
            # Branding
            "logo", "invoice_footer",
            # Defaults & locale
            "currency_code", "country", "timezone", "financial_year_start_month",
            "default_tax_code", "default_payment_terms_days", "po_approval_threshold",
            "expense_approval_threshold",
            # Credit control / dunning
            "dunning_enabled", "dunning_interval_days",
            # Inventory controls
            "block_negative_stock",
        ]
        widgets = {
            "invoice_footer": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_vat_number(self):
        v = self.cleaned_data.get("vat_number", "")
        validate_vat_number(v)
        return v

    def clean_company_number(self):
        v = self.cleaned_data.get("company_number", "")
        validate_company_number(v)
        return v

    def clean_utr_number(self):
        v = self.cleaned_data.get("utr_number", "")
        validate_utr(v)
        return v

    def clean_phone(self):
        v = self.cleaned_data.get("phone", "")
        validate_phone(v)
        return v

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("vat_registered") and not cleaned.get("vat_number"):
            self.add_error("vat_number", "VAT number is required when the business is VAT registered.")
        return cleaned

class UnitOfMeasureForm(TenantModelForm):
    class Meta:
        model = UnitOfMeasure
        fields = ["code", "name"]


class UOMConversionForm(TenantModelForm):
    class Meta:
        model = UOMConversion
        fields = ["product", "from_uom", "to_uom", "multiplier"]


class BillOfMaterialsForm(TenantModelForm):
    class Meta:
        model = BillOfMaterials
        fields = ["product", "name", "is_active"]


BOMLineFormSet = inlineformset_factory(
    BillOfMaterials,
    BillOfMaterialsLine,
    form=TenantModelForm,
    fields=("component", "qty", "uom"),
    extra=1,
    can_delete=True
)


# ---------------- Inventory Controls ----------------

class CycleCountForm(TenantModelForm):
    class Meta:
        model = CycleCount
        fields = ["location", "count_date", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._limit_stock_locations("location")

class CycleCountLineForm(TenantModelForm):
    class Meta:
        model = CycleCountLine
        fields = ["product", "lot_code", "serial_number", "expiry_date", "counted_qty"]

CycleCountLineFormSet = inlineformset_factory(
    CycleCount,
    CycleCountLine,
    form=CycleCountLineForm,
    extra=1,
    can_delete=True
)


# --- Transfers ---
class InventoryTransferForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save","Save Draft"),("post","Post Transfer")),
        required=False
    )
    class Meta:
        model = InventoryTransfer
        fields = ["from_location","to_location","notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._limit_stock_locations("from_location", "to_location")

InventoryTransferLineFormSet = inlineformset_factory(
    InventoryTransfer,
    InventoryTransferLine,
    form=TenantModelForm,
    fields=("product","qty","lot_code","serial_number","expiry_date"),
    extra=1,
    can_delete=True
)


# --- Goods Receipt (GRN) ---
class GoodsReceiptForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save","Save Draft"),("post","Post GRN")),
        required=False
    )
    class Meta:
        model = GoodsReceipt
        fields = ["grn_number","received_at","received_to","attachment"]

GoodsReceiptLineFormSet = inlineformset_factory(
    GoodsReceipt,
    GoodsReceiptLine,
    form=TenantModelForm,
    fields=("po_line","product","qty_received","unit_cost","lot_code","serial_number","expiry_date"),
    extra=1,
    can_delete=True
)

class LandedCostChargeForm(TenantModelForm):
    class Meta:
        model = LandedCostCharge
        fields = ["name","amount","currency_code"]


# --- Supplier Invoice (3-way match) ---
class SupplierInvoiceForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save","Save Draft"),("submit","Run Match"),("approve","Approve"),("post","Post")),
        required=False
    )
    class Meta:
        model = SupplierInvoice
        fields = ["supplier","po","receipt","invoice_number","invoice_date","currency_code","attachment"]

SupplierInvoiceLineFormSet = inlineformset_factory(
    SupplierInvoice,
    SupplierInvoiceLine,
    form=TenantModelForm,
    fields=("product","po_line","receipt_line","qty","unit_cost","tax_code"),
    extra=1,
    can_delete=True
)


from core.models import ReturnAuthorization, ReturnLine

class ReturnAuthorizationForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("approve", "Approve"), ("receive", "Receive & Restock")),
        required=False
    )
    class Meta:
        model = ReturnAuthorization
        fields = ["channel", "rma_number", "original_order_number", "receive_location"]

ReturnLineFormSet = inlineformset_factory(
    ReturnAuthorization,
    ReturnLine,
    form=TenantModelForm,
    fields=("product", "qty", "reason", "lot_code", "serial_number", "expiry_date"),
    extra=1,
    can_delete=True
)


class TaxCodeForm(TenantModelForm):
    class Meta:
        model = TaxCode
        fields = ["code", "name", "rate", "kind", "is_active"]
        help_texts = {"rate": "Decimal fraction, e.g. 0.20 for 20%.",
                      "kind": "VAT treatment - drives how amounts appear on the VAT return."}

class CustomerForm(TenantModelForm):
    class Meta:
        model = Customer
        fields = ["name", "customer_type", "status", "contact_person", "email", "phone",
                  "vat_number", "company_number", "billing_address", "shipping_address",
                  "payment_terms_days", "credit_limit", "tags", "notes"]
        widgets = {
            "billing_address": forms.Textarea(attrs={"rows": 3}),
            "shipping_address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "payment_terms_days": "Days to pay. Leave blank to use the company default.",
            "credit_limit": "0 = no credit limit.",
            "tags": "Comma-separated, e.g. VIP, Reseller.",
        }

class CustomerInvoiceForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("issue", "Issue (Post to GL)")),
        required=False
    )
    class Meta:
        model = CustomerInvoice
        fields = ["customer", "location", "invoice_number", "invoice_date", "due_date", "notes", "terms"]
        widgets = {
            "invoice_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "terms": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "invoice_number": "Leave blank to auto-generate (e.g. INV-0001).",
            "location": "Shop / warehouse stock is fulfilled from.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Number is auto-generated when left blank, so it isn't required.
        self.fields["invoice_number"].required = False
        self._limit_stock_locations("location")
        self.fields["location"].required = False

CustomerInvoiceLineFormSet = inlineformset_factory(
    CustomerInvoice,
    CustomerInvoiceLine,
    form=TenantModelForm,
    fields=("product", "description", "qty", "uom", "unit_price", "discount_pct", "tax_code"),
    extra=1,
    can_delete=True
)

class GLAccountForm(TenantModelForm):
    class Meta:
        model = GLAccount
        fields = ["code", "name", "type", "is_active"]


class InviteUserForm(forms.Form):
    """Admin invites a teammate directly - creates their account + role."""
    from core.roles import ROLE_CHOICES as _ROLE_CHOICES
    name = forms.CharField(max_length=200)
    email = forms.EmailField()
    role = forms.ChoiceField(choices=_ROLE_CHOICES)
    employee_id = forms.CharField(max_length=50, required=False, label="Employee ID")


class NewOrganisationForm(forms.ModelForm):
    """Minimal first step - create the organisation; the rest is filled in
    during onboarding."""
    class Meta:
        model = Tenant
        fields = ["name", "business_type", "currency_code", "country"]


class AccessRequestForm(forms.ModelForm):
    """Public form - a prospective user requests an account from the admin."""
    class Meta:
        model = AccessRequest
        fields = ["name", "employee_id", "email", "team", "message"]
        widgets = {
            "message": forms.Textarea(attrs={"rows": 2, "placeholder": "Anything the admin should know (optional)"}),
        }
        labels = {"employee_id": "Employee ID", "email": "Email", "team": "Team / Department"}


class ReceiptForm(TenantModelForm):
    """Record money received from a customer."""
    class Meta:
        model = Payment
        fields = ["customer", "payment_date", "amount", "method", "reference", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class SupplierPaymentForm(TenantModelForm):
    """Record money paid to a supplier."""
    class Meta:
        model = Payment
        fields = ["supplier", "payment_date", "amount", "method", "reference", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class RefundForm(TenantModelForm):
    """Record money refunded to a customer."""
    class Meta:
        model = Payment
        fields = ["customer", "payment_date", "amount", "method", "reference", "notes"]
        widgets = {"payment_date": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}


class ExpenseForm(TenantModelForm):
    """Record a business cost. 'Category' is restricted to expense accounts so
    business users pick a plain category rather than dealing with the ledger."""
    class Meta:
        model = Expense
        fields = ["expense_date", "payee", "supplier", "category", "description",
                  "net_amount", "tax_code", "paid", "reimbursable", "method", "reference", "receipt"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "expense_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {"net_amount": "Net amount (before VAT)", "paid": "Already paid",
                  "reimbursable": "Reimbursable (paid personally)",
                  "receipt": "Receipt (image or PDF)"}
        help_texts = {"receipt": "Attach a photo or PDF of the receipt (optional)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only expense-type accounts are valid expense categories.
        if "category" in self.fields:
            self.fields["category"].queryset = self.fields["category"].queryset.filter(
                type__in=[GLAccount.Type.EXPENSE], is_active=True
            ).order_by("code")
        self.fields["supplier"].required = False
        self.fields["tax_code"].required = False
        self.fields["receipt"].required = False

    def clean_receipt(self):
        f = self.cleaned_data.get("receipt")
        if f and getattr(f, "name", None):
            allowed = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")
            if not f.name.lower().endswith(allowed):
                raise forms.ValidationError("Receipt must be a PDF or an image (PNG/JPG/GIF/WEBP/HEIC).")
        return f


class CreditNoteForm(TenantModelForm):
    class Meta:
        model = CreditNote
        fields = ["kind", "credit_note_number", "credit_note_date", "customer", "supplier",
                  "customer_invoice", "supplier_invoice", "reason"]
        widgets = {
            "credit_note_date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "customer_invoice": "Apply to customer invoice (optional)",
            "supplier_invoice": "Apply to supplier invoice (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("customer", "supplier", "customer_invoice", "supplier_invoice", "reason"):
            self.fields[f].required = False

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        if kind == CreditNote.Kind.SALES and not cleaned.get("customer"):
            self.add_error("customer", "Choose the customer this credit is for.")
        if kind == CreditNote.Kind.PURCHASE and not cleaned.get("supplier"):
            self.add_error("supplier", "Choose the supplier this credit is from.")
        return cleaned


CreditNoteLineFormSet = inlineformset_factory(
    CreditNote,
    CreditNoteLine,
    form=TenantModelForm,
    fields=("description", "qty", "unit_amount", "tax_code", "account"),
    extra=1,
    can_delete=True,
)


class SalesQuoteForm(TenantModelForm):
    class Meta:
        model = SalesQuote
        fields = ["customer", "quote_number", "quote_date", "valid_until", "notes", "terms"]
        widgets = {
            "quote_date": forms.DateInput(attrs={"type": "date"}),
            "valid_until": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "terms": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {"quote_number": "Leave blank to auto-generate (e.g. QUO-0001)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quote_number"].required = False


SalesQuoteLineFormSet = inlineformset_factory(
    SalesQuote, SalesQuoteLine, form=TenantModelForm,
    fields=("product", "description", "qty", "uom", "unit_price", "discount_pct", "tax_code"),
    extra=1, can_delete=True,
)


class CustomerOrderForm(TenantModelForm):
    class Meta:
        model = CustomerOrder
        fields = ["customer", "location", "order_number", "order_date", "notes", "terms"]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "terms": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "order_number": "Leave blank to auto-generate (e.g. SO-0001).",
            "location": "Shop / warehouse this order is fulfilled from.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["order_number"].required = False
        self._limit_stock_locations("location")
        self.fields["location"].required = False


CustomerOrderLineFormSet = inlineformset_factory(
    CustomerOrder, CustomerOrderLine, form=TenantModelForm,
    fields=("product", "description", "qty", "uom", "unit_price", "discount_pct", "tax_code"),
    extra=1, can_delete=True,
)


class RecurringInvoiceForm(TenantModelForm):
    class Meta:
        model = RecurringInvoice
        fields = ["name", "customer", "frequency", "interval", "start_date", "next_run_date",
                  "end_date", "max_occurrences", "auto_issue", "notes", "terms"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "next_run_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "terms": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "interval": "Every N periods (e.g. 1 = every month, 2 = every other month).",
            "next_run_date": "The date the next invoice will be generated.",
            "auto_issue": "Post each generated invoice to the ledger automatically.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["next_run_date"].required = False  # defaults to start_date

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("next_run_date") and cleaned.get("start_date"):
            cleaned["next_run_date"] = cleaned["start_date"]
        return cleaned


RecurringInvoiceLineFormSet = inlineformset_factory(
    RecurringInvoice, RecurringInvoiceLine, form=TenantModelForm,
    fields=("product", "description", "qty", "uom", "unit_price", "discount_pct", "tax_code"),
    extra=1, can_delete=True,
)


class BankTransactionForm(TenantModelForm):
    """Enter one bank statement line by hand."""
    class Meta:
        model = BankTransaction
        fields = ["txn_date", "description", "amount", "reference"]
        widgets = {"txn_date": forms.DateInput(attrs={"type": "date"})}
        help_texts = {"amount": "Positive for money received, negative for money paid out."}
