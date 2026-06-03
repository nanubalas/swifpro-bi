from django import forms
from django.forms import inlineformset_factory
from core.current import get_current_tenant
from core.validators import (
    validate_vat_number, validate_company_number, validate_utr, validate_phone,
)
from core.models import (
    CycleCount, CycleCountLine, InventoryLotBalance, InventoryReservation,
    PurchaseOrder, PurchaseOrderLine, Shipment,
    Product, Supplier, Location, ChannelConnection,
    SalesOrder, SalesOrderLine, Tenant,
    UnitOfMeasure, UOMConversion, BillOfMaterials, BillOfMaterialsLine, ProductBarcode,
    InventoryTransfer, InventoryTransferLine,
    GoodsReceipt, GoodsReceiptLine, LandedCostCharge,
    SupplierInvoice, SupplierInvoiceLine,
    TaxCode, Customer, CustomerInvoice, CustomerInvoiceLine, GLAccount,
    Payment, AccessRequest
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

class PurchaseOrderForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("submit", "Submit PO")),
        required=False
    )

    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "expected_date", "notes"]

PurchaseOrderLineFormSet = inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderLine,
    form=TenantModelForm,
    fields=("product", "ordered_qty", "unit_cost"),
    extra=1,
    can_delete=True
)

class ShipmentUpdateForm(TenantModelForm):
    class Meta:
        model = Shipment
        fields = ["carrier", "tracking_number", "status"]

class ProductForm(TenantModelForm):
    barcode = forms.CharField(required=False, help_text="Optional EAN/UPC/Barcode")

    class Meta:
        model = Product
        fields = ["parent", "sku", "name", "variant_name", "option1", "option2", "option3",
                  "base_uom", "uom", "cost_method", "standard_cost"]

class SupplierForm(TenantModelForm):
    class Meta:
        model = Supplier
        fields = ["name","email","phone","currency_code"]

class LocationForm(TenantModelForm):
    class Meta:
        model = Location
        fields = ["name", "type"]

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
    fields=("product", "ship_from_location", "qty", "unit_price", "lot_code", "serial_number", "expiry_date"),
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
        fields = ["code", "name", "rate", "is_active"]

class CustomerForm(TenantModelForm):
    class Meta:
        model = Customer
        fields = ["name", "email", "phone", "vat_number", "billing_address"]
        widgets = {"billing_address": forms.Textarea(attrs={"rows": 3})}

class CustomerInvoiceForm(TenantModelForm):
    action = forms.ChoiceField(
        choices=(("save", "Save Draft"), ("issue", "Issue (Post to GL)")),
        required=False
    )
    class Meta:
        model = CustomerInvoice
        fields = ["customer", "invoice_number", "invoice_date", "due_date", "notes"]

CustomerInvoiceLineFormSet = inlineformset_factory(
    CustomerInvoice,
    CustomerInvoiceLine,
    form=TenantModelForm,
    fields=("product", "description", "qty", "unit_price", "tax_code"),
    extra=1,
    can_delete=True
)

class GLAccountForm(TenantModelForm):
    class Meta:
        model = GLAccount
        fields = ["code", "name", "type", "is_active"]


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
