from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User
from core.models import (
    UserProfile,
    Tenant, Location, Supplier, Product,
    PurchaseOrder, PurchaseOrderLine,
    Shipment, ShipmentEvent,
    InventoryBalance, InventoryMovement,
    ChannelConnection, SyncRun, ChannelSnapshot, ChannelOrder,
    UnitOfMeasure, UOMConversion, ProductBarcode, BillOfMaterials, BillOfMaterialsLine,
    TaxCode, Customer, CustomerInvoice, CustomerInvoiceLine,
    GLAccount, JournalEntry, JournalLine
)

class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    extra = 0


class UserAdmin(DjangoUserAdmin):
    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
admin.site.register(UserProfile)

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
