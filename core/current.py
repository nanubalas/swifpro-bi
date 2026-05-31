"""Thread-local storage for the request's active tenant.

Lets forms (which have no request handle) scope their FK choice fields to the
current tenant. Populated per-request by CurrentTenantMiddleware.
"""
import threading

_state = threading.local()


def set_current_tenant(tenant):
    _state.tenant = tenant


def get_current_tenant():
    return getattr(_state, "tenant", None)


def clear_current_tenant():
    if hasattr(_state, "tenant"):
        del _state.tenant
