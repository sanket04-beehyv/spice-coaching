"""AM → PO → SK hierarchy helpers for document-usage dashboard scoping.

Uses the same hardcoded org map as assignment resolvers (`get_all_users`).
True platform admins (not found as AM/PO/SK, or super-user class) see all
users; AM/PO see descendants only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platform_service.services.user_service import get_all_users


@dataclass(frozen=True, slots=True)
class OrgUser:
    id: int
    name: str
    role: str
    district: str | None
    upazila: str | None
    parent_id: int | None


def _users_by_id() -> dict[int, OrgUser]:
    out: dict[int, OrgUser] = {}
    for raw in get_all_users():
        user_id = int(raw["id"])
        out[user_id] = OrgUser(
            id=user_id,
            name=str(raw.get("name") or ""),
            role=str(raw.get("role") or ""),
            district=raw.get("district"),
            upazila=raw.get("upazila"),
            parent_id=int(raw["parent_id"]) if raw.get("parent_id") is not None else None,
        )
    return out


def org_user_index() -> dict[int, OrgUser]:
    """Map SPICE user id → org user record."""
    return _users_by_id()


def resolve_visible_chw_ids(
    viewer_id: int | None,
    *,
    unrestricted: bool = False,
) -> frozenset[int] | None:
    """Return chw_ids the viewer may see, or None when unrestricted (all users).

    - ``unrestricted=True`` (super/head-office/auth-off): no chw filter.
    - AM: all descendant POs and SKs (plus self).
    - PO: own id + direct SK children.
    - SK: self only.
    - Viewer id missing from the map and not unrestricted: empty set (deny).
    """
    if unrestricted:
        return None
    if viewer_id is None:
        return frozenset()

    by_id = _users_by_id()
    viewer = by_id.get(viewer_id)
    if viewer is None:
        # Not in AM/PO/SK map — treat as platform admin with full visibility.
        return None

    if viewer.role == "AM":
        visible: set[int] = {viewer.id}
        po_ids = {u.id for u in by_id.values() if u.role == "PO" and u.parent_id == viewer.id}
        visible.update(po_ids)
        visible.update(u.id for u in by_id.values() if u.role == "SK" and u.parent_id in po_ids)
        return frozenset(visible)

    if viewer.role == "PO":
        visible = {viewer.id}
        visible.update(u.id for u in by_id.values() if u.role == "SK" and u.parent_id == viewer.id)
        return frozenset(visible)

    # SK or unknown role in map
    return frozenset({viewer.id})


def apply_document_usage_filters(
    visible_chw_ids: frozenset[int] | None,
    *,
    po_id: int | None = None,
    sk_id: int | None = None,
    user_id: int | None = None,
    district: str | None = None,
    upazila: str | None = None,
) -> frozenset[int] | None:
    """Intersect hierarchy visibility with PO/SK/user/district/upazila filters.

    Geography filters (``district``, ``upazila``) resolve against the org user
    map — the same approach as district — so scope stays consistent with
    hierarchy. Returns None when still unrestricted (no chw filter needed).
    Returns an empty frozenset when filters yield no matching users.
    """
    by_id = _users_by_id()
    candidates: set[int] | None
    if visible_chw_ids is None:
        candidates = None
    else:
        candidates = set(visible_chw_ids)

    def _intersect(ids: set[int]) -> None:
        nonlocal candidates
        if candidates is None:
            candidates = set(ids)
        else:
            candidates &= ids

    if po_id is not None:
        po_set = {po_id}
        po_set.update(u.id for u in by_id.values() if u.role == "SK" and u.parent_id == po_id)
        _intersect(po_set)

    target_user = sk_id if sk_id is not None else user_id
    if target_user is not None:
        _intersect({target_user})

    if district is not None and district.strip():
        needle = district.strip().casefold()
        district_ids = {
            u.id for u in by_id.values() if u.district is not None and u.district.casefold() == needle
        }
        _intersect(district_ids)

    if upazila is not None and upazila.strip():
        needle = upazila.strip().casefold()
        upazila_ids = {
            u.id for u in by_id.values() if u.upazila is not None and u.upazila.casefold() == needle
        }
        _intersect(upazila_ids)

    if candidates is None:
        return None
    return frozenset(candidates)


def user_display(user_id: int, index: dict[int, OrgUser] | None = None) -> dict[str, Any]:
    """Name / role / geo display fields for a chw_id (nulls when unknown)."""
    by_id = index if index is not None else _users_by_id()
    user = by_id.get(user_id)
    if user is None:
        return {
            "user_name": None,
            "user_role": None,
            "district": None,
            "upazila": None,
        }
    return {
        "user_name": user.name or None,
        "user_role": user.role or None,
        "district": user.district,
        "upazila": user.upazila,
    }
