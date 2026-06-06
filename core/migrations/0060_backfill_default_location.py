from django.db import migrations


def backfill_default_location(apps, schema_editor):
    """Give every existing organisation with no location a default 'Main Location'
    so stock, POs and sales work without a manual setup step."""
    Tenant = apps.get_model("core", "Tenant")
    Location = apps.get_model("core", "Location")
    for tenant in Tenant.objects.all():
        if not Location.objects.filter(tenant=tenant).exists():
            Location.objects.create(
                tenant=tenant, name="Main Location",
                type="WAREHOUSE", holds_stock=True, is_active=True,
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0059_customerinvoice_is_intercompany_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_default_location, noop),
    ]
