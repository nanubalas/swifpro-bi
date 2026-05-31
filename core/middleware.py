"""Per-request tenant resolution, stored in a thread-local for forms to read."""
from core.current import set_current_tenant, clear_current_tenant


class CurrentTenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_current_tenant(self._resolve(request))
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()

    @staticmethod
    def _resolve(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        from core.access import get_active_tenant
        return get_active_tenant(request)
