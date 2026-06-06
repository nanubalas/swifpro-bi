"""Per-request tenant + site resolution, stored in a thread-local for forms and
services to read, plus the mandatory company/site selection gate."""
from django.shortcuts import redirect
from django.utils import timezone as djtz

from core.current import (set_current_tenant, clear_current_tenant,
                          set_current_location, clear_current_location,
                          set_current_site, clear_current_site)


class CurrentTenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from core.access import get_active_tenant, get_active_location, get_active_site, context_gate

        user = getattr(request, "user", None)
        authed = bool(user and user.is_authenticated)

        # Enforce the company + site context before dispatching the view. The
        # gate auto-selects when there's exactly one choice and otherwise
        # redirects to the picker; it's a no-op for exempt paths / anon users.
        if authed:
            target = context_gate(request)
            if target and request.path != target:
                return redirect(f"{target}?next={request.path}")

        tenant = get_active_tenant(request) if authed else None
        location = get_active_location(request) if authed else None
        site = get_active_site(request) if authed else None
        set_current_tenant(tenant)
        set_current_location(location)
        set_current_site(site)

        # Render dates/times in the active organisation's timezone.
        tz = getattr(tenant, "timezone", None)
        if tz:
            try:
                djtz.activate(tz)
            except Exception:
                djtz.deactivate()
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()
            clear_current_location()
            clear_current_site()
            djtz.deactivate()
