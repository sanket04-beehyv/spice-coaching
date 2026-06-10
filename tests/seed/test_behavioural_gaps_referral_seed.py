"""Regression guard: referral seed registry uses uniform domain."""

from __future__ import annotations

import json
from pathlib import Path

_REFERRAL_SEED = Path(__file__).resolve().parents[2] / "seed" / "behavioural_gaps_referral.json"
_EXPECTED_GAP_COUNT = 45
_REFERRAL_DOMAIN = "referral"


def test_referral_seed_all_gaps_use_referral_domain() -> None:
    payload = json.loads(_REFERRAL_SEED.read_text(encoding="utf-8"))
    gaps = payload["gaps"]
    assert len(gaps) == _EXPECTED_GAP_COUNT
    domains = {gap["domain"] for gap in gaps}
    assert domains == {_REFERRAL_DOMAIN}
