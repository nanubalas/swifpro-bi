from django.db import migrations, models
import django.db.models.deletion


def backfill_receiving_location(apps, schema_editor):
    """Stamp existing purchase orders with their org's default stock location so
    receiving lands in a structured location rather than relying on free text."""
    Tenant = apps.get_model("core", "Tenant")
    Location = apps.get_model("core", "Location")
    PurchaseOrder = apps.get_model("core", "PurchaseOrder")
    for tenant in Tenant.objects.all():
        loc = (Location.objects.filter(tenant=tenant, type="WAREHOUSE", is_active=True, holds_stock=True)
               .order_by("id").first()
               or Location.objects.filter(tenant=tenant).order_by("id").first())
        if loc is None:
            continue
        PurchaseOrder.objects.filter(tenant=tenant, receiving_location__isnull=True).update(receiving_location=loc)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0062_customerorder_location_customerinvoice_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="receiving_location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="purchase_orders", to="core.location"),
        ),
        migrations.RunPython(backfill_receiving_location, noop),
    ]
