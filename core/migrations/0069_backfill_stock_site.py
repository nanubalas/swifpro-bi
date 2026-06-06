from django.db import migrations


def backfill_stock_site(apps, schema_editor):
    """Set site_id on existing stock balances and movements from their
    location's site (every Location now belongs to a Site)."""
    InventoryBalance = apps.get_model("core", "InventoryBalance")
    InventoryMovement = apps.get_model("core", "InventoryMovement")
    for model in (InventoryBalance, InventoryMovement):
        for row in model.objects.filter(site__isnull=True).select_related("location").iterator():
            site_id = getattr(row.location, "site_id", None)
            if site_id:
                model.objects.filter(pk=row.pk).update(site_id=site_id)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0068_inventorybalance_site_inventorymovement_site"),
    ]

    operations = [
        migrations.RunPython(backfill_stock_site, noop),
    ]
