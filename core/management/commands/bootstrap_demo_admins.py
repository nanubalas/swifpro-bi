"""Bootstrap demo admin accounts from environment variables.

For hosted demos (e.g. Render free tier, which has no Shell to run
`createsuperuser`). Idempotent and safe to run on every deploy — it never prints
passwords, never raises on missing config, and never touches other tenants.

Creates:
  * one Django superuser "owner" (staff + superuser + app ADMIN role), and
  * optionally four demo app-admin users (staff + app ADMIN role, NOT Django
    superusers) for shared testing.

All accounts are placed in a single demo tenant ("SwifPro BI Demo Ltd") with an
ADMIN OrgMembership, which grants full, unrestricted app access (admins bypass
site/location scoping and have all permissions).

DEMO / TESTING ONLY — disable or delete these accounts before real production use.

Environment variables
---------------------
Main owner (required to create the owner):
  DJANGO_SUPERUSER_PASSWORD   (required; owner is skipped if unset)
  DJANGO_SUPERUSER_USERNAME   (default: santhosh)
  DJANGO_SUPERUSER_EMAIL      (default: santhoshkumarnanubala@gmail.com)

Four demo admins (all optional):
  DEMO_ADMINS_ENABLED=1       (create the four demo admins; otherwise only owner)
  DEMO_ADMIN_DEFAULT_PASSWORD (required for the four; skipped with a warning if unset)
  DEMO_ADMIN_1_EMAIL .. DEMO_ADMIN_4_EMAIL  (a missing email skips that one user)
"""
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from core import roles

DEMO_TENANT_NAME = "SwifPro BI Demo Ltd"

DEFAULT_OWNER_USERNAME = "santhosh"
DEFAULT_OWNER_EMAIL = "santhoshkumarnanubala@gmail.com"

DEMO_ADMINS = [
    ("admin1", "DEMO_ADMIN_1_EMAIL"),
    ("admin2", "DEMO_ADMIN_2_EMAIL"),
    ("admin3", "DEMO_ADMIN_3_EMAIL"),
    ("admin4", "DEMO_ADMIN_4_EMAIL"),
]


class Command(BaseCommand):
    help = "Create/ensure demo admin accounts from env vars (idempotent, safe for hosted demos)."

    def handle(self, *args, **options):
        # Owner first (independent of demo admins).
        owner_pw = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if not owner_pw:
            self.stdout.write(self.style.WARNING(
                "DJANGO_SUPERUSER_PASSWORD not set — skipping main owner. (No accounts changed.)"))
        else:
            owner_username = os.environ.get("DJANGO_SUPERUSER_USERNAME", DEFAULT_OWNER_USERNAME)
            owner_email = os.environ.get("DJANGO_SUPERUSER_EMAIL", DEFAULT_OWNER_EMAIL)
            self._ensure_user(owner_username, owner_email, owner_pw, superuser=True)

        # Four demo app-admins, only when explicitly enabled.
        if os.environ.get("DEMO_ADMINS_ENABLED", "") != "1":
            self.stdout.write("DEMO_ADMINS_ENABLED != 1 — not creating the four demo admins.")
            self.stdout.write(self.style.SUCCESS("bootstrap_demo_admins complete."))
            return

        demo_pw = os.environ.get("DEMO_ADMIN_DEFAULT_PASSWORD", "")
        if not demo_pw:
            self.stdout.write(self.style.WARNING(
                "DEMO_ADMINS_ENABLED=1 but DEMO_ADMIN_DEFAULT_PASSWORD is not set — "
                "skipping the four demo admins."))
            self.stdout.write(self.style.SUCCESS("bootstrap_demo_admins complete."))
            return

        for username, email_var in DEMO_ADMINS:
            email = os.environ.get(email_var, "")
            if not email:
                self.stdout.write(self.style.WARNING(
                    f"{email_var} not set — skipping demo admin '{username}'."))
                continue
            self._ensure_user(username, email, demo_pw, superuser=False)

        self.stdout.write(self.style.SUCCESS("bootstrap_demo_admins complete."))

    # ------------------------------------------------------------------
    @transaction.atomic
    def _ensure_user(self, username, email, password, *, superuser):
        """Create the user if missing (never resets an existing password) and
        ensure staff flag + full app ADMIN membership in the demo tenant."""
        user, created = User.objects.get_or_create(
            username=username, defaults={"email": email})
        if created:
            user.email = email
            user.set_password(password)          # only on first creation
            user.is_staff = True
            user.is_superuser = superuser
            user.save()
            self.stdout.write(self.style.SUCCESS(f"Created user '{username}' ({email})."))
        else:
            # Idempotent convergence: ensure access flags, but DO NOT touch the
            # existing password (don't recreate / don't reset).
            changed = False
            if not user.is_staff:
                user.is_staff = True; changed = True
            if superuser and not user.is_superuser:
                user.is_superuser = True; changed = True
            if changed:
                user.save()
            self.stdout.write(f"User '{username}' already exists — left password unchanged.")

        self._ensure_admin_membership(user)

    def _ensure_admin_membership(self, user):
        """Ensure the user belongs to the demo tenant with the ADMIN role (full
        access). Creates the demo tenant on first use. Touches no other tenant."""
        from core.models import Tenant, OrgMembership, UserProfile
        tenant, _ = Tenant.objects.get_or_create(name=DEMO_TENANT_NAME)

        m, m_created = OrgMembership.objects.get_or_create(
            user=user, tenant=tenant,
            defaults={"role": roles.ADMIN, "is_default": True})
        if not m_created and (m.role != roles.ADMIN or not m.is_default):
            m.role = roles.ADMIN
            m.is_default = True
            m.save(update_fields=["role", "is_default"])

        # Legacy fallback binding (harmless; OrgMembership is authoritative).
        UserProfile.objects.get_or_create(user=user, defaults={"tenant": tenant})
