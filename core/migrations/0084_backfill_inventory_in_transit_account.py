"""Seed the Inventory In Transit (1010) GL account for existing tenants."""
from django.db import migrations


def add_in_transit_account(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    GLAccount = apps.get_model("core", "GLAccount")
    for tenant in Tenant.objects.all():
        GLAccount.objects.get_or_create(
            tenant=tenant, code="1010",
            defaults={"name": "Inventory In Transit", "type": "ASSET"})


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0083_transfer_in_transit"),
    ]

    operations = [
        migrations.RunPython(add_in_transit_account, migrations.RunPython.noop),
    ]
