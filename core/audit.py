"""Lightweight audit logging for security-sensitive events."""


def get_client_ip(request):
    if not request:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def get_user_agent(request):
    if not request:
        return None
    return (request.META.get("HTTP_USER_AGENT") or "")[:255] or None


def log_audit(*, action, request=None, user=None, tenant=None, detail=None, username=None,
              entity_type=None, entity_id=None, old_value=None, new_value=None):
    """Best-effort audit record; never raises into the request path.

    Captures who/what/when plus optional structured fields (entity + before/after
    values) and the request's IP and browser/device (user-agent)."""
    from core.models import AuditLog
    try:
        real_user = user if (user is not None and getattr(user, "is_authenticated", False)) else None
        AuditLog.objects.create(
            tenant=tenant,
            user=real_user,
            username=username or getattr(user, "username", None),
            action=action,
            entity_type=(str(entity_type)[:80] if entity_type else None),
            entity_id=(str(entity_id)[:64] if entity_id is not None else None),
            old_value=(str(old_value) if old_value is not None else None),
            new_value=(str(new_value) if new_value is not None else None),
            detail=(detail or "")[:255],
            path=(getattr(request, "path", None) if request else None),
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
    except Exception:
        pass
