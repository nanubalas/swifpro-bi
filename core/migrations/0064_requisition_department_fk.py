from django.db import migrations, models
import django.db.models.deletion


def text_to_department(apps, schema_editor):
    """Convert each requisition's free-text department into a structured
    Department (created per tenant on first use) and link the FK."""
    PurchaseRequisition = apps.get_model("core", "PurchaseRequisition")
    Department = apps.get_model("core", "Department")
    cache = {}  # (tenant_id, name_lower) -> Department
    for req in PurchaseRequisition.objects.exclude(department_text__isnull=True).exclude(department_text=""):
        name = req.department_text.strip()
        if not name:
            continue
        key = (req.tenant_id, name.lower())
        dept = cache.get(key)
        if dept is None:
            dept = (Department.objects.filter(tenant_id=req.tenant_id, name__iexact=name).first()
                    or Department.objects.create(tenant_id=req.tenant_id, name=name, is_active=True))
            cache[key] = dept
        req.department = dept
        req.save(update_fields=["department"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0063_purchaseorder_receiving_location"),
    ]

    operations = [
        # Preserve the existing free-text values under a temporary name...
        migrations.RenameField(
            model_name="purchaserequisition",
            old_name="department",
            new_name="department_text",
        ),
        # ...add the structured FK...
        migrations.AddField(
            model_name="purchaserequisition",
            name="department",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="requisitions", to="core.department"),
        ),
        # ...migrate text -> Department, then drop the text column.
        migrations.RunPython(text_to_department, noop),
        migrations.RemoveField(model_name="purchaserequisition", name="department_text"),
    ]
