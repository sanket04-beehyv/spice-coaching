"""Unit tests for AM/PO/SK document-usage hierarchy scoping."""

from __future__ import annotations

from platform_service.services.document_usage_hierarchy import (
    apply_document_usage_filters,
    org_user_index,
    resolve_visible_chw_ids,
    user_display,
)
from platform_service.services.user_service import get_all_users


def _first_of_role(role: str) -> int:
    for user in get_all_users():
        if user["role"] == role:
            return int(user["id"])
    raise AssertionError(f"no user with role={role}")


class TestResolveVisibleChwIds:
    def test_unrestricted_returns_none(self) -> None:
        assert resolve_visible_chw_ids(None, unrestricted=True) is None
        assert resolve_visible_chw_ids(401, unrestricted=True) is None

    def test_po_sees_self_and_sks(self) -> None:
        po_id = _first_of_role("PO")
        visible = resolve_visible_chw_ids(po_id)
        assert visible is not None
        assert po_id in visible
        index = org_user_index()
        for uid in visible:
            user = index[uid]
            assert user.id == po_id or (user.role == "SK" and user.parent_id == po_id)

    def test_am_sees_descendant_pos_and_sks(self) -> None:
        am_id = _first_of_role("AM")
        visible = resolve_visible_chw_ids(am_id)
        assert visible is not None
        assert am_id in visible
        index = org_user_index()
        po_ids = {u.id for u in index.values() if u.role == "PO" and u.parent_id == am_id}
        assert po_ids
        assert po_ids.issubset(visible)
        for uid in visible:
            user = index[uid]
            assert user.id == am_id or user.role in {"PO", "SK"}

    def test_missing_viewer_id_without_unrestricted_is_empty(self) -> None:
        assert resolve_visible_chw_ids(None, unrestricted=False) == frozenset()

    def test_unknown_viewer_id_treated_as_unrestricted(self) -> None:
        assert resolve_visible_chw_ids(9_999_999_999, unrestricted=False) is None


class TestApplyDocumentUsageFilters:
    def test_po_filter_intersects_visible(self) -> None:
        po_id = _first_of_role("PO")
        other_po = next(int(u["id"]) for u in get_all_users() if u["role"] == "PO" and int(u["id"]) != po_id)
        visible = resolve_visible_chw_ids(po_id)
        assert visible is not None
        narrowed = apply_document_usage_filters(visible, po_id=other_po)
        assert narrowed is not None
        # Intersection of PO A's tree with PO B's cohort should be empty.
        assert len(narrowed) == 0

    def test_sk_filter_within_po_tree(self) -> None:
        po_id = 401
        sk_id = 395
        visible = resolve_visible_chw_ids(po_id)
        result = apply_document_usage_filters(visible, sk_id=sk_id)
        assert result == frozenset({sk_id})

    def test_district_filter_from_unrestricted(self) -> None:
        result = apply_document_usage_filters(None, district="Lalmonirhat")
        assert result is not None
        assert len(result) > 0
        index = org_user_index()
        assert all(index[uid].district == "Lalmonirhat" for uid in result)

    def test_upazila_filter_from_unrestricted(self) -> None:
        result = apply_document_usage_filters(None, upazila="Lalmonirhat Sadar")
        assert result is not None
        assert len(result) > 0
        index = org_user_index()
        assert all(index[uid].upazila == "Lalmonirhat Sadar" for uid in result)

    def test_user_display_known_and_unknown(self) -> None:
        display = user_display(401)
        assert display["user_name"]
        assert display["user_role"] == "PO"
        unknown = user_display(9_999_999_999)
        assert unknown["user_name"] is None
        assert unknown["user_role"] is None
