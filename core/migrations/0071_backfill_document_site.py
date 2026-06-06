from django.db import migrations


def backfill_document_site(apps, schema_editor):
    """Set site_id on existing sales orders, invoices and POs from their
    location / receiving_location's site, falling back to the company's default
    Site so no document is left site-less."""
    Tenant = apps.get_model("core", "Tenant")
    Site = apps.get_model("core", "Site")
    CustomerOrder = apps.get_model("core", "CustomerOrder")
    CustomerInvoice = apps.get_model("core", "CustomerInvoice")
    PurchaseOrder = apps.get_model("core", "PurchaseOrder")

    default_by_tenant = {}

    def default_site(tenant_id):
        if tenant_id not in default_by_tenant:
            default_by_tenant[tenant_id] = (
                Site.objects.filter(tenant_id=tenant_id, is_default=True).order_by("id").first()
                or Site.objects.filter(tenant_id=tenant_id).order_by("id").first())
        return default_by_tenant[tenant_id]

    for model, loc_attr in ((CustomerOrder, "location"), (CustomerInvoice, "location"),
                            (PurchaseOrder, "receiving_location")):
        for row in model.objects.filter(site__isnull=True).iterator():
            loc = getattr(row, loc_attr, None)
            site_id = getattr(loc, "site_id", None) if loc else None
            if not site_id:
                d = default_site(row.tenant_id)
                site_id = d.id if d else None
            if site_id:
                model.objects.filter(pk=row.pk).update(site_id=site_id)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0070_customerinvoice_site_customerorder_site_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_document_site, noop),
    ]
