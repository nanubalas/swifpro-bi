from django.db import migrations


def add_accruals_account(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    GLAccount = apps.get_model("core", "GLAccount")
    for tenant in Tenant.objects.all():
        GLAccount.objects.get_or_create(
            tenant=tenant, code="2150",
            defaults={"name": "Accruals (Landed Costs)", "type": "LIABILITY", "is_active": True},
        )


def remove_accruals_account(apps, schema_editor):
    GLAccount = apps.get_model("core", "GLAccount")
    GLAccount.objects.filter(code="2150", name="Accruals (Landed Costs)").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_inventorycostlayer"),
    ]
    operations = [
        migrations.RunPython(add_accruals_account, remove_accruals_account),
    ]
