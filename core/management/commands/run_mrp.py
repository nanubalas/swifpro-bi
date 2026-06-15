"""Run an MRP run from the command line (safe alternative to the UI action).

Examples:
    python manage.py run_mrp --run-id 12
    python manage.py run_mrp --tenant "MRP Co" --run-number MRP-20260615-...
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import MRPRun
from core.services.mrp import run_mrp


class Command(BaseCommand):
    help = "Execute an existing MRP run (Phase 2: BUY items)."

    def add_arguments(self, parser):
        parser.add_argument("--run-id", type=int, help="MRPRun primary key.")
        parser.add_argument("--tenant", help="Tenant name (with --run-number).")
        parser.add_argument("--run-number", help="MRP run number (with --tenant).")

    def handle(self, *args, **opts):
        run = self._resolve_run(opts)
        self.stdout.write(f"Running MRP for {run.run_number} (tenant {run.tenant.name}) ...")
        run_mrp(run)
        self.stdout.write(self.style.SUCCESS(
            f"{run.run_number}: {run.status} - "
            f"{run.planned_orders.count()} planned order(s), "
            f"{run.exceptions.count()} exception(s)."))

    def _resolve_run(self, opts):
        if opts.get("run_id"):
            try:
                return MRPRun.objects.select_related("tenant").get(id=opts["run_id"])
            except MRPRun.DoesNotExist:
                raise CommandError(f"No MRPRun with id {opts['run_id']}.")
        if opts.get("tenant") and opts.get("run_number"):
            try:
                return MRPRun.objects.select_related("tenant").get(
                    tenant__name=opts["tenant"], run_number=opts["run_number"])
            except MRPRun.DoesNotExist:
                raise CommandError("No matching MRPRun for that tenant/run-number.")
        raise CommandError("Provide --run-id, or both --tenant and --run-number.")
