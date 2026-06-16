"""Process due, active scheduled MRP report exports (Phase 17).

    python manage.py run_scheduled_report_exports

Finds scheduled exports whose next_run_at has passed, generates each (CSV/XLSX)
and emails it to recipients when an email backend is configured. Safe to run on
a cron / scheduler; failures are recorded on the export, never raised.
"""
from django.core.management.base import BaseCommand

from core.services.mrp import scheduled_export


class Command(BaseCommand):
    help = "Generate and send any due scheduled MRP report exports."

    def handle(self, *args, **opts):
        processed, failed = scheduled_export.process_due_exports()
        self.stdout.write(self.style.SUCCESS(
            f"Scheduled exports processed: {processed} ({failed} failed)."))
