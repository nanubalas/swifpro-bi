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
