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
        # Import lazily so app registry is ready.
        from core.models import Tenant

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            profile = getattr(user, "profile", None)
            if profile is not None:
                return profile.tenant
            # Profile-less (e.g. initial superuser): fall back to first tenant.
            return Tenant.objects.order_by("id").first()
        return None
