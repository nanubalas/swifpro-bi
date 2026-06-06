from django.db import migrations, models
import django.db.models.deletion


def backfill_sales_location(apps, schema_editor):
    """Stamp existing sales orders and invoices with their org's default stock
    location (first stock-holding warehouse) so location-scoped fulfilment and
    reporting work for historical documents."""
    Tenant = apps.get_model("core", "Tenant")
    Location = apps.get_model("core", "Location")
    CustomerOrder = apps.get_model("core", "CustomerOrder")
    CustomerInvoice = apps.get_model("core", "CustomerInvoice")
    for tenant in Tenant.objects.all():
        loc = (Location.objects.filter(tenant=tenant, type="WAREHOUSE", is_active=True, holds_stock=True)
               .order_by("id").first()
               or Location.objects.filter(tenant=tenant).order_by("id").first())
        if loc is None:
            continue
        CustomerOrder.objects.filter(tenant=tenant, location__isnull=True).update(location=loc)
        CustomerInvoice.objects.filter(tenant=tenant, location__isnull=True).update(location=loc)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0061_department_orgmembership_department"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerorder",
            name="location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="customer_orders", to="core.location"),
        ),
        migrations.AddField(
            model_name="customerinvoice",
            name="location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="customer_invoices", to="core.location"),
        ),
        migrations.RunPython(backfill_sales_location, noop),
    ]
