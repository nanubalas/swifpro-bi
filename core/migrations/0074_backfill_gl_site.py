from django.db import migrations


def backfill_gl_site(apps, schema_editor):
    """Attribute existing journal entries (and expenses) to a Site, derived from
    their source document, so historical P&L can be filtered by site. Entries
    with no site-bearing source (payments, VAT, opening balances) stay null =
    company-level."""
    Expense = apps.get_model("core", "Expense")
    JournalEntry = apps.get_model("core", "JournalEntry")
    CustomerInvoice = apps.get_model("core", "CustomerInvoice")
    SupplierInvoice = apps.get_model("core", "SupplierInvoice")
    StockAdjustment = apps.get_model("core", "StockAdjustment")
    Site = apps.get_model("core", "Site")

    # Expenses: default to the company's default site.
    default_by_tenant = {}

    def default_site(tenant_id):
        if tenant_id not in default_by_tenant:
            default_by_tenant[tenant_id] = (
                Site.objects.filter(tenant_id=tenant_id, is_default=True).order_by("id").first()
                or Site.objects.filter(tenant_id=tenant_id).order_by("id").first())
        return default_by_tenant[tenant_id]

    for e in Expense.objects.filter(site__isnull=True).iterator():
        d = default_site(e.tenant_id)
        if d:
            Expense.objects.filter(pk=e.pk).update(site_id=d.id)

    # Build ref lookups per tenant.
    ar_site = {}   # (tenant, invoice_number) -> site_id
    for inv in CustomerInvoice.objects.exclude(site__isnull=True).values("tenant_id", "invoice_number", "site_id"):
        ar_site[(inv["tenant_id"], inv["invoice_number"])] = inv["site_id"]
    ap_site = {}
    for inv in SupplierInvoice.objects.values("tenant_id", "invoice_number", "po__site_id"):
        if inv["po__site_id"]:
            ap_site[(inv["tenant_id"], inv["invoice_number"])] = inv["po__site_id"]
    adj_site = {}  # (tenant, str(id)) -> location.site_id
    for a in StockAdjustment.objects.values("tenant_id", "id", "location__site_id"):
        if a["location__site_id"]:
            adj_site[(a["tenant_id"], str(a["id"]))] = a["location__site_id"]
    exp_site = {}  # (tenant, str(id)) -> expense.site_id (now set above)
    for e in Expense.objects.exclude(site__isnull=True).values("tenant_id", "id", "site_id"):
        exp_site[(e["tenant_id"], str(e["id"]))] = e["site_id"]

    for je in JournalEntry.objects.filter(site__isnull=True).iterator():
        key = (je.tenant_id, je.ref_id)
        site_id = None
        if je.ref_type in ("AR_INVOICE", "COGS"):
            site_id = ar_site.get(key)
        elif je.ref_type == "AP_INVOICE":
            site_id = ap_site.get(key)
        elif je.ref_type == "STOCK_ADJ":
            site_id = adj_site.get(key)
        elif je.ref_type == "EXPENSE":
            site_id = exp_site.get(key)
        if site_id:
            JournalEntry.objects.filter(pk=je.pk).update(site_id=site_id)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0073_expense_site_journalentry_site"),
    ]

    operations = [
        migrations.RunPython(backfill_gl_site, noop),
    ]
