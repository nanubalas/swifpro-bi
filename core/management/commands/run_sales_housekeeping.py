"""Expire stale quotes and generate due recurring invoices for all tenants.

Schedule this daily (cron / Task Scheduler):
    python manage.py run_sales_housekeeping
"""
from django.core.management.base import BaseCommand

from core.services import housekeeping


class Command(BaseCommand):
    help = "Expire overdue quotes and generate any due recurring invoices."

    def handle(self, *args, **options):
        results = housekeeping.run_all()
        expired = sum((r["expired"] for r in results.values() if r), 0)
        generated = sum((r["generated"] for r in results.values() if r), 0)
        self.stdout.write(self.style.SUCCESS(
            f"Housekeeping done: {expired} quote(s) expired, {generated} recurring invoice(s) generated."))
