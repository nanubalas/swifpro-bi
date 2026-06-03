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
        THREEPL = "THREEPL", "3PL"
        STORE = "STORE", "Store"
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
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")  # NEW

    class Meta:
        unique_together = ("tenant", "name")

    def __str__(self):
        return self.name


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


class Product(models.Model):
    class CostMethod(models.TextChoices):
        FIFO = "FIFO", "FIFO"
        AVERAGE = "AVERAGE", "Average"
        STANDARD = "STANDARD", "Standard"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    sku = models.CharField(max_length=64)
    name = models.CharField(max_length=255)

    # Variants (keep SKU-level records; link variants to a parent 'style' SKU)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, blank=True, null=True, related_name="variants")
    variant_name = models.CharField(max_length=255, blank=True, null=True)
    option1 = models.CharField(max_length=64, blank=True, null=True)  # e.g., Size=M
    option2 = models.CharField(max_length=64, blank=True, null=True)  # e.g., Color=Black
    option3 = models.CharField(max_length=64, blank=True, null=True)

    # UOM
    base_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, blank=True, null=True)
    uom = models.CharField(max_length=32, default="each")  # legacy display / quick entry

    # Costing
    cost_method = models.CharField(max_length=20, choices=CostMethod.choices, default=CostMethod.AVERAGE)
    standard_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    # Running moving-average cost, maintained on inbound movements.
    average_cost = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))

    class Meta:
        unique_together = ("tenant", "sku")

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
        RECEIVED = "RECEIVED", "Received"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    po_number = models.CharField(max_length=40)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
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


class PurchaseOrderLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    ordered_qty = models.DecimalField(max_digits=12, decimal_places=2)
    received_qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        unique_together = ("po", "product")

    @property
    def open_qty(self):
        return self.ordered_qty - self.received_qty

    @property
    def line_total(self):
        return self.ordered_qty * self.unit_cost



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
        RECEIVE = "RECEIVE", "Receive"
        SALE = "SALE", "Sale"
        TRANSFER_IN = "TRANSFER_IN", "Transfer In"
        TRANSFER_OUT = "TRANSFER_OUT", "Transfer Out"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        RETURN = "RETURN", "Return"
        RESERVATION = "RESERVATION", "Reservation"
        RELEASE = "RELEASE", "Release"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
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
    def outstanding(self):
        return self.total - self.amount_paid


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
# Finance (VAT + AR + GL)
# ============================

class TaxCode(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    rate = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0000"))  # e.g. 0.2000 for 20%
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("tenant", "code")

    def __str__(self):
        return f"{self.code} ({self.rate})"


class Customer(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    vat_number = models.CharField(max_length=50, blank=True, null=True)
    billing_address = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "name")

    def __str__(self):
        return self.name


class CustomerInvoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"
        PAID = "PAID", "Paid"
        VOID = "VOID", "Void"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    invoice_number = models.CharField(max_length=50)
    invoice_date = models.DateField(default=timezone.now)
    due_date = models.DateField(blank=True, null=True)
    currency_code = models.CharField(max_length=3, default="GBP")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now)
    issued_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ("tenant", "invoice_number")

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
    def outstanding(self):
        return self.total - self.amount_paid


class CustomerInvoiceLine(models.Model):
    invoice = models.ForeignKey(CustomerInvoice, related_name="lines", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, blank=True, null=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    qty = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("1.00"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, blank=True, null=True)

    class Meta:
        unique_together = ("invoice", "product", "description")

    @property
    def line_total(self):
        return (self.qty or Decimal("0.00")) * (self.unit_price or Decimal("0.00"))

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
