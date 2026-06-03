from django.db import migrations

NEW_ACCOUNTS = [
    ("6000", "General Expenses", "EXPENSE"),
    ("6100", "Rent & Rates", "EXPENSE"),
    ("6200", "Utilities", "EXPENSE"),
    ("6300", "Office & Admin", "EXPENSE"),
    ("6400", "Travel & Subsistence", "EXPENSE"),
    ("6500", "Marketing", "EXPENSE"),
    ("6600", "Professional Fees", "EXPENSE"),
    ("6700", "Software & Subscriptions", "EXPENSE"),
    ("6900", "Other Expenses", "EXPENSE"),
]


def add_expense_accounts(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    GLAccount = apps.get_model("core", "GLAccount")
    for tenant in Tenant.objects.all():
        for code, name, acc_type in NEW_ACCOUNTS:
            GLAccount.objects.get_or_create(
                tenant=tenant, code=code,
                defaults={"name": name, "type": acc_type},
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0023_expense")]
    operations = [migrations.RunPython(add_expense_accounts, noop)]
