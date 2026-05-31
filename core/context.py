"""Template context processor: active role, tenant, and the role-filtered nav."""
from core import roles
from core.access import get_active_role, get_active_tenant, get_memberships


def nav(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    role = get_active_role(request)
    tenant = get_active_tenant(request)
    return {
        "active_role": role,
        "active_role_label": roles.ROLE_LABELS.get(role, role),
        "sidebar": roles.sidebar_for_role(role),
        "membership_count": len(get_memberships(user)),
        "active_tenant_name": getattr(tenant, "name", ""),
    }
