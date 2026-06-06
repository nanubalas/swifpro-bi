"""Thread-local storage for the request's active tenant and site (location).

Lets forms (which have no request handle) scope their FK choice fields to the
current tenant, and lets services read the selected site. Populated per-request
by CurrentTenantMiddleware.
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


def set_current_location(location):
    _state.location = location


def get_current_location():
    return getattr(_state, "location", None)


def clear_current_location():
    if hasattr(_state, "location"):
        del _state.location
