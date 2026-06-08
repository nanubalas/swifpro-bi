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
admin.site.register(BillOfMaterials)
admin.site.register(BillOfMaterialsLine)

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
