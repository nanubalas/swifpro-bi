"""Scheduled report exports (Phase 17).

Renders an MRP report (via reports.build_report) to CSV/XLSX bytes and, when
recipients + an email backend are configured, emails it as an attachment. The
cadence is computed from frequency / day-of-week / day-of-month / time-of-day.
A management command (run_scheduled_report_exports) processes due, active
exports; "Run now" calls deliver_export directly. Tenant scoping is enforced by
build_report (run/version resolved within the export's tenant).
"""
import datetime
import io

from django.utils import timezone


def render_bytes(columns, rows, fmt):
    """Return (bytes, mimetype, extension) for the given rows in CSV or XLSX."""
    if (fmt or "CSV").upper() == "XLSX":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = "Export"
        ws.append([str(c) for c in columns])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([("" if v is None else v) for v in r])
        ws.freeze_panes = "A2"
        buf = io.BytesIO()
        wb.save(buf)
        return (buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")
    import csv
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow(["" if v is None else v for v in r])
    return (buf.getvalue().encode("utf-8"), "text/csv", "csv")


def _effective_filters(export):
    filters = {}
    if export.saved_view_id and export.saved_view.filters_json:
        filters.update(export.saved_view.filters_json)
    if export.filters_json:
        filters.update(export.filters_json)
    return filters


def compute_next_run(export, after=None):
    """Next run datetime at/after ``after`` (default now) for the export's cadence."""
    after = after or timezone.now()
    tod = export.time_of_day or datetime.time(6, 0)
    local = timezone.localtime(after)
    candidate = local.replace(hour=tod.hour, minute=tod.minute, second=0, microsecond=0)

    if export.frequency == "DAILY":
        if candidate <= local:
            candidate += datetime.timedelta(days=1)
    elif export.frequency == "WEEKLY":
        target = export.day_of_week if export.day_of_week is not None else 0
        days = (target - candidate.weekday()) % 7
        candidate += datetime.timedelta(days=days)
        if candidate <= local:
            candidate += datetime.timedelta(days=7)
    else:  # MONTHLY
        dom = export.day_of_month or 1
        candidate = candidate.replace(day=min(dom, 28))
        if candidate <= local:
            month = candidate.month + 1
            year = candidate.year + (1 if month > 12 else 0)
            month = 1 if month > 12 else month
            candidate = candidate.replace(year=year, month=month, day=min(dom, 28))
    return candidate


def deliver_export(export, user=None):
    """Generate the export and email it if recipients exist. Updates last_run_at,
    last_status, last_error, next_run_at. Returns (filename, bytes, mimetype)."""
    from core.services.mrp import reports
    from core.notify import log_email
    now = timezone.now()
    filename = bytes_ = mimetype = None
    try:
        data = reports.build_report(export.tenant, export.report_type, _effective_filters(export))
        bytes_, mimetype, ext = render_bytes(data["columns"], data["rows"], export.format)
        filename = f"{data['filename']}.{ext}"

        recipients = export.recipient_list
        if recipients:
            from django.core.mail import EmailMessage
            from django.conf import settings
            subject = f"{export.name} - {data['title']} ({timezone.localdate()})"
            msg = EmailMessage(
                subject=subject,
                body=f"Attached: {data['title']} export ({len(data['rows'])} rows) from {export.tenant.name}.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@example.com"),
                to=recipients)
            msg.attach(filename, bytes_, mimetype)
            msg.send(fail_silently=False)
            for r in recipients:
                log_email(r, subject, category="MRP_EXPORT", tenant=export.tenant, created_by=user)

        export.last_status = "SUCCESS"
        export.last_error = ""
    except Exception as e:  # never let a scheduled run crash the batch
        export.last_status = "FAILED"
        export.last_error = str(e)[:255]
    export.last_run_at = now
    export.next_run_at = compute_next_run(export, after=now)
    export.save(update_fields=["last_status", "last_error", "last_run_at", "next_run_at"])
    return filename, bytes_, mimetype


def due_exports(now=None):
    from core.models import ScheduledReportExport
    now = now or timezone.now()
    return ScheduledReportExport.objects.filter(
        is_active=True, next_run_at__isnull=False, next_run_at__lte=now).select_related("tenant", "saved_view")


def process_due_exports(now=None):
    """Run all due, active exports. Returns (processed, failed)."""
    processed = failed = 0
    for export in due_exports(now):
        deliver_export(export)
        processed += 1
        if export.last_status == "FAILED":
            failed += 1
    return processed, failed
