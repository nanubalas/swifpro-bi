from functools import wraps
from django.core.exceptions import PermissionDenied
from django.contrib.auth.views import redirect_to_login

ROLE_ADMIN = "Admin"
ROLE_PROCUREMENT = "Procurement"
ROLE_WAREHOUSE = "Warehouse"
ROLE_SALES = "Sales"
ROLE_FINANCE = "Finance"
ROLE_READONLY = "Read-only"

ALL_ROLES = [ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_SALES, ROLE_FINANCE, ROLE_READONLY]


def user_in_any_group(user, groups):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    user_groups = set(user.groups.values_list("name", flat=True))
    return bool(user_groups.intersection(set(groups)))


def effective_groups(request):
    """The permission groups in force for THIS request's active organisation.

    For members, this is derived from the active-org membership role only, so a
    multi-org user's access reflects their role in the *current* organisation
    (not the union across orgs). Users without a membership (legacy/group-only)
    fall back to their actual Django groups."""
    from core.access import get_active_membership
    from core.roles import ROLE_TO_GROUPS
    membership = get_active_membership(request)
    if membership is not None:
        return set(ROLE_TO_GROUPS.get(membership.role, []))
    return set(request.user.groups.values_list("name", flat=True))


def permission_required(perm):
    """Gate a view on a named permission from the role->permission matrix
    (core.permissions), scoped to the active organisation."""
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if user.is_superuser:
                return view_func(request, *args, **kwargs)
            from core.access import get_effective_permissions
            if perm not in get_effective_permissions(request):
                raise PermissionDenied("You do not have permission for this action.")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def role_required(read_groups, write_groups=None):
    """
    Per-active-organisation RBAC:
    - GET/HEAD/OPTIONS allowed for read_groups (+ Admin implied)
    - Mutating methods require write_groups (or read_groups if not provided)
    Enforcement is based on the active org's role (see effective_groups).
    """
    if write_groups is None:
        write_groups = read_groups

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if user.is_superuser:
                return view_func(request, *args, **kwargs)

            wanted = read_groups if request.method in ("GET", "HEAD", "OPTIONS") else write_groups
            allowed = set(wanted) | {ROLE_ADMIN}
            if not (allowed & effective_groups(request)):
                raise PermissionDenied("You do not have permission for this action.")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
