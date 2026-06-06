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
SESSION_LOCATION_KEY = "active_location_id"
SESSION_SITE_KEY = "active_site_id"


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


def accessible_site_ids(user, tenant):
    """The site IDs a user may work in for a tenant, or None for 'all'.

    Mirrors accessible_location_ids but for the Site tier: Admins, superusers
    and users with no explicit grants are unrestricted (None)."""
    from core.models import OrgMembership, UserSiteAccess
    if tenant is None or user is None or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return None
    m = OrgMembership.objects.filter(user=user, tenant=tenant).first()
    if m and m.role == roles.ADMIN:
        return None
    grants = set(UserSiteAccess.objects.filter(tenant=tenant, user=user)
                 .values_list("site_id", flat=True))
    return grants or None  # no grants -> unrestricted


def accessible_sites(user, tenant):
    """Site queryset the user may access (all active sites if unrestricted)."""
    from core.models import Site
    qs = Site.objects.filter(tenant=tenant)
    ids = accessible_site_ids(user, tenant)
    if ids is not None:
        qs = qs.filter(id__in=ids)
    return qs


def selectable_sites(user, tenant):
    """Active sites the user may select as their working site, ordered by name.
    No 'all' sentinel - always a concrete set."""
    if tenant is None:
        from core.models import Site
        return Site.objects.none()
    return accessible_sites(user, tenant).filter(is_active=True).order_by("name")


# ---------------------------------------------------------------------------
# Mandatory company + site (location) context
#
# Every authenticated user must operate inside exactly one selected company and
# one selected site. There is deliberately NO "all sites" option. The selected
# site is a Location (the entity that scopes data); Sites group locations in the
# picker UI. See the context gate in CurrentTenantMiddleware.
# ---------------------------------------------------------------------------

def selectable_locations(user, tenant):
    """Active locations the user may select as their working site, ordered by
    site then name. No 'all' sentinel - the result is always a concrete set."""
    if tenant is None:
        from core.models import Location
        return Location.objects.none()
    return (accessible_locations(user, tenant)
            .filter(is_active=True)
            .select_related("site")
            .order_by("site__name", "name"))


def get_active_location(request):
    """The validated selected site (Location) for this session, or None.

    Returns None - forcing (re)selection - when nothing is chosen, or when the
    chosen location no longer belongs to the active company / is not accessible /
    is inactive. Never returns an 'all sites' value."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    tenant = get_active_tenant(request)
    if tenant is None:
        return None
    lid = request.session.get(SESSION_LOCATION_KEY) if hasattr(request, "session") else None
    if not lid:
        return None
    return selectable_locations(user, tenant).filter(id=lid).first()


def active_location_ids(request):
    """Inventory-location ids to scope module data to: the accessible locations
    under the **selected Site**. Returns None only when no site is selected (e.g.
    on exempt setup pages), meaning 'do not narrow'. An empty list means the site
    has no accessible locations (so location-scoped lists are correctly empty)."""
    site = get_active_site(request)
    if site is None:
        return None
    user = getattr(request, "user", None)
    tenant = get_active_tenant(request)
    return list(accessible_locations(user, tenant).filter(site=site).values_list("id", flat=True))


def get_active_site(request):
    """The validated selected Site for this session, or None.

    During the Location->Site transition this falls back to the selected
    location's site, so the context resolves before the gate is flipped (S4)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    tenant = get_active_tenant(request)
    if tenant is None:
        return None
    sid = request.session.get(SESSION_SITE_KEY) if hasattr(request, "session") else None
    if sid:
        site = selectable_sites(user, tenant).filter(id=sid).first()
        if site is not None:
            return site
    # Transition fallback: derive the site from the currently selected location.
    loc = get_active_location(request)
    if loc is not None and loc.site_id:
        return selectable_sites(user, tenant).filter(id=loc.site_id).first()
    return None


def active_site_id(request):
    site = get_active_site(request)
    return site.id if site is not None else None


def can_access_company(user, tenant_id):
    """True if the user is a member of (or superuser over) the given company."""
    from core.models import OrgMembership
    if user is None or not getattr(user, "is_authenticated", False) or not tenant_id:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return OrgMembership.objects.filter(user=user, tenant_id=tenant_id).exists()


def can_access_site(user, tenant, site_id):
    """True if `site_id` is a selectable Site for the user in this company."""
    if tenant is None or not site_id:
        return False
    return selectable_sites(user, tenant).filter(id=site_id).exists()


def can_access_location(user, tenant, location_id):
    """True if `location_id` is an accessible inventory location in this company."""
    if tenant is None or not location_id:
        return False
    return accessible_locations(user, tenant).filter(id=location_id).exists()


# Path prefixes that never require a selected context (auth, static, the
# selection/onboarding endpoints themselves, etc.).
_GATE_EXEMPT_PREFIXES = (
    "/login", "/logout", "/static/", "/media/", "/admin/", "/request-access",
    "/select-org", "/select-site", "/switch-company", "/switch-site",
    "/no-site", "/change-password", "/healthz",
    # Setup / structure areas must be reachable before a site exists, so an
    # admin can create the org's first location(s).
    "/onboarding", "/locations", "/sites",
)


def context_gate(request):
    """Resolve & enforce the company+site context for an authenticated request.

    Auto-selects when there is exactly one valid choice (mutating the session so
    single-company / single-site users flow straight through). Returns a redirect
    URL when the user must choose, or None when the context is satisfied / the
    path is exempt.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    path = request.path or ""
    if any(path.startswith(p) for p in _GATE_EXEMPT_PREFIXES):
        return None

    tenant = get_active_tenant(request)
    if tenant is None:
        return None  # zero-membership user: landing / new-organisation handles this

    # Company: force an explicit choice when the user belongs to several.
    if not request.session.get(SESSION_TENANT_KEY) and len(get_memberships(user)) > 1:
        return _safe_reverse("select_org")
    # Persist the single-company auto-selection so it's an explicit context.
    if not request.session.get(SESSION_TENANT_KEY):
        request.session[SESSION_TENANT_KEY] = tenant.id

    # Site: the global context resolves to exactly one Site (never a location).
    if get_active_site(request) is not None:
        return None
    sites = list(selectable_sites(user, tenant))
    if len(sites) == 1:
        request.session[SESSION_SITE_KEY] = sites[0].id
        return None
    if not sites:
        return _safe_reverse("no_site")
    return _safe_reverse("select_site")


def _safe_reverse(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return "/"
