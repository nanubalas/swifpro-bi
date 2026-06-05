from django.db import migrations

NEW_ACCOUNTS = [
    ("6150", "Repairs & Maintenance"),
    ("6250", "Insurance"),
    ("6450", "Meals & Entertainment"),
]


def add_accounts(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    GLAccount = apps.get_model("core", "GLAccount")
    for tenant in Tenant.objects.all():
        for code, name in NEW_ACCOUNTS:
            GLAccount.objects.get_or_create(
                tenant=tenant, code=code,
                defaults={"name": name, "type": "EXPENSE", "is_active": True},
            )


def remove_accounts(apps, schema_editor):
    GLAccount = apps.get_model("core", "GLAccount")
    for code, name in NEW_ACCOUNTS:
        GLAccount.objects.filter(code=code, name=name).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0049_expense_receipt_expense_reimbursable"),
    ]
    operations = [
        migrations.RunPython(add_accounts, remove_accounts),
    ]
