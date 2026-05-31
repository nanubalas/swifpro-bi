from django.db import migrations


def add_bank_account(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    GLAccount = apps.get_model("core", "GLAccount")
    for tenant in Tenant.objects.all():
        GLAccount.objects.get_or_create(
            tenant=tenant, code="1050",
            defaults={"name": "Bank", "type": "ASSET", "is_active": True},
        )


def remove_bank_account(apps, schema_editor):
    GLAccount = apps.get_model("core", "GLAccount")
    GLAccount.objects.filter(code="1050", name="Bank").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_payment_paymentallocation"),
    ]
    operations = [
        migrations.RunPython(add_bank_account, remove_bank_account),
    ]
