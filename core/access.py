"""Active organisation + role resolution for the request.

Resolution order for the active tenant:
  1. The tenant chosen for this session (set by the org picker)
  2. The user's single membership, or their default membership
  3. Their UserProfile tenant (legacy)
  4. The first tenant (legacy fallback / superuser without membership)
"""
from django.urls import reverse, NoReverseMatch

from core import roles

SESSION_TENANT_KEY = "active_tenant_id"


def get_memberships(user):
    from core.models import OrgMembership
    return list(OrgMembership.objects.filter(user=user).select_related("tenant").order_by("tenant__name"))


def get_active_tenant(request):
    from core.models import Tenant, OrgMembership
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return Tenant.objects.order_by("id").first()

    tid = request.session.get(SESSION_TENANT_KEY) if hasattr(request, "session") else None
    if tid:
        m = OrgMembership.objects.filter(user=user, tenant_id=tid).select_related("tenant").first()
        if m:
            return m.tenant

    memberships = get_memberships(user)
    if len(memberships) == 1:
        return memberships[0].tenant
    if memberships:
        default = next((m for m in memberships if m.is_default), memberships[0])
        return default.tenant

    profile = getattr(user, "profile", None)
    if profile is not None:
        return profile.tenant
    return Tenant.objects.order_by("id").first()


def get_active_membership(request):
    from core.models import OrgMembership
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    tenant = get_active_tenant(request)
    if tenant is None:
        return None
    return OrgMembership.objects.filter(user=user, tenant=tenant).first()


def get_active_role(request):
    membership = get_active_membership(request)
    if membership:
        return membership.role
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_superuser:
        return roles.ADMIN
    return roles.READONLY


def get_user_overrides(user, tenant):
    """Return {permission_code: effect} for a user's per-org permission overrides."""
    from core.models import UserPermissionOverride
    if user is None or not getattr(user, "is_authenticated", False) or tenant is None:
        return {}
    return dict(
        UserPermissionOverride.objects.filter(user=user, tenant=tenant)
        .values_list("permission", "effect")
    )


def get_effective_permissions(request):
    """The active user's effective permission set for the active organisation:
    role baseline + per-user overrides. Superusers get everything."""
    from core import permissions
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False) and user.is_superuser:
        return set(permissions.ALL_PERMISSIONS)
    tenant = get_active_tenant(request)
    role = get_active_role(request)
    return permissions.effective_permissions(role, get_user_overrides(user, tenant))


def default_landing_url(tenant, role):
    override = (tenant.role_landing or {}).get(role) if tenant is not None else None
    name = override or roles.DASHBOARD_ROUTE.get(role, "dashboard_admin")
    try:
        return reverse(name)
    except NoReverseMatch:
        return reverse(roles.DASHBOARD_ROUTE.get(role, "dashboard_admin"))


def group_companies(user, tenant):
    """Companies (Tenants) in the same group that this user is a member of,
    including the current one. Used for the group switcher and consolidated
    reporting. If the company has no group, just returns [tenant]."""
    from core.models import Tenant, OrgMembership
    if tenant is None:
        return []
    if not getattr(tenant, "group_id", None):
        return [tenant]
    member_ids = set(OrgMembership.objects.filter(user=user).values_list("tenant_id", flat=True))
    if getattr(user, "is_superuser", False):
        member_ids = None  # superuser sees all companies in the group
    qs = Tenant.objects.filter(group_id=tenant.group_id).order_by("name")
    companies = [t for t in qs if (member_ids is None or t.id in member_ids)]
    if tenant not in companies:
        companies.append(tenant)
    return companies


def accessible_location_ids(user, tenant):
    """The location IDs a user may see/use in a tenant, or None for 'all'.

    Admins, superusers, and users with no explicit grants are unrestricted
    (returns None). Otherwise returns the set of granted location IDs."""
    from core.models import OrgMembership, UserLocationAccess
    if tenant is None or user is None or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return None
    m = OrgMembership.objects.filter(user=user, tenant=tenant).first()
    if m and m.role == roles.ADMIN:
        return None
    grants = set(UserLocationAccess.objects.filter(tenant=tenant, user=user)
                 .values_list("location_id", flat=True))
    return grants or None  # no grants -> unrestricted


def accessible_locations(user, tenant):
    """Location queryset the user may access (all active locations if unrestricted)."""
    from core.models import Location
    qs = Location.objects.filter(tenant=tenant)
    ids = accessible_location_ids(user, tenant)
    if ids is not None:
        qs = qs.filter(id__in=ids)
    return qs
