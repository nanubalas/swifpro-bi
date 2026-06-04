from django.db import migrations


def add_account(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    GLAccount = apps.get_model("core", "GLAccount")
    for tenant in Tenant.objects.all():
        GLAccount.objects.get_or_create(
            tenant=tenant, code="5200",
            defaults={"name": "Inventory Adjustments / Shrinkage", "type": "EXPENSE", "is_active": True},
        )


def remove_account(apps, schema_editor):
    GLAccount = apps.get_model("core", "GLAccount")
    GLAccount.objects.filter(code="5200", name="Inventory Adjustments / Shrinkage").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_purchaserequisition_purchaserequisitionline"),
    ]
    operations = [
        migrations.RunPython(add_account, remove_account),
    ]
