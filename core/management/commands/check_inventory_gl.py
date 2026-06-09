"""Flag tenants whose inventory subledger and GL control account have drifted.

Schedule this (cron / Task Scheduler), e.g. daily:
    python manage.py check_inventory_gl
    python manage.py check_inventory_gl --tolerance 0.50
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from core.services.reports import check_inventory_gl_variance


class Command(BaseCommand):
    help = "Report any tenant whose inventory subledger differs from the GL control account."

    def add_arguments(self, parser):
        parser.add_argument("--tolerance", default="0.01",
                            help="Absolute variance tolerance (default 0.01).")

    def handle(self, *args, **options):
        tolerance = Decimal(str(options["tolerance"]))
        flagged = check_inventory_gl_variance(tolerance=tolerance)
        if not flagged:
            self.stdout.write(self.style.SUCCESS("Inventory GL reconciliation clean for all tenants."))
            return
        for rec in flagged:
            self.stdout.write(self.style.ERROR(
                f"[{rec['tenant'].name}] inventory subledger {rec['closing_subledger']} "
                f"vs GL {rec['account_code']} {rec['closing_gl']} -> variance {rec['variance']}"))
        self.stdout.write(self.style.WARNING(f"{len(flagged)} tenant(s) out of tolerance."))
