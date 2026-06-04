from django.db import models
from django.utils import timezone
from decimal import Decimal

from core.roles import ROLE_CHOICES


class Tenant(models.Model):
    class BusinessType(models.TextChoices):
        LTD = "LTD", "Limited company"
        SOLE_TRADER = "SOLE_TRADER", "Sole trader"
        PARTNERSHIP = "PARTNERSHIP", "Partnership"
        CHARITY = "CHARITY", "Charity"
        FRANCHISE = "FRANCHISE", "Franchise"

    # --- Identity ---
    name = models.CharField(max_length=255)                       # display / organisation name
    legal_name = models.CharField(max_length=255, blank=True)
    trading_name = models.CharField(max_length=255, blank=True)
    business_type = models.CharField(max_length=20, choices=BusinessType.choices, blank=True)

    # --- Registration & tax ---
    company_number = models.CharField(max_length=50, blank=True)
    utr_number = models.CharField(max_length=20, blank=True)
    vat_registered = models.BooleanField(default=False)
    vat_number = models.CharField(max_length=50, blank=True)

    # --- Business address ---
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    address_city = models.CharField(max_length=120, blank=True)
    address_postcode = models.CharField(max_length=20, blank=True)
    address_country = models.CharField(max_length=80, blank=True, default="United Kingdom")

    # --- Billing address ---
    billing_same_as_business = models.BooleanField(default=True)
    billing_line1 = models.CharField(max_length=255, blank=True)
    billing_line2 = models.CharField(max_length=255, blank=True)
    billing_city = models.CharField(max_length=120, blank=True)
    billing_postcode = models.CharField(max_length=20, blank=True)
    billing_country = models.CharField(max_length=80, blank=True, default="United Kingdom")

    # --- Contact ---
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.URLField(blank=True)

    # --- Branding ---
    logo = models.ImageField(upload_to="tenant_logos/", blank=True, null=True)
    invoice_footer = models.TextField(blank=True, help_text="Shown at the bottom of invoices and receipts.")

    # --- Defaults & locale ---
    currency_code = models.CharField(max_length=10, default="GBP")
    country = models.CharField(max_length=80, default="United Kingdom")
    timezone = models.CharField(max_length=64, default="Europe/London")
    financial_year_start_month = models.PositiveSmallIntegerField(default=4)  # April (UK)
    default_tax_code = models.ForeignKey("TaxCode", on_delete=models.SET_NULL, null=True, blank=True, related_name="default_for_tenants")
    default_payment_terms_days = models.PositiveSmallIntegerField(default=30)

    po_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )

    # Admin-configurable default landing route per role: {role_code: url_name}.
    role_landing = models.JSONField(default=dict, blank=True)

    onboarding_complete = models.BooleanField(default=False)

    # Access policy: when a member's role changes, keep their per-user permission
    # overrides (pruning ones made redundant by the new role) instead of resetting
    # to the new role's default. Off by default for predictable, safe access.
    keep_permissions_on_role_change = models.BooleanField(default=False)

    # Last date the once-a-day sales housekeeping ran (quote expiry + recurring
    # invoice generation) for this tenant; throttles the in-app scheduler.
    last_housekeeping_date = models.DateField(null=True, blank=True)

    # Stock adjustments whose absolute value is at/above this need approval
    # before they post (0 = no approval required; all adjustments auto-post).
    stock_adjustment_approval_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Automatic overdue-invoice reminders (dunning). When enabled, the daily
    # housekeeping emails customers whose invoices are past due, at most once
    # every `dunning_interval_days`.
    dunning_enabled = models.BooleanField(default=True)
    dunning_interval_days = models.PositiveSmallIntegerField(default=7)

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    """Binds a Django auth user to a primary tenant (fallback when the user
    has no org membership). Multi-org membership lives in OrgMembership."""
    user = models.OneToOneField("auth.User", on_delete=models.CASCADE, related_name="profile")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="members")

    def __str__(self):
        return f"{self.user} @ {self.tenant}"


class OrgMembership(models.Model):
    """A user's role within an organisation (tenant). A user may belong to many
    organisations with a different role in each."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    is_default = models.BooleanField(default=False)  # preferred org at login
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("user", "tenant")

    def __str__(self):
        return f"{self.user} - {self.get_role_display()} @ {self.tenant}"


class UserPermissionOverride(models.Model):
    """A per-user permission delta on top of their role baseline, scoped to one
    organisation. effect=GRANT adds a permission the role lacks; effect=REVOKE
    removes one the role normally grants. The effective permission set is the
    role's matrix permissions with these overrides applied (see core.permissions
    .effective_permissions). Owners/Admins always have everything, so overrides
    do not apply to them."""
    GRANT = "GRANT"
    REVOKE = "REVOKE"
    EFFECT_CHOICES = [(GRANT, "Grant"), (REVOKE, "Revoke")]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="permission_overrides")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="permission_overrides")
    permission = models.CharField(max_length=50)
    effect = models.CharField(max_length=6, choices=EFFECT_CHOICES)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "user", "permission")
        indexes = [models.Index(fields=["tenant", "user"])]

    def __str__(self):
        return f"{self.user} {self.effect} {self.permission} @ {self.tenant}"


class AccessRequest(models.Model):
    """A request from a prospective user to be granted access. Submitted from
    the public request-access form; an Admin reviews and approves (which
    provisions the account) or rejects."""
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    name = models.CharField(max_length=200)
    employee_id = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField()
    team = models.CharField(max_length=100, blank=True, null=True)
    message = models.TextField(blank=True, null=True)

    tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name="access_requests")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_access_requests")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="from_access_request")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"


class AuditLog(models.Model):
    """Security/audit trail: logins, access-denied, and sensitive actions."""
    tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    username = models.CharField(max_length=150, blank=True, null=True)
    action = models.CharField(max_length=50)          # LOGIN, LOGOUT, ACCESS_DENIED, ...
    detail = models.CharField(max_length=255, blank=True, null=True)
    path = models.CharField(max_length=255, blank=True, null=True)
    ip = models.CharField(max_length=64, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "-created_at"]), models.Index(fields=["action"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.username or ''}"


class Location(models.Model):
    class Type(models.TextChoices):
        WAREHOUSE = "WAREHOUSE", "Warehouse"
        SHOP = "STORE", "Shop / Store"
        OFFICE = "OFFICE", "Office"
        VAN = "VAN", "Van"
        THREEPL = "THREEPL", "3PL"
        POPUP = "POPUP", "Pop-up location"
        TRANSIT = "TRANSIT", "Transit"
        RETURNS = "RETURNS", "Returns"
        QUARANTINE = "QUARANTINE", "Quarantine"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.WAREHOUSE)

    class Meta:
        unique_together = ("tenant", "name")

    def __str__(self):
        return f"{self.tenant.name} - {self.name}"


class Supplier(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ON_HOLD = "ON_HOLD", "On hold"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=200, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    vat_number = models.CharField(max_length=50, blank=True, null=True)
    company_number = models.CharField(max_length=50, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    payment_terms_days = models.PositiveSmallIntegerField(blank=True, null=True)  # null -> company default
    # Bank details for paying the supplier.
    bank_name = models.CharField(max_length=120, blank=True, null=True)
    bank_account_name = models.CharField(max_length=200, blank=True, null=True)
    bank_sort_code = models.CharField(max_length=20, blank=True, null=True)
    bank_account_number = models.CharField(max_length=40, blank=True, null=True)
    categories = models.CharField(max_length=255, blank=True, null=True)  # comma-separated
    notes = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "name")

    def __str__(self):
        return self.name

    @property
    def category_list(self):
        return [c.strip() for c in (self.categories or "").split(",") if c.strip()]

    @property
    def outstanding_payables(self):
        """Total still owed across this supplier's posted bills."""
        from decimal import Decimal as _D
        total = _D("0.00")
        for inv in SupplierInvoice.objects.filter(tenant=self.tenant, supplier=self, status="POSTED").prefetch_related(
                "lines", "lines__tax_code", "payment_allocations", "credit_notes"):
            out = inv.outstanding
            if out > _D("0.00"):
                total += out
        return total


class UnitOfMeasure(models.Model):
    """UOM master (e.g., each, case, box, kg)."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=64, blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "code")

    def __str__(self):
        return self.code


class UOMConversion(models.Model):
    """Conversion rules. If product is null, conversion is global for the tenant."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey("Product", on_delete=models.CASCADE, blank=True, null=True, related_name="uom_conversions")
    from_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.CASCADE, related_name="conversions_from")
    to_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.CASCADE, related_name="conversions_to")
    multiplier = models.DecimalField(max_digits=18, decimal_places=6, help_text="Multiply qty in from_uom by this to get qty in to_uom.")

    class Meta:
        unique_together = ("tenant", "product", "from_uom", "to_uom")

    def __str__(self):
        scope = self.product.sku if self.product else "GLOBAL"
        return f"{scope}: 1 {self.from_uom.code} = {self.multiplier} {self.to_uom.code}"


class ProductCategory(models.Model):
    """Product category, optionally nested one level for subcategories."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="product_categories")
    name = models.CharField(max_length=120)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="subcategories")

    class Meta:
        unique_together = ("tenant", "name", "parent")
        verbose_name_plural = "product categories"

    def __str__(self):
        return f"{self.parent.name} / {self.name}" if self.parent_id else self.name


class Product(models.Model):
    class CostMethod(models.TextChoices):
        FIFO = "FIFO", "FIFO"
        AVERAGE = "AVERAGE", "Average"
        STANDARD = "STANDARD", "Standard"

    class Type(models.TextChoices):
        STOCK = "STOCK", "Stock item"
        SERVICE = "SERVICE", "Service"
        NON_STOCK = "NON_STOCK", "Non-stock item"
        BUNDLE = "BUNDLE", "Bundle / kit"
        RAW_MATERIAL = "RAW_MATERIAL", "Raw material"
        FINISHED_GOOD = "FINISHED_GOOD", "Finished good"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    sku = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    product_type = models.CharField(max_length=15, choices=Type.choices, default=Type.STOCK)
    category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="products")
    brand = models.CharField(max_length=120, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to="product_images/", blank=True, null=True)
    is_active = models.BooleanField(default=True)

    # Variants (keep SKU-level records; link variants to a parent 'style' SKU)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, blank=True, null=True, related_name="variants")
    variant_name = models.CharField(max_length=255, blank=True, null=True)
    option1 = models.CharField(max_length=64, blank=True, null=True)  # e.g., Size=M
    option2 = models.CharField(max_length=64, blank=True, null=True)  # e.g., Color=Black
    option3 = models.CharField(max_length=64, blank=True, null=True)
    pack_size = models.CharField(max_length=64, blank=True, null=True)  # e.g., "Box of 12"

    # UOM
    base_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, blank=True, null=True)
    uom = models.CharField(max_length=32, default="each")  # legacy display / quick entry

    # Pricing
    sales_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_code = models.ForeignKey("TaxCode", on_delete=models.SET_NULL, null=True, blank=True, related_name="products")

    # Costing
    cost_method = models.CharField(max_length=20, choices=CostMethod.choices, default=CostMethod.AVERAGE)
    standard_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # Running moving-average cost, maintained on inbound movements.
    average_cost = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    reorder_level = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # Preferred supplier for reordering this product.
    preferred_supplier = models.ForeignKey("Supplier", on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name="preferred_products")
    # Batch / expiry / serial tracking flags.
    track_lots = models.BooleanField(default=False)
    track_expiry = models.BooleanField(default=False)
    track_serial = models.BooleanField(default=False)

    class Meta:
        unique_together = ("tenant", "sku")

    @property
    def cost_price(self):
        return self.average_cost or self.standard_cost or Decimal("0.00")

    @property
    def margin(self):
        if not self.sales_price:
            return None
        return self.sales_price - self.cost_price

    @property
    def margin_pct(self):
        m = self.margin
        if m is None or not self.sales_price:
            return None
        return (m / self.sales_price * Decimal("100")).quantize(Decimal("0.1"))

    @property
    def on_hand_total(self):
        from decimal import Decimal as _D
        return sum((b.on_hand or _D("0.00") for b in InventoryBalance.objects.filter(tenant=self.tenant, product=self)), _D("0.00"))

    def __str__(self):
        return f"{self.sku} - {self.name}"


class ProductBarcode(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="barcodes")
    code = models.CharField(max_length=64)

    class Meta:
        unique_together = ("tenant", "code")

    def __str__(self):
        return self.code


class BillOfMaterials(models.Model):
    """BOM / Kit definition for an assembled/bundled SKU."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="boms")
    name = models.CharField(max_length=200, default="Default BOM")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "product", "name")

    def __str__(self):
        return f"{self.product.sku} - {self.name}"


class BillOfMaterialsLine(models.Model):
    bom = models.ForeignKey(BillOfMaterials, on_delete=models.CASCADE, related_name="lines")
    component = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="bom_component_of")
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, blank=True, null=True)

    class Meta:
        unique_together = ("bom", "component")

    def __str__(self):
        return f"{self.component.sku} x {self.qty}"


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVAL_PENDING = "APPROVAL_PENDING", "Approval Pending"
        APPROVED = "APPROVED", "Approved"
        SENT = "SENT", "Sent"
        IN_TRANSIT = "IN_TRANSIT", "In Transit"
        PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially Received"
        RECEIVED = "RECEIVED", "Fully Received"
        BILLED = "BILLED", "Billed"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    po_number = models.CharField(max_length=40)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    delivery_address = models.TextField(blank=True, null=True)  # where goods should be delivered
    currency_code = models.CharField(max_length=3, default="GBP")  # defaults from supplier/tenant
    version = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="amended_by")
    is_current = models.BooleanField(default=True)
    cancelled_reason = models.CharField(max_length=255, blank=True, null=True)
    sent_to = models.EmailField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    sent_subject = models.CharField(max_length=200, blank=True, null=True)
    approval_required = models.BooleanField(default=False)  # NEW
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_pos")  # NEW
    approved_at = models.DateTimeField(null=True, blank=True)  # NEW
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(default=timezone.now)
    expected_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "po_number")

    def __str__(self):
        return self.po_number

    @property
    def subtotal(self):
        return sum((l.line_total for l in self.lines.all()), Decimal("0.00"))

    @property
    def tax_total(self):
        return sum((l.tax_amount for l in self.lines.all()), Decimal("0.00"))

    @property
    def total(self):
        return self.subtotal + self.tax_total

    @property
    def is_fully_received(self):
        lines = list(self.lines.all())
        return bool(lines) and all(l.open_qty <= 0 for l in lines)


class PurchaseOrderLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    ordered_qty = models.DecimalField(max_digits=12, decimal_places=2)
    received_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    tax_code = models.ForeignKey("TaxCode", on_delete=models.SET_NULL, null=True, blank=True, related_name="po_lines")

    class Meta:
        unique_together = ("po", "product")

    @property
    def open_qty(self):
        return self.ordered_qty - self.received_qty

    @property
    def line_total(self):
        return self.ordered_qty * self.unit_cost

    @property
    def tax_amount(self):
        rate = self.tax_code.rate if self.tax_code else Decimal("0.00")
        return self.line_total * rate



class SupplierPriceHistory(models.Model):
    """A recorded unit cost for a supplier+product at a point in time.

    Captured when a PO is submitted (the agreed price) and when a supplier bill
    is posted (the actual billed cost). Lets buyers see last/average paid and
    powers price-prefill at PO entry."""
    class Source(models.TextChoices):
        PO = "PO", "Purchase Order"
        BILL = "BILL", "Supplier Bill"
        MANUAL = "MANUAL", "Manual"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="price_history")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_prices")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    currency_code = models.CharField(max_length=3, default="GBP")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.PO)
    reference = models.CharField(max_length=64, blank=True, null=True)  # PO / bill number
    recorded_at = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-recorded_at", "-id"]
        # One record per source document line (idempotent capture).
        unique_together = ("tenant", "supplier", "product", "source", "reference")

    def __str__(self):
        return f"{self.supplier_id}/{self.product_id} {self.unit_cost} ({self.source})"


class PurchaseRequisition(models.Model):
    """Internal purchase request that precedes a Purchase Order.

    Flow: Draft -> Submitted (pending approval) -> Approved -> Converted (to PO).
    May also be Rejected or Cancelled.
    """
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CONVERTED = "CONVERTED", "Converted to PO"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    req_number = models.CharField(max_length=40)
    department = models.CharField(max_length=100, blank=True, null=True)
    preferred_supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="requisitions")
    needed_by = models.DateField(null=True, blank=True)
    justification = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    requested_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="requisitions")
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_requisitions")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.CharField(max_length=255, blank=True, null=True)
    converted_po = models.ForeignKey("PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="from_requisition")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "req_number")

    def __str__(self):
        return self.req_number

    @property
    def estimated_total(self):
        return sum((l.estimated_total for l in self.lines.all()), Decimal("0.00"))


class PurchaseRequisitionLine(models.Model):
    requisition = models.ForeignKey(PurchaseRequisition, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    estimated_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, null=True)

    @property
    def estimated_total(self):
        return self.quantity * (self.estimated_unit_cost or Decimal("0.00"))


class PurchaseOrderAmendment(models.Model):
    """Tracks an amendment action and links old PO to new PO version."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    from_po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="amendments_from")
    to_po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="amendments_to")
    reason = models.CharField(max_length=255)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.from_po.po_number} v{self.from_po.version} -> v{self.to_po.version}"


class Shipment(models.Model):
    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PICKED_UP = "PICKED_UP", "Picked Up"
        IN_TRANSIT = "IN_TRANSIT", "In Transit"
        ARRIVED = "ARRIVED", "Arrived"
        DELIVERED = "DELIVERED", "Delivered"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    po = models.ForeignKey(PurchaseOrder, related_name="shipments", on_delete=models.CASCADE)
    from_supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    destination = models.ForeignKey(Location, on_delete=models.PROTECT)
    carrier = models.CharField(max_length=100, blank=True, null=True)
    tracking_number = models.CharField(max_length=100, blank=True, null=True)
    eta = models.DateField(blank=True, null=True)
    reference = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED)
    created_at = models.DateTimeField(default=timezone.now)



class ShipmentLine(models.Model):
    shipment = models.ForeignKey(Shipment, related_name="lines", on_delete=models.CASCADE)
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT)
    expected_qty = models.DecimalField(max_digits=12, decimal_places=2)
    received_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        unique_together = ("shipment", "po_line")

    @property
    def open_qty(self):
        return self.expected_qty - self.received_qty


class Container(models.Model):
    shipment = models.ForeignKey(Shipment, related_name="containers", on_delete=models.CASCADE)
    container_number = models.CharField(max_length=50)
    seal_number = models.CharField(max_length=50, blank=True, null=True)
    mode = models.CharField(max_length=20, blank=True, null=True)  # air/sea/road
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("shipment", "container_number")

    def __str__(self):
        return self.container_number


class ShipmentEvent(models.Model):
    shipment = models.ForeignKey(Shipment, related_name="events", on_delete=models.CASCADE)
    container = models.ForeignKey(Container, related_name="events", on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=50)
    status = models.CharField(max_length=20, blank=True, null=True)
    notes = models.CharField(max_length=255, blank=True, null=True)
    occurred_at = models.DateTimeField(default=timezone.now)


class InventoryBalance(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    on_hand = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        unique_together = ("tenant", "product", "location")


class InventoryLotBalance(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    lot_code = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)
    on_hand = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        unique_together = ("tenant", "product", "location", "lot_code", "serial_number", "expiry_date")


class InventoryReservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        RELEASED = "RELEASED", "Released"
        CONSUMED = "CONSUMED", "Consumed"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    # Optional lot/serial tracking
    lot_code = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)

    # Generic reference (e.g., SALES_ORDER)
    ref_type = models.CharField(max_length=50)
    ref_id = models.CharField(max_length=100)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "product", "location", "status"]),
            models.Index(fields=["tenant", "ref_type", "ref_id"]),
        ]


class CycleCount(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        POSTED = "POSTED", "Posted"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    count_date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"CycleCount {self.id} ({self.location.name})"


class CycleCountLine(models.Model):
    cycle_count = models.ForeignKey(CycleCount, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)

    # Optional lot/serial
    lot_code = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)

    system_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    counted_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    variance_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        unique_together = ("cycle_count", "product", "lot_code", "serial_number", "expiry_date")


class InventoryMovement(models.Model):
    class MovementType(models.TextChoices):
        RECEIVE = "RECEIVE", "Purchase received"
        SALE = "SALE", "Sale fulfilled"
        TRANSFER_IN = "TRANSFER_IN", "Transfer in"
        TRANSFER_OUT = "TRANSFER_OUT", "Transfer out"
        ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"
        RETURN = "RETURN", "Return from customer"
        RETURN_SUPPLIER = "RETURN_SUPPLIER", "Return to supplier"
        DAMAGE = "DAMAGE", "Damage"
        WRITE_OFF = "WRITE_OFF", "Write-off"
        RESERVATION = "RESERVATION", "Reservation"
        RELEASE = "RELEASE", "Release"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    # Who triggered the movement (null for system/automatic postings).
    user = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    qty_delta = models.DecimalField(max_digits=12, decimal_places=2)
    # Cost captured at the time of the movement (unit_cost = cost per unit;
    # value = qty_delta * unit_cost, so it carries the same sign as qty_delta).
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    ref_type = models.CharField(max_length=50)  # "PO", "ORDER", "MANUAL"
    ref_id = models.CharField(max_length=100)   # po_number or order_id
    notes = models.CharField(max_length=255, blank=True, null=True)
    lot_code = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)


class StockAdjustment(models.Model):
    """A manual stock change (correction, damage, write-off/loss, return to
    supplier). Sensitive adjustments (value at/above the tenant threshold) wait
    for approval; otherwise they post immediately. Posting writes one inventory
    movement of the matching type."""
    class Reason(models.TextChoices):
        ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"
        DAMAGE = "DAMAGE", "Damaged stock"
        WRITE_OFF = "WRITE_OFF", "Lost / write-off"
        RETURN_SUPPLIER = "RETURN_SUPPLIER", "Return to supplier"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        POSTED = "POSTED", "Posted"
        REJECTED = "REJECTED", "Rejected"

    # Reason -> the inventory movement type it posts as.
    REASON_TO_MOVEMENT = {
        "ADJUSTMENT": "ADJUSTMENT", "DAMAGE": "DAMAGE",
        "WRITE_OFF": "WRITE_OFF", "RETURN_SUPPLIER": "RETURN_SUPPLIER",
    }

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="stock_adjustments")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.ADJUSTMENT)
    # For RETURN_SUPPLIER: who the goods go back to (drives a purchase credit note).
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_returns")
    credit_note = models.ForeignKey("CreditNote", on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_returns")
    qty_delta = models.DecimalField(max_digits=12, decimal_places=2)  # signed: +found / -loss
    notes = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    estimated_value = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    requested_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_adjustments")
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_adjustments")
    created_at = models.DateTimeField(default=timezone.now)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Adj {self.product.sku} {self.qty_delta} ({self.get_reason_display()})"

    @property
    def movement_type(self):
        return self.REASON_TO_MOVEMENT.get(self.reason, "ADJUSTMENT")


class InventoryCostLayer(models.Model):
    """A FIFO cost layer: a tranche of stock received at a known unit cost.

    Used only for products whose cost_method is FIFO. Outbound movements
    consume layers oldest-first (by received_at, id)."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="cost_layers")
    received_at = models.DateTimeField(default=timezone.now)
    qty_received = models.DecimalField(max_digits=12, decimal_places=2)
    qty_remaining = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4)
    ref_type = models.CharField(max_length=50, blank=True, null=True)
    ref_id = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["tenant", "product", "qty_remaining"])]
        ordering = ["received_at", "id"]


# ---------- Phase 2 (Channel Sync) ----------

class SalesChannel(models.TextChoices):
    SHOPIFY = "SHOPIFY", "Shopify"
    AMAZON = "AMAZON", "Amazon"


class ChannelConnection(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=SalesChannel.choices)
    name = models.CharField(max_length=100, default="default")

    # Keep secrets out of DB in real deployment; for MVP okay.
    access_token = models.TextField(blank=True, null=True)
    shop_domain = models.CharField(max_length=255, blank=True, null=True)  # Shopify
    created_at = models.DateTimeField(default=timezone.now)


class SyncRun(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=SalesChannel.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=20, default="RUNNING")
    detail = models.TextField(blank=True, null=True)


class ChannelSnapshot(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=SalesChannel.choices)
    sku = models.CharField(max_length=64)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    as_of = models.DateTimeField(default=timezone.now)


class ChannelOrder(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=SalesChannel.choices)
    external_order_id = models.CharField(max_length=100)
    processed_at = models.DateTimeField()
    payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "channel", "external_order_id")

class SalesOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=SalesChannel.choices, default=SalesChannel.SHOPIFY)
    order_number = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    order_date = models.DateTimeField(default=timezone.now)
    ship_from_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True)  # optional default
    currency_code = models.CharField(max_length=3, default="GBP")  # use tenant default
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "channel", "order_number")

class SalesOrderLine(models.Model):
    order = models.ForeignKey(SalesOrder, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    ship_from_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True)
    lot_code = models.CharField(max_length=100, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        unique_together = ("order", "product")

    @property
    def line_total(self):
        return self.qty * self.unit_price


# --- Transfers ---
class InventoryTransfer(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    transfer_number = models.CharField(max_length=50)
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="transfers_out")
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="transfers_in")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    posted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "transfer_number")

    def __str__(self):
        return self.transfer_number


class InventoryTransferLine(models.Model):
    transfer = models.ForeignKey(InventoryTransfer, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    qty = models.DecimalField(max_digits=12, decimal_places=2)

    lot_code = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)

    class Meta:
        unique_together = ("transfer", "product", "lot_code", "serial_number", "expiry_date")


# --- Receiving / GRN ---
class GoodsReceipt(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    shipment = models.ForeignKey(Shipment, on_delete=models.PROTECT, related_name="receipts", null=True, blank=True)
    grn_number = models.CharField(max_length=50)
    received_at = models.DateTimeField(default=timezone.now)
    received_to = models.ForeignKey(Location, on_delete=models.PROTECT)
    attachment = models.FileField(upload_to="grn_attachments/", blank=True, null=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    posted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "grn_number")

    def __str__(self):
        return self.grn_number


class GoodsReceiptLine(models.Model):
    receipt = models.ForeignKey(GoodsReceipt, related_name="lines", on_delete=models.CASCADE)
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    qty_received = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)

    lot_code = models.CharField(max_length=50, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)

    class Meta:
        unique_together = ("receipt", "po_line", "lot_code", "serial_number", "expiry_date")


# --- Landed cost ---
class LandedCostCharge(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    receipt = models.ForeignKey(GoodsReceipt, related_name="landed_costs", on_delete=models.CASCADE)
    name = models.CharField(max_length=100, default="Freight")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency_code = models.CharField(max_length=3, default="GBP")
    created_at = models.DateTimeField(default=timezone.now)


# --- AP / 3-way match ---
class SupplierInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        MATCHED = "MATCHED", "Matched"
        APPROVED = "APPROVED", "Approved"
        POSTED = "POSTED", "Posted"
        REJECTED = "REJECTED", "Rejected"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="invoices")
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=50)
    invoice_date = models.DateField(default=timezone.now)
    currency_code = models.CharField(max_length=3, default="GBP")
    attachment = models.FileField(upload_to="invoice_attachments/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    created_at = models.DateTimeField(default=timezone.now)
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_invoices")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("tenant", "supplier", "invoice_number")

    def __str__(self):
        return self.invoice_number

    @property
    def subtotal(self):
        return sum((l.line_total for l in self.lines.all()), Decimal("0.00"))

    @property
    def tax_total(self):
        return sum((l.tax_amount for l in self.lines.all()), Decimal("0.00"))

    @property
    def total(self):
        return self.subtotal + self.tax_total

    @property
    def amount_paid(self):
        return sum((a.amount for a in self.payment_allocations.all()), Decimal("0.00"))

    @property
    def credit_applied(self):
        return sum((c.total for c in self.credit_notes.all() if c.status == "POSTED"), Decimal("0.00"))

    @property
    def outstanding(self):
        return self.total - self.amount_paid - self.credit_applied


class SupplierInvoiceLine(models.Model):
    invoice = models.ForeignKey(SupplierInvoice, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, null=True, blank=True)
    receipt_line = models.ForeignKey(GoodsReceiptLine, on_delete=models.PROTECT, null=True, blank=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    tax_code = models.ForeignKey("TaxCode", on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        unique_together = ("invoice", "product", "po_line", "receipt_line")

    @property
    def line_total(self):
        return (self.qty or Decimal("0.00")) * (self.unit_cost or Decimal("0.00"))

    @property
    def tax_amount(self):
        rate = self.tax_code.rate if self.tax_code else Decimal("0.00")
        return self.line_total * rate


class ReturnAuthorization(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"
        RECEIVED = "RECEIVED", "Received"
        CLOSED = "CLOSED", "Closed"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    channel = models.CharField(max_length=20, choices=SalesChannel.choices, default=SalesChannel.SHOPIFY)
    rma_number = models.CharField(max_length=50)
    original_order_number = models.CharField(max_length=50, blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    receive_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="returns_received_to")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "channel", "rma_number")

class ReturnLine(models.Model):
    rma = models.ForeignKey(ReturnAuthorization, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=200, blank=True, null=True)
    lot_code = models.CharField(max_length=100, blank=True, null=True)
    serial_number = models.CharField(max_length=100, blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)

    class Meta:
        unique_together = ("rma", "product", "lot_code", "serial_number", "expiry_date")


# ============================
# Customer sales documents (non-POS): Quote -> Sales Order -> Invoice
# ============================

class _SalesTotalsMixin:
    """Subtotal / VAT / grand-total computed from `self.lines`."""
    @property
    def subtotal(self):
        return sum((l.line_total for l in self.lines.all()), Decimal("0.00"))

    @property
    def tax_total(self):
        return sum((l.tax_amount for l in self.lines.all()), Decimal("0.00"))

    @property
    def total(self):
        return self.subtotal + self.tax_total


class _SalesLine(models.Model):
    """Shared line shape for quotes and customer orders (mirrors invoice lines)."""
    product = models.ForeignKey(Product, on_delete=models.PROTECT, blank=True, null=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.00"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), blank=True)
    tax_code = models.ForeignKey("TaxCode", on_delete=models.PROTECT, blank=True, null=True)

    class Meta:
        abstract = True

    @property
    def gross(self):
        return (self.qty or Decimal("0.00")) * (self.unit_price or Decimal("0.00"))

    @property
    def discount_amount(self):
        return (self.gross * (self.discount_pct or Decimal("0.00")) / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def line_total(self):
        return self.gross - self.discount_amount

    @property
    def tax_amount(self):
        rate = self.tax_code.rate if self.tax_code else Decimal("0.00")
        return self.line_total * rate


class SalesQuote(_SalesTotalsMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"
        CONVERTED = "CONVERTED", "Converted"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="quotes")
    customer = models.ForeignKey("Customer", on_delete=models.PROTECT, related_name="quotes")
    quote_number = models.CharField(max_length=50)
    quote_date = models.DateField(default=timezone.now)
    valid_until = models.DateField(blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "quote_number")

    def __str__(self):
        return self.quote_number

    @property
    def is_expired(self):
        from django.utils import timezone as _tz
        return bool(self.valid_until and self.status in ("SENT", "DRAFT") and self.valid_until < _tz.localdate())


class SalesQuoteLine(_SalesLine):
    quote = models.ForeignKey(SalesQuote, related_name="lines", on_delete=models.CASCADE)


class CustomerOrder(_SalesTotalsMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        CONFIRMED = "CONFIRMED", "Confirmed"
        INVOICED = "INVOICED", "Invoiced"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="customer_orders")
    customer = models.ForeignKey("Customer", on_delete=models.PROTECT, related_name="customer_orders")
    order_number = models.CharField(max_length=50)
    order_date = models.DateField(default=timezone.now)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)
    quote = models.ForeignKey(SalesQuote, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "order_number")

    def __str__(self):
        return self.order_number


class CustomerOrderLine(_SalesLine):
    order = models.ForeignKey(CustomerOrder, related_name="lines", on_delete=models.CASCADE)


class RecurringInvoice(_SalesTotalsMixin, models.Model):
    """A template that generates customer invoices on a schedule."""
    class Frequency(models.TextChoices):
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        YEARLY = "YEARLY", "Yearly"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="recurring_invoices")
    customer = models.ForeignKey("Customer", on_delete=models.PROTECT, related_name="recurring_invoices")
    name = models.CharField(max_length=120)  # e.g. "Monthly retainer"
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)
    interval = models.PositiveSmallIntegerField(default=1)  # every N periods
    start_date = models.DateField(default=timezone.now)
    next_run_date = models.DateField()
    end_date = models.DateField(blank=True, null=True)
    max_occurrences = models.PositiveIntegerField(blank=True, null=True)
    occurrences = models.PositiveIntegerField(default=0)
    auto_issue = models.BooleanField(default=True)  # post to GL on generation vs leave draft
    currency_code = models.CharField(max_length=3, default="GBP")
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_run_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.name} ({self.customer})"


class RecurringInvoiceLine(_SalesLine):
    template = models.ForeignKey(RecurringInvoice, related_name="lines", on_delete=models.CASCADE)


# ============================
# Finance (VAT + AR + GL)
# ============================

class TaxCode(models.Model):
    class Kind(models.TextChoices):
        STANDARD = "STANDARD", "Standard rate"
        REDUCED = "REDUCED", "Reduced rate"
        ZERO = "ZERO", "Zero rate"
        EXEMPT = "EXEMPT", "Exempt"
        OUTSIDE_SCOPE = "OUTSIDE", "Outside the scope of VAT"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    rate = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0000"))  # e.g. 0.2000 for 20%
    # VAT treatment drives the VAT return: outside-scope is excluded from the
    # net sales/purchases boxes; zero/exempt are included at a 0 rate.
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.STANDARD)
    is_active = models.BooleanField(default=True)

    @property
    def in_vat_boxes(self):
        """Whether amounts with this code count toward boxes 6/7 (net sales/
        purchases). Everything except outside-the-scope supplies is included."""
        return self.kind != self.Kind.OUTSIDE_SCOPE

    class Meta:
        unique_together = ("tenant", "code")

    def __str__(self):
        return f"{self.code} ({self.rate})"


class Customer(models.Model):
    class Type(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", "Individual"
        COMPANY = "COMPANY", "Company"
        TRADE = "TRADE", "Trade customer"
        WHOLESALE = "WHOLESALE", "Wholesale customer"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        ON_HOLD = "ON_HOLD", "On hold"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    customer_type = models.CharField(max_length=12, choices=Type.choices, default=Type.COMPANY)
    contact_person = models.CharField(max_length=200, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    vat_number = models.CharField(max_length=50, blank=True, null=True)
    company_number = models.CharField(max_length=50, blank=True, null=True)
    billing_address = models.TextField(blank=True, null=True)
    shipping_address = models.TextField(blank=True, null=True)
    # null payment terms -> fall back to the company default.
    payment_terms_days = models.PositiveSmallIntegerField(blank=True, null=True)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))  # 0 = no limit
    notes = models.TextField(blank=True, null=True)
    tags = models.CharField(max_length=255, blank=True, null=True)  # comma-separated
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("tenant", "name")

    def __str__(self):
        return self.name

    @property
    def tag_list(self):
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    @property
    def outstanding_balance(self):
        """Total still owed across the customer's live (issued/sent) invoices."""
        from decimal import Decimal as _D
        total = _D("0.00")
        for inv in CustomerInvoice.objects.filter(tenant=self.tenant, customer=self,
                                                   status__in=CustomerInvoice.ISSUED_STATES).prefetch_related(
                "lines", "lines__tax_code", "payment_allocations", "credit_notes"):
            out = inv.outstanding
            if out > _D("0.00"):
                total += out
        return total

    @property
    def available_credit(self):
        if not self.credit_limit or self.credit_limit <= Decimal("0.00"):
            return None  # no limit set
        return self.credit_limit - self.outstanding_balance

    @property
    def is_over_limit(self):
        ac = self.available_credit
        return ac is not None and ac < Decimal("0.00")

    def credit_status(self, additional=Decimal("0.00")):
        """Assess whether taking on `additional` new receivable is allowed.

        Returns (ok, reason). ok=False means the sale should be blocked:
        either the customer is on hold, or a credit limit is set and this
        amount would push the outstanding balance over it.
        """
        if self.status == self.Status.ON_HOLD:
            return False, f"{self.name} is on hold — new sales are blocked until released."
        if not self.credit_limit or self.credit_limit <= Decimal("0.00"):
            return True, ""  # no limit configured
        projected = self.outstanding_balance + (additional or Decimal("0.00"))
        if projected > self.credit_limit:
            over = projected - self.credit_limit
            return False, (
                f"Credit limit exceeded for {self.name}: limit {self.credit_limit:.2f}, "
                f"outstanding {self.outstanding_balance:.2f}, this amount {additional:.2f} "
                f"(over by {over:.2f})."
            )
        return True, ""


class CustomerInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"
        SENT = "SENT", "Sent"
        PAID = "PAID", "Paid"
        CANCELLED = "CANCELLED", "Cancelled"
        REFUNDED = "REFUNDED", "Refunded"
        VOID = "VOID", "Void"  # legacy alias of cancelled

    # Statuses that represent a live, GL-posted invoice (used by aged/VAT reports).
    ISSUED_STATES = ("ISSUED", "SENT", "PAID")
    OPEN_STATES = ("ISSUED", "SENT")  # may still have an outstanding balance

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    invoice_number = models.CharField(max_length=50)
    invoice_date = models.DateField(default=timezone.now)
    due_date = models.DateField(blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)  # terms & conditions shown on the invoice

    created_at = models.DateTimeField(default=timezone.now)
    issued_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    # Dunning: when the last overdue reminder went out, and how many so far.
    last_reminder_at = models.DateField(blank=True, null=True)
    reminder_count = models.PositiveSmallIntegerField(default=0)
    # Conversion traceability (set when generated from a quote / order).
    source_quote = models.ForeignKey("SalesQuote", on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    source_order = models.ForeignKey("CustomerOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")

    class Meta:
        unique_together = ("tenant", "invoice_number")

    def __str__(self):
        return self.invoice_number

    @property
    def is_overdue(self):
        from django.utils import timezone as _tz
        return bool(self.due_date and self.status in self.OPEN_STATES
                    and self.outstanding > Decimal("0.00") and self.due_date < _tz.localdate())

    @property
    def display_status(self):
        """The customer-facing status, including the derived Partially paid /
        Overdue states that aren't stored on the lifecycle field."""
        if self.status in ("DRAFT", "CANCELLED", "VOID", "REFUNDED", "PAID"):
            return "Cancelled" if self.status == "VOID" else self.get_status_display()
        # ISSUED / SENT: refine by payment + due date.
        paid = self.amount_paid + self.credit_applied
        if self.outstanding <= Decimal("0.00"):
            return "Paid"
        if self.is_overdue:
            return "Overdue"
        if paid > Decimal("0.00"):
            return "Partially paid"
        return self.get_status_display()

    @property
    def subtotal(self):
        return sum((l.line_total for l in self.lines.all()), Decimal("0.00"))

    @property
    def tax_total(self):
        return sum((l.tax_amount for l in self.lines.all()), Decimal("0.00"))

    @property
    def total(self):
        return self.subtotal + self.tax_total

    @property
    def amount_paid(self):
        return sum((a.amount for a in self.payment_allocations.all()), Decimal("0.00"))

    @property
    def credit_applied(self):
        return sum((c.total for c in self.credit_notes.all() if c.status == "POSTED"), Decimal("0.00"))

    @property
    def outstanding(self):
        return self.total - self.amount_paid - self.credit_applied


class CustomerInvoiceLine(models.Model):
    invoice = models.ForeignKey(CustomerInvoice, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, blank=True, null=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.00"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), blank=True)  # % off this line
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, blank=True, null=True)

    class Meta:
        unique_together = ("invoice", "product", "description")

    @property
    def gross(self):
        return (self.qty or Decimal("0.00")) * (self.unit_price or Decimal("0.00"))

    @property
    def discount_amount(self):
        return (self.gross * (self.discount_pct or Decimal("0.00")) / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def line_total(self):
        return self.gross - self.discount_amount

    @property
    def tax_amount(self):
        rate = self.tax_code.rate if self.tax_code else Decimal("0.00")
        return self.line_total * rate


class GLAccount(models.Model):
    class Type(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        EQUITY = "EQUITY", "Equity"
        INCOME = "INCOME", "Income"
        COGS = "COGS", "Cost of goods sold"
        EXPENSE = "EXPENSE", "Expense"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=Type.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("tenant", "code")

    def __str__(self):
        return f"{self.code} - {self.name}"


class JournalEntry(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    entry_date = models.DateField(default=timezone.now)
    ref_type = models.CharField(max_length=50, blank=True, null=True)
    ref_id = models.CharField(max_length=100, blank=True, null=True)
    memo = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    posted_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    posted_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"JE {self.id}"

    @property
    def total_debit(self):
        return sum((l.debit for l in self.lines.all()), Decimal("0.00"))

    @property
    def total_credit(self):
        return sum((l.credit for l in self.lines.all()), Decimal("0.00"))


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, related_name="lines", on_delete=models.CASCADE)
    account = models.ForeignKey(GLAccount, on_delete=models.PROTECT)
    description = models.CharField(max_length=255, blank=True, null=True)
    debit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))


# ============================
# Payments (AR receipts / AP payments) + bank reconciliation
# ============================

class Payment(models.Model):
    class Direction(models.TextChoices):
        RECEIPT = "RECEIPT", "Customer receipt"   # money in
        PAYMENT = "PAYMENT", "Supplier payment"   # money out
        REFUND = "REFUND", "Customer refund"      # money out (back to a customer)

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"

    class Method(models.TextChoices):
        BANK = "BANK", "Bank transfer"
        CARD = "CARD", "Card"
        CASH = "CASH", "Cash"
        CHEQUE = "CHEQUE", "Cheque"
        OTHER = "OTHER", "Other"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    payment_date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.BANK)
    reference = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    # Bank reconciliation
    is_reconciled = models.BooleanField(default=False)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        party = self.customer or self.supplier
        return f"{self.get_direction_display()} {self.amount} ({party})"

    @property
    def party_name(self):
        if self.customer_id:
            return self.customer.name
        if self.supplier_id:
            return self.supplier.name
        return ""

    @property
    def allocated(self):
        return sum((a.amount for a in self.allocations.all()), Decimal("0.00"))

    @property
    def unallocated(self):
        return self.amount - self.allocated


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, related_name="allocations", on_delete=models.CASCADE)
    customer_invoice = models.ForeignKey(CustomerInvoice, on_delete=models.PROTECT, null=True, blank=True, related_name="payment_allocations")
    supplier_invoice = models.ForeignKey(SupplierInvoice, on_delete=models.PROTECT, null=True, blank=True, related_name="payment_allocations")
    amount = models.DecimalField(max_digits=12, decimal_places=2)


class Expense(models.Model):
    """A business cost the owner records directly (rent, fuel, software, ...),
    without a formal supplier bill. Posting it creates the double-entry:
    DR the chosen expense account (+ DR VAT input), CR Bank when paid now, or
    CR Accounts Payable when it is still owed."""
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"

    class Method(models.TextChoices):
        BANK = "BANK", "Bank transfer"
        CARD = "CARD", "Card"
        CASH = "CASH", "Cash"
        OTHER = "OTHER", "Other"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="expenses")
    expense_date = models.DateField(default=timezone.now)
    payee = models.CharField(max_length=200)  # merchant / who was paid
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses")
    category = models.ForeignKey(GLAccount, on_delete=models.PROTECT, related_name="expenses")  # an expense/COGS account
    description = models.CharField(max_length=255, blank=True, null=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, null=True, blank=True)
    paid = models.BooleanField(default=True)  # paid from bank now vs owed (AP)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.BANK)
    reference = models.CharField(max_length=100, blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(default=timezone.now)
    posted_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Expense {self.payee} {self.total}"

    @property
    def tax_amount(self):
        rate = self.tax_code.rate if self.tax_code else Decimal("0.00")
        return (self.net_amount or Decimal("0.00")) * rate

    @property
    def total(self):
        return (self.net_amount or Decimal("0.00")) + self.tax_amount


class CreditNote(models.Model):
    """A credit note: a sales credit reduces what a customer owes (or refunds
    them); a purchase credit reduces what we owe a supplier. Posting it creates
    the reversing double-entry and, when linked to an invoice, reduces that
    invoice's outstanding balance."""
    class Kind(models.TextChoices):
        SALES = "SALES", "Sales credit (to customer)"
        PURCHASE = "PURCHASE", "Purchase credit (from supplier)"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="credit_notes")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    credit_note_number = models.CharField(max_length=50)
    credit_note_date = models.DateField(default=timezone.now)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True, related_name="credit_notes")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="credit_notes")
    customer_invoice = models.ForeignKey(CustomerInvoice, on_delete=models.PROTECT, null=True, blank=True, related_name="credit_notes")
    supplier_invoice = models.ForeignKey(SupplierInvoice, on_delete=models.PROTECT, null=True, blank=True, related_name="credit_notes")
    reason = models.CharField(max_length=255, blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(default=timezone.now)
    posted_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("tenant", "credit_note_number")

    def __str__(self):
        return self.credit_note_number

    @property
    def party_name(self):
        return (self.customer.name if self.customer_id else
                self.supplier.name if self.supplier_id else "")

    @property
    def subtotal(self):
        return sum((l.line_total for l in self.lines.all()), Decimal("0.00"))

    @property
    def tax_total(self):
        return sum((l.tax_amount for l in self.lines.all()), Decimal("0.00"))

    @property
    def total(self):
        return self.subtotal + self.tax_total


class CreditNoteLine(models.Model):
    credit_note = models.ForeignKey(CreditNote, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, blank=True, null=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.00"))
    unit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, blank=True, null=True)
    # For purchase credits, the account to credit back (expense / inventory).
    # For sales credits, left blank -> Sales Revenue.
    account = models.ForeignKey(GLAccount, on_delete=models.PROTECT, blank=True, null=True, related_name="credit_note_lines")

    @property
    def line_total(self):
        return (self.qty or Decimal("0.00")) * (self.unit_amount or Decimal("0.00"))

    @property
    def tax_amount(self):
        rate = self.tax_code.rate if self.tax_code else Decimal("0.00")
        return self.line_total * rate


class BankTransaction(models.Model):
    """A line from the bank statement (imported or entered by hand). Positive
    amount = money in, negative = money out. Reconciliation matches each line to
    an internal record (a payment or a paid expense) and marks it reconciled;
    it is preparation only and posts nothing to the ledger."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="bank_transactions")
    txn_date = models.DateField(default=timezone.now)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # signed: +in / -out
    reference = models.CharField(max_length=100, blank=True, null=True)

    matched_payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_transactions")
    matched_expense = models.ForeignKey(Expense, on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_transactions")
    is_reconciled = models.BooleanField(default=False)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-txn_date", "-id"]

    def __str__(self):
        return f"{self.txn_date} {self.description} {self.amount}"

    @property
    def direction(self):
        return "IN" if self.amount >= Decimal("0.00") else "OUT"

    @property
    def matched_label(self):
        if self.matched_payment_id:
            return f"Payment: {self.matched_payment.party_name} {self.matched_payment.amount}"
        if self.matched_expense_id:
            return f"Expense: {self.matched_expense.payee} {self.matched_expense.total}"
        return ""


# ============================
# VAT return (UK MTD 9-box)
# ============================

class VatReturn(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    period_from = models.DateField()
    period_to = models.DateField()

    # The nine HMRC boxes (1-5 to the penny, 6-9 whole pounds in real returns).
    box1_vat_due_sales = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box2_vat_due_acquisitions = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box3_total_vat_due = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box4_vat_reclaimed = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box5_net_vat = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box6_total_sales_ex_vat = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box7_total_purchases_ex_vat = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box8_eu_supplies = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    box9_eu_acquisitions = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    hmrc_reference = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "period_from", "period_to")
        ordering = ["-period_to"]

    def __str__(self):
        return f"VAT {self.period_from} to {self.period_to}"
