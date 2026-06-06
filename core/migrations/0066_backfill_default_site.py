from django.db import migrations


def backfill_default_site(apps, schema_editor):
    """Ensure every company has a default Site, and attach any inventory
    Location that has no Site to it. Makes Company -> Site -> Location whole
    for existing data without a NOT NULL change."""
    Tenant = apps.get_model("core", "Tenant")
    Site = apps.get_model("core", "Site")
    Location = apps.get_model("core", "Location")
    for tenant in Tenant.objects.all():
        site = (Site.objects.filter(tenant=tenant, is_default=True).order_by("id").first()
                or Site.objects.filter(tenant=tenant).order_by("id").first())
        if site is None:
            site = Site.objects.create(
                tenant=tenant, name="Main Site", site_type="operating_site",
                is_default=True, is_active=True,
            )
        elif not site.is_default:
            site.is_default = True
            site.save(update_fields=["is_default"])
        Location.objects.filter(tenant=tenant, site__isnull=True).update(site=site)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0065_alter_site_options_site_is_default_site_manager_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_default_site, noop),
    ]
