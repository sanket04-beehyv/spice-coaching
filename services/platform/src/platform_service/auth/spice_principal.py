"""Principal checks from SPICE ``/authenticate`` user context (suite access planes)."""

from __future__ import annotations

from platform_service.auth.spice_context import SpiceUserContext

ADMIN_SUITE = "admin"
MOB_SUITE = "mob"

ADMIN_ROLE_NAMES = frozenset(
    {
        "area manager",
        "division manager",
        "head office",
        "super admin",
    }
)


def _normalize_role_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _role_suite_names(user: SpiceUserContext) -> frozenset[str]:
    names: set[str] = set()
    for role in user.roles:
        if role.suite_access_name:
            names.add(role.suite_access_name.strip().lower())
    return frozenset(names)


def is_admin_principal(user: SpiceUserContext) -> bool:
    """Admin plane: SUPER_USER or admin-management roles.

    Primary signal: role names in `user.roles[].name`.
    Compatibility fallback: `suiteAccessName == "admin"`.
    """
    if user.is_super_user:
        return True
    if any(_normalize_role_name(role.name) in ADMIN_ROLE_NAMES for role in user.roles):
        return True
    return ADMIN_SUITE in _role_suite_names(user)


def is_device_principal(user: SpiceUserContext) -> bool:
    """Device plane: SUPER_USER, JOB_USER, or any role with ``suiteAccessName == mob``."""
    if user.is_super_user:
        return True
    if user.is_job_user:
        return True
    return MOB_SUITE in _role_suite_names(user)
