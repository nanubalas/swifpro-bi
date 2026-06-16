from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User
from core.models import (
    UserProfile, OrgMembership, AuditLog, AccessRequest, UserPermissionOverride,
    Tenant, Location, Supplier, Product,
    PurchaseOrder, PurchaseOrderLine,
    Shipment, ShipmentEvent,
    InventoryBalance, InventoryMovement,
    ChannelConnection, SyncRun, ChannelSnapshot, ChannelOrder,
    UnitOfMeasure, UOMConversion, ProductBarcode, BillOfMaterials, BillOfMaterialsLine,
    BillOfMaterialsLinePlacement,
    TaxCode, Customer, CustomerInvoice, CustomerInvoiceLine,
    GLAccount, JournalEntry, JournalLine, Expense, CreditNote, CreditNoteLine,
    BankTransaction
)

admin.site.register(Expense)
admin.site.register(CreditNote)
admin.site.register(BankTransaction)
from core.models import SalesQuote, CustomerOrder, ProductCategory, StockAdjustment
admin.site.register(SalesQuote)
admin.site.register(CustomerOrder)
admin.site.register(ProductCategory)
admin.site.register(StockAdjustment)


# --- BOM (header -> lines -> reference designators / placements) ---
class BillOfMaterialsLineInline(admin.TabularInline):
    model = BillOfMaterialsLine
    extra = 0


@admin.register(BillOfMaterials)
class BillOfMaterialsAdmin(admin.ModelAdmin):
    list_display = ("product", "name", "output_qty", "is_active")
    inlines = [BillOfMaterialsLineInline]


class BillOfMaterialsLinePlacementInline(admin.TabularInline):
    model = BillOfMaterialsLinePlacement
    extra = 0


@admin.register(BillOfMaterialsLine)
class BillOfMaterialsLineAdmin(admin.ModelAdmin):
    list_display = ("bom", "line_no", "component", "qty", "notes")
    inlines = [BillOfMaterialsLinePlacementInline]

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0


class OrgMembershipInline(admin.TabularInline):
    model = OrgMembership
    extra = 0


class UserAdmin(DjangoUserAdmin):
    inlines = [UserProfileInline, OrgMembershipInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
admin.site.register(UserProfile)


@admin.register(OrgMembership)
class OrgMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "is_default")
    list_filter = ("role", "tenant")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "username", "tenant", "path", "ip")
    list_filter = ("action", "tenant")
    search_fields = ("username", "detail", "path")


@admin.register(UserPermissionOverride)
class UserPermissionOverrideAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "permission", "effect", "created_at")
    list_filter = ("effect", "tenant", "permission")
    search_fields = ("user__username", "permission")


@admin.register(AccessRequest)
class AccessRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "name", "email", "team", "status", "reviewed_by")
    list_filter = ("status",)
    search_fields = ("name", "email", "employee_id")

admin.site.register(Tenant)
admin.site.register(Location)
admin.site.register(Supplier)
admin.site.register(Product)
admin.site.register(PurchaseOrder)
admin.site.register(PurchaseOrderLine)
admin.site.register(Shipment)
admin.site.register(ShipmentEvent)
admin.site.register(InventoryBalance)
admin.site.register(InventoryMovement)
admin.site.register(ChannelConnection)
admin.site.register(SyncRun)
admin.site.register(ChannelSnapshot)
admin.site.register(ChannelOrder)

admin.site.register(UnitOfMeasure)
admin.site.register(UOMConversion)
admin.site.register(ProductBarcode)
# BillOfMaterials / BillOfMaterialsLine are registered above with inlines.

admin.site.register(TaxCode)
admin.site.register(Customer)
admin.site.register(CustomerInvoice)
admin.site.register(CustomerInvoiceLine)
admin.site.register(GLAccount)
admin.site.register(JournalEntry)
admin.site.register(JournalLine)

from core.models import Notification, EmailLog, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "recipient", "category", "title", "is_read", "tenant")
    list_filter = ("category", "is_read")
    search_fields = ("title", "message", "recipient__username")


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "to_email", "subject", "category", "status", "tenant")
    list_filter = ("category", "status")
    search_fields = ("to_email", "subject")


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "category", "in_app", "email")
    list_filter = ("category", "in_app", "email")


# --- MRP (Material Requirements Planning) ---
from core.models import (
    ItemSitePlanning, MRPRun, MRPDemand, MRPSupply,
    MRPPlannedOrder, MRPPegging, MRPException,
)


@admin.register(ItemSitePlanning)
class ItemSitePlanningAdmin(admin.ModelAdmin):
    list_display = ("product", "site", "source_type", "default_supplier",
                    "safety_stock_qty", "min_order_qty", "lead_time_days",
                    "lot_sizing_method", "mrp_enabled", "is_active")
    list_filter = ("source_type", "lot_sizing_method", "mrp_enabled", "is_active", "site")
    search_fields = ("product__sku", "product__name", "site__name")
    raw_id_fields = ("product", "default_supplier")


@admin.register(MRPRun)
class MRPRunAdmin(admin.ModelAdmin):
    list_display = ("run_number", "run_type", "site_scope", "status",
                    "planning_start_date", "planning_end_date", "started_by", "created_at")
    list_filter = ("status", "run_type")
    search_fields = ("run_number",)
    date_hierarchy = "created_at"


@admin.register(MRPPlannedOrder)
class MRPPlannedOrderAdmin(admin.ModelAdmin):
    list_display = ("planned_order_number", "mrp_run", "product", "site", "source_type",
                    "quantity", "required_date", "status", "exception_level")
    list_filter = ("source_type", "status", "exception_level")
    search_fields = ("planned_order_number", "product__sku")
    raw_id_fields = ("mrp_run", "product", "supplier", "parent_planned_order")


@admin.register(MRPException)
class MRPExceptionAdmin(admin.ModelAdmin):
    list_display = ("exception_code", "severity", "mrp_run", "product", "site", "is_resolved", "created_at")
    list_filter = ("exception_code", "severity", "is_resolved")
    search_fields = ("product__sku", "message")
    raw_id_fields = ("mrp_run", "product", "planned_order")


admin.site.register(MRPDemand)
admin.site.register(MRPSupply)
admin.site.register(MRPPegging)


# --- MRP Phase 5: Work Orders (planning level) ---
from core.models import WorkOrder, WorkOrderMaterial


class WorkOrderMaterialInline(admin.TabularInline):
    model = WorkOrderMaterial
    extra = 0
    raw_id_fields = ("component", "bom_line", "source_mrp_demand")


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("work_order_number", "product", "site", "quantity", "status",
                    "required_date", "created_at")
    list_filter = ("status",)
    search_fields = ("work_order_number", "product__sku")
    raw_id_fields = ("product", "source_mrp_planned_order", "created_by")
    inlines = [WorkOrderMaterialInline]


# --- MRP Phase 7: Manufacturing accounting profile ---
from core.models import ManufacturingAccountingProfile


@admin.register(ManufacturingAccountingProfile)
class ManufacturingAccountingProfileAdmin(admin.ModelAdmin):
    list_display = ("tenant", "site", "is_default", "is_active",
                    "raw_material_inventory_account", "wip_account",
                    "finished_goods_inventory_account", "manufacturing_variance_account")
    list_filter = ("is_default", "is_active")
    raw_id_fields = ("raw_material_inventory_account", "wip_account",
                     "finished_goods_inventory_account", "manufacturing_variance_account")


from core.models import ForecastVersion, ForecastLine


class ForecastLineInline(admin.TabularInline):
    model = ForecastLine
    extra = 0
    raw_id_fields = ("product", "site")


@admin.register(ForecastVersion)
class ForecastVersionAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "tenant", "status", "forecast_type",
                    "consumption_method", "is_default", "start_date", "end_date")
    list_filter = ("status", "forecast_type", "consumption_method", "is_default")
    search_fields = ("code", "name")
    inlines = [ForecastLineInline]


@admin.register(ForecastLine)
class ForecastLineAdmin(admin.ModelAdmin):
    list_display = ("forecast_version", "product", "site", "forecast_date",
                    "bucket_type", "quantity", "consumed_quantity", "remaining_quantity")
    list_filter = ("bucket_type", "source")
    raw_id_fields = ("product", "site", "forecast_version")


from core.models import WorkCentre, RoutingHeader, RoutingOperation, WorkOrderOperation


@admin.register(WorkCentre)
class WorkCentreAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "tenant", "site", "capacity_hours_per_day",
                    "efficiency_percent", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    raw_id_fields = ("site",)


class RoutingOperationInline(admin.TabularInline):
    model = RoutingOperation
    extra = 0
    raw_id_fields = ("work_centre",)


@admin.register(RoutingHeader)
class RoutingHeaderAdmin(admin.ModelAdmin):
    list_display = ("routing_code", "product", "tenant", "site", "status", "is_default",
                    "effective_from", "effective_to")
    list_filter = ("status", "is_default")
    search_fields = ("routing_code", "product__sku")
    raw_id_fields = ("product", "site")
    inlines = [RoutingOperationInline]


@admin.register(WorkOrderOperation)
class WorkOrderOperationAdmin(admin.ModelAdmin):
    list_display = ("work_order", "operation_sequence", "operation_name", "work_centre",
                    "planned_hours", "planned_start", "planned_end", "status")
    list_filter = ("status",)
    raw_id_fields = ("work_order", "work_centre", "source_routing_operation")
