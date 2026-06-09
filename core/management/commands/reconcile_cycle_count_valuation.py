"""Reconcile (and optionally correct) historical cycle-count valuation drift.

Dry-run by default - it only reports. Corrections post only with --apply, and
are idempotent (a movement is never corrected twice).

    # Report drift for all tenants (no changes):
    python manage.py reconcile_cycle_count_valuation

    # Report for one tenant:
    python manage.py reconcile_cycle_count_valuation --tenant 3

    # Apply corrections, treating periods up to 2026-03-31 as closed (those
    # corrections post into the current open period, referencing the original):
    python manage.py reconcile_cycle_count_valuation --apply --lock-date 2026-03-31

Original movements are never deleted or rewritten; corrections are posted as new
auditable revaluation entries and recorded in CycleCountValuationCorrection.
"""
import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant
from core.services import inventory_corrections as recon


def _parse_date(value, label):
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"Invalid {label} '{value}' (expected YYYY-MM-DD).")


class Command(BaseCommand):
    help = "Report (and with --apply, correct) historical cycle-count valuation drift."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None, help="Limit to one tenant id.")
        parser.add_argument("--apply", action="store_true",
                            help="Post corrections. Omit for a dry-run report (default).")
        parser.add_argument("--tolerance", default="0.01", help="Absolute variance tolerance (default 0.01).")
        parser.add_argument("--lock-date", default=None,
                            help="Close boundary (YYYY-MM-DD). Corrections for movements on/before it "
                                 "post into the current open period (or are blocked with --block-closed).")
        parser.add_argument("--posting-date", default=None,
                            help="Open-period posting date for closed-period corrections (default today).")
        parser.add_argument("--block-closed", action="store_true",
                            help="Block (skip) corrections that fall in a closed period instead of "
                                 "posting them into the current open period.")

    def handle(self, *args, **options):
        tolerance = Decimal(str(options["tolerance"]))
        lock_date = _parse_date(options["lock_date"], "--lock-date")
        posting_date = _parse_date(options["posting_date"], "--posting-date")
        apply = options["apply"]
        block_closed = options["block_closed"]

        if options["tenant"] is not None:
            tenants = list(Tenant.objects.filter(id=options["tenant"]))
            if not tenants:
                raise CommandError(f"Tenant {options['tenant']} not found.")
        else:
            tenants = list(Tenant.objects.all())

        grand_rows = grand_corrected = grand_blocked = 0
        for tenant in tenants:
            if apply:
                summary = recon.apply_corrections(
                    tenant, tolerance=tolerance, lock_date=lock_date,
                    posting_date=posting_date, block_closed=block_closed)
                rows = summary["corrected"] + summary["blocked"]
                grand_corrected += summary["corrected_count"]
                grand_blocked += summary["blocked_count"]
            else:
                rows = recon.find_drift(tenant, tolerance=tolerance)
            if not rows:
                continue
            grand_rows += len(rows)
            self.stdout.write(self.style.MIGRATE_HEADING(f"[{tenant.name}]"))
            for r in rows:
                blocked = apply and r in summary["blocked"]
                lot = r["lot_code"] or r["serial_number"] or (r["expiry_date"] and str(r["expiry_date"])) or "-"
                self.stdout.write(
                    f"  mv#{r['movement_id']} {r['product'].sku} @ {r['location'].name} lot={lot} "
                    f"date={r['movement_date']} qty={r['qty']} "
                    f"orig={r['original_value']} expected={r['expected_value']} var={r['variance']} "
                    f"[{r['valuation_source']}] glΔ={r['gl_impact']} "
                    + ("BLOCKED (closed period)" if blocked else
                       ("APPLIED: " if apply else "ACTION: ") + r["suggested_action"]))

        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"Applied {grand_corrected} correction(s); {grand_blocked} blocked (closed period)."))
        else:
            if grand_rows:
                self.stdout.write(self.style.WARNING(
                    f"DRY-RUN: {grand_rows} movement(s) need correction. Re-run with --apply to post them."))
            else:
                self.stdout.write(self.style.SUCCESS("No cycle-count valuation drift found."))
