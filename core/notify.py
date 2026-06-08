"""Email notifications for the access-request workflow.

All sends are best-effort (fail_silently) so a mail problem never breaks the
request/approval flow.
"""
from django.conf import settings
from django.core.mail import send_mail


def _login_url(request):
    if request is not None:
        try:
            return request.build_absolute_uri("/login/")
        except Exception:
            pass
    return "/login/"


def _admin_emails(tenant=None):
    """Email addresses of admins to notify: org Admins (scoped to the tenant
    when known) plus active superusers."""
    from django.contrib.auth.models import User
    from core.models import OrgMembership

    emails = set()
    memberships = OrgMembership.objects.filter(role="ADMIN")
    if tenant is not None:
        memberships = memberships.filter(tenant=tenant)
    admin_ids = list(memberships.values_list("user_id", flat=True))
    qs = User.objects.filter(is_active=True).filter(
        id__in=admin_ids
    ) | User.objects.filter(is_active=True, is_superuser=True)
    for e in qs.exclude(email="").values_list("email", flat=True):
        if e:
            emails.add(e)
    if tenant is not None and getattr(tenant, "email", ""):
        emails.add(tenant.email)
    return sorted(emails)


def notify_admins_new_request(req, request=None):
    recipients = _admin_emails(req.tenant)
    if not recipients:
        return
    subject = f"[SwifPro BI] Access request from {req.name}"
    body = (
        f"A new access request has been submitted.\n\n"
        f"Name:        {req.name}\n"
        f"Employee ID: {req.employee_id or '-'}\n"
        f"Email:       {req.email}\n"
        f"Team:        {req.team or '-'}\n"
        f"Message:     {req.message or '-'}\n\n"
        f"Review it here: {request.build_absolute_uri('/access-requests/') if request else '/access-requests/'}\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=True)
    for _r in recipients:
        log_email(_r, subject, category="ACCESS_REQUEST", tenant=req.tenant, related=req)


def notify_applicant_approved(req, username, temp_password, request=None):
    if not req.email:
        return
    subject = "[SwifPro BI] Your account is ready"
    body = (
        f"Hi {req.name},\n\n"
        f"Your access request has been approved and an account has been created.\n\n"
        f"Username:           {username}\n"
        f"Temporary password: {temp_password}\n\n"
        f"Sign in here: {_login_url(request)}\n"
        f"Please change your password after your first sign-in.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [req.email], fail_silently=True)


def notify_credentials(email, name, username, temp_password, request=None):
    """Email new account credentials (used by admin invites)."""
    if not email:
        return
    subject = "[SwifPro BI] You've been invited"
    body = (
        f"Hi {name},\n\n"
        f"An account has been created for you on SwifPro BI.\n\n"
        f"Username:           {username}\n"
        f"Temporary password: {temp_password}\n\n"
        f"Sign in here: {_login_url(request)}\n"
        f"Please change your password after your first sign-in.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=True)


def notify_invoice(inv, request=None, attachment=None):
    """Email an issued invoice to the customer. Returns True if sent, False if
    the customer has no email. `attachment` is an optional (filename, bytes,
    mimetype) tuple (e.g. the PDF)."""
    from django.core.mail import EmailMessage
    email = getattr(inv.customer, "email", None)
    if not email:
        return False
    tenant = inv.tenant
    link = ""
    if request is not None:
        try:
            link = request.build_absolute_uri(f"/ar/invoices/{inv.id}/")
        except Exception:
            link = ""
    body = (
        f"Dear {inv.customer.name},\n\n"
        f"Please find invoice {inv.invoice_number} from {tenant.name}.\n\n"
        f"Invoice date: {inv.invoice_date}\n"
        f"Due date:     {inv.due_date or '-'}\n"
        f"Amount due:   {inv.currency_code} {inv.total:.2f}\n\n"
        + (f"View it online: {link}\n\n" if link else "")
        + "Thank you for your business.\n"
    )
    subject = f"Invoice {inv.invoice_number} from {tenant.name}"
    msg = EmailMessage(
        subject=subject,
        body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[email],
    )
    if attachment:
        msg.attach(*attachment)
    msg.send(fail_silently=True)
    log_email(email, subject, category="DOCUMENT_SENT", tenant=tenant, related=inv)
    return True


def notify_overdue_invoice(inv, request=None, attachment=None):
    """Email an overdue-payment reminder to the customer. Returns True if sent,
    False if the customer has no email."""
    from django.core.mail import EmailMessage
    from django.utils import timezone as _tz
    email = getattr(inv.customer, "email", None)
    if not email:
        return False
    tenant = inv.tenant
    days = (_tz.localdate() - inv.due_date).days if inv.due_date else 0
    link = ""
    if request is not None:
        try:
            link = request.build_absolute_uri(f"/ar/invoices/{inv.id}/")
        except Exception:
            link = ""
    body = (
        f"Dear {inv.customer.name},\n\n"
        f"Our records show invoice {inv.invoice_number} from {tenant.name} is overdue"
        f"{f' by {days} day(s)' if days > 0 else ''}.\n\n"
        f"Invoice date: {inv.invoice_date}\n"
        f"Due date:     {inv.due_date or '-'}\n"
        f"Amount due:   {inv.currency_code} {inv.outstanding:.2f}\n\n"
        + (f"View it online: {link}\n\n" if link else "")
        + "If payment has already been made, please disregard this reminder.\n"
        + "Thank you.\n"
    )
    subject = f"Payment reminder: invoice {inv.invoice_number} from {tenant.name}"
    msg = EmailMessage(
        subject=subject,
        body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[email],
    )
    if attachment:
        msg.attach(*attachment)
    msg.send(fail_silently=True)
    log_email(email, subject, category="OVERDUE", tenant=tenant, related=inv)
    return True


def notify_sales_document(doc, label, number, request=None, attachment=None):
    """Email a quote or sales order to its customer. Returns True if sent."""
    from django.core.mail import EmailMessage
    email = getattr(doc.customer, "email", None)
    if not email:
        return False
    tenant = doc.tenant
    body = (
        f"Dear {doc.customer.name},\n\n"
        f"Please find {label.lower()} {number} from {tenant.name} attached.\n\n"
        f"Total: {doc.currency_code} {doc.total:.2f}\n\n"
        "Thank you for your business.\n"
    )
    subject = f"{label} {number} from {tenant.name}"
    msg = EmailMessage(
        subject=subject,
        body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[email],
    )
    if attachment:
        msg.attach(*attachment)
    msg.send(fail_silently=True)
    log_email(email, subject, category="DOCUMENT_SENT", tenant=tenant, related=doc)
    return True


def notify_applicant_rejected(req, request=None):
    if not req.email:
        return
    subject = "[SwifPro BI] Access request update"
    body = (
        f"Hi {req.name},\n\n"
        f"Thank you for your interest. Your access request was not approved at this time. "
        f"Please contact your administrator if you believe this is a mistake.\n"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [req.email], fail_silently=True)
    log_email(req.email, subject, category="ACCESS_REQUEST", tenant=getattr(req, "tenant", None), related=req)


# ---------------------------------------------------------------------------
# In-app notifications + outbound-email audit log
# ---------------------------------------------------------------------------

def log_email(to_email, subject, category="GENERAL", status="SENT", error="",
              tenant=None, related=None, created_by=None):
    """Record one outbound email in the EmailLog audit trail. Best-effort."""
    if not to_email:
        return None
    from core.models import EmailLog
    kind, rid = "", None
    if related is not None:
        kind = related.__class__.__name__
        rid = getattr(related, "id", None)
    try:
        return EmailLog.objects.create(
            tenant=tenant, to_email=to_email, subject=(subject or "")[:255], category=category,
            status=status, error=(error or "")[:255], related_kind=kind, related_id=rid,
            created_by=created_by,
        )
    except Exception:
        return None


def _channel_prefs(user, tenant, category):
    """(in_app, email) channel preference for this user/tenant/category. Missing
    row => both channels on (the default)."""
    if user is None:
        return (True, True)
    from core.models import NotificationPreference
    try:
        p = NotificationPreference.objects.filter(user=user, tenant=tenant, category=category).first()
    except Exception:
        p = None
    return (True, True) if p is None else (p.in_app, p.email)


def notify_user(recipient, tenant=None, category="GENERAL", title="", message="", url="",
                actor=None, email_subject=None, email_body=None, request=None):
    """Create an in-app notification for ``recipient`` and, when their preference
    allows it, also email them. Honours per-category channel preferences and is
    best-effort (never raises)."""
    if recipient is None:
        return None
    from core.models import Notification
    in_app, want_email = _channel_prefs(recipient, tenant, category)
    note = None
    if in_app:
        try:
            note = Notification.objects.create(
                tenant=tenant, recipient=recipient, actor=actor, category=category,
                title=(title or "")[:200], message=message or "", url=url or "",
            )
        except Exception:
            note = None
    if want_email and getattr(recipient, "email", ""):
        subject = email_subject or f"[SwifPro BI] {title}"
        body = email_body or (message or title)
        if url:
            full = url
            if request is not None and url.startswith("/"):
                try:
                    full = request.build_absolute_uri(url)
                except Exception:
                    full = url
            body = f"{body}\n\nOpen: {full}\n"
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient.email], fail_silently=True)
            log_email(recipient.email, subject, category=category, status="SENT", tenant=tenant, created_by=actor)
        except Exception as e:  # pragma: no cover - send_mail already fail_silently
            log_email(recipient.email, subject, category=category, status="FAILED", error=str(e),
                      tenant=tenant, created_by=actor)
    return note


def notify_roles(tenant, roles, exclude_user=None, **kwargs):
    """Notify every active user holding one of ``roles`` in ``tenant`` (plus active
    superusers). Extra kwargs are passed through to ``notify_user``."""
    from django.contrib.auth.models import User
    from core.models import OrgMembership
    user_ids = set(
        OrgMembership.objects.filter(tenant=tenant, role__in=list(roles)).values_list("user_id", flat=True)
    )
    qs = (User.objects.filter(is_active=True, id__in=user_ids)
          | User.objects.filter(is_active=True, is_superuser=True)).distinct()
    notes = []
    for u in qs:
        if exclude_user is not None and u.id == exclude_user.id:
            continue
        notes.append(notify_user(u, tenant=tenant, **kwargs))
    return notes
