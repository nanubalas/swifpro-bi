from functools import wraps
from django.core.exceptions import PermissionDenied

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

def role_required(read_groups, write_groups=None):
    """
    Enforce simple RBAC:
    - GET/HEAD/OPTIONS allowed for read_groups (+ Admin implied)
    - Mutating methods require write_groups (or read_groups if write_groups not provided)
    Read-only group is only included when you explicitly add it to read_groups.
    """
    if write_groups is None:
        write_groups = read_groups

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if request.method in ("GET", "HEAD", "OPTIONS"):
                allowed = list(set(read_groups + [ROLE_ADMIN]))
                if not user_in_any_group(request.user, allowed):
                    raise PermissionDenied("You do not have permission to view this page.")
                return view_func(request, *args, **kwargs)
            else:
                allowed = list(set(write_groups + [ROLE_ADMIN]))
                if not user_in_any_group(request.user, allowed):
                    raise PermissionDenied("You do not have permission to perform this action.")
                return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
