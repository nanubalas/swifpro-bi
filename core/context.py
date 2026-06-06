"""Template context processor: active role, company + site context, the
role-filtered nav, and the active role's permission set (for conditional UI)."""
from core import roles, permissions
from core.access import (get_active_role, get_active_tenant, get_memberships,
                         get_effective_permissions, get_active_location,
                         selectable_locations)


def nav(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    role = get_active_role(request)
    tenant = get_active_tenant(request)
    memberships = get_memberships(user)
    location = get_active_location(request)
    # Companies the user may switch to (for the header dropdown).
    companies = [m.tenant for m in memberships]
    if getattr(user, "is_superuser", False) and not companies and tenant is not None:
        companies = [tenant]
    # Sites of the selected company the user may switch to (no 'all sites').
    sites = list(selectable_locations(user, tenant)) if tenant is not None else []
    return {
        "active_role": role,
        "active_role_label": roles.ROLE_LABELS.get(role, role),
        "sidebar": roles.sidebar_for_role(role),
        "membership_count": len(memberships),
        "active_tenant_name": getattr(tenant, "name", ""),
        "active_tenant_id": getattr(tenant, "id", None),
        "active_site": location,
        "active_site_name": getattr(location, "name", ""),
        "active_site_id": getattr(location, "id", None),
        "switch_companies": companies,
        "switch_sites": sites,
        "perms": get_effective_permissions(request),
    }
