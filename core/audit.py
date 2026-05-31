"""Lightweight audit logging for security-sensitive events."""


def get_client_ip(request):
    if not request:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_audit(*, action, request=None, user=None, tenant=None, detail=None, username=None):
    """Best-effort audit record; never raises into the request path."""
    from core.models import AuditLog
    try:
        real_user = user if (user is not None and getattr(user, "is_authenticated", False)) else None
        AuditLog.objects.create(
            tenant=tenant,
            user=real_user,
            username=username or getattr(user, "username", None),
            action=action,
            detail=(detail or "")[:255],
            path=(getattr(request, "path", None) if request else None),
            ip=get_client_ip(request),
        )
    except Exception:
        pass
