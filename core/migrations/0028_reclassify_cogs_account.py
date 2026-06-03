from django.db import migrations


def set_cogs_type(apps, schema_editor):
    GLAccount = apps.get_model("core", "GLAccount")
    # The Cost of Goods Sold account (code 5000) becomes its own COGS type so
    # the P&L can show gross profit. Created originally as EXPENSE.
    GLAccount.objects.filter(code="5000", type="EXPENSE").update(type="COGS")


def revert(apps, schema_editor):
    GLAccount = apps.get_model("core", "GLAccount")
    GLAccount.objects.filter(code="5000", type="COGS").update(type="EXPENSE")


class Migration(migrations.Migration):
    dependencies = [("core", "0027_alter_glaccount_type")]
    operations = [migrations.RunPython(set_cogs_type, revert)]
