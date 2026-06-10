"""Audit legacy serial-tracked stock for data that violates the serial rules.

Audit-first and DRY-RUN by default — it only reads and reports. It never invents
serial numbers, never changes balances/financials and never rewrites movements.

    python manage.py audit_serial_readiness                 # all tenants, report
    python manage.py audit_serial_readiness --tenant "Acme" # one tenant
    python manage.py audit_serial_readiness --csv out.csv    # also write CSV
    python manage.py audit_serial_readiness --apply          # write audit-log flags only

--apply records an AuditLog entry per issue (action SERIAL_AUDIT) for traceability;
it makes NO inventory/GL changes. Real corrections are manual (see suggestions).
"""
import csv

from django.core.management.base import BaseCommand, CommandError

from core.models import Tenant, AuditLog
from core.services.serial_audit import audit_serial_readiness, summarize

CSV_COLUMNS = ["tenant", "sku", "location", "lot_code", "serial_number", "on_hand",
               "issue_type", "severity", "suggestion", "related", "detail"]


class Command(BaseCommand):
    help = "Audit (dry-run) legacy serial stock data for cardinality / coverage / costing issues."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None, help="Tenant name (default: all tenants).")
        parser.add_argument("--csv", default=None, help="Write the full issue list to this CSV path.")
        parser.add_argument("--apply", action="store_true",
                            help="Write an audit-log flag per issue (no inventory/GL changes).")

    def handle(self, *args, **options):
        tenant = None
        if options["tenant"]:
            tenant = Tenant.objects.filter(name=options["tenant"]).first()
            if tenant is None:
                raise CommandError(f"Tenant '{options['tenant']}' not found.")

        issues = audit_serial_readiness(tenant=tenant)
        summary = summarize(issues)

        if not issues:
            self.stdout.write(self.style.SUCCESS("Serial readiness: no issues found. Safe for strict serial rules."))
            return

        # Group lines by severity (high first) for readable output.
        order = {"high": 0, "medium": 1, "low": 2}
        for it in sorted(issues, key=lambda i: (order.get(i["severity"], 9), i["issue_type"], i["sku"])):
            style = self.style.ERROR if it["severity"] == "high" else (
                self.style.WARNING if it["severity"] == "medium" else self.style.NOTICE)
            self.stdout.write(style(
                f"[{it['severity'].upper()}] {it['issue_type']} · {it['tenant']} · {it['sku'] or '-'}"
                f"{(' · ' + it['location']) if it['location'] else ''}"
                f"{(' · SN ' + it['serial_number']) if it['serial_number'] else ''}"
                f"{(' · on_hand ' + str(it['on_hand'])) if it['on_hand'] is not None else ''}"
                f"  -> {it['suggestion']}  ({it['related']})"))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{summary['total']} issue(s): "
            + ", ".join(f"{k}={v}" for k, v in sorted(summary['by_severity'].items()))
            + " | by type: " + ", ".join(f"{k}={v}" for k, v in sorted(summary['by_type'].items()))))

        if options["csv"]:
            with open(options["csv"], "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                w.writeheader()
                for it in issues:
                    w.writerow(it)
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(issues)} rows to {options['csv']}."))

        if options["apply"]:
            n = 0
            for it in issues:
                AuditLog.objects.create(
                    tenant_id=it["tenant_id"], action="SERIAL_AUDIT",
                    entity_type="InventoryLotBalance", entity_id=(it["related"] or "")[:64],
                    detail=f"{it['issue_type']} [{it['severity']}] {it['sku']} {it['serial_number']} - {it['suggestion']}"[:255])
                n += 1
            self.stdout.write(self.style.SUCCESS(
                f"--apply: recorded {n} audit-log flag(s). No inventory or GL data was changed."))
        else:
            self.stdout.write(self.style.NOTICE(
                "Dry-run only (no changes). Re-run with --apply to record audit-log flags, "
                "or --csv PATH to export. Corrections are manual — see each suggestion."))
