"""Per-request tenant resolution, stored in a thread-local for forms to read."""
from django.utils import timezone as djtz

from core.current import set_current_tenant, clear_current_tenant


class CurrentTenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve(request)
        set_current_tenant(tenant)
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
            djtz.deactivate()

    @staticmethod
    def _resolve(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        from core.access import get_active_tenant
        return get_active_tenant(request)
