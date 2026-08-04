#!/usr/bin/env python3
"""Fail if docs/error-codes.json drifts from mc_contracts.errors.ErrorCode."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from mc_contracts.errors import ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "docs" / "error-codes.json"

ALLOWED_DOMAINS = frozenset({"cross_cutting", "ingest", "modules", "auth", "ai", "worker"})
ALLOWED_AUDIENCES = frozenset({"client", "operator"})
REQUIRED_FIELDS = ("title", "description", "typical_status", "domain", "retryable", "audience")


def _fail(messages: list[str]) -> int:
    for message in messages:
        print(f"error-codes catalog: {message}", file=sys.stderr)
    return 1


def _validate_entry(code: str, entry: Any, errors: list[str]) -> None:
    if not isinstance(entry, dict):
        errors.append(f"{code}: entry must be an object")
        return
    for field in REQUIRED_FIELDS:
        if field not in entry:
            errors.append(f"{code}: missing required field {field!r}")
    title = entry.get("title")
    if "title" in entry and (not isinstance(title, str) or not title.strip()):
        errors.append(f"{code}: title must be a non-empty string")
    description = entry.get("description")
    if "description" in entry and (not isinstance(description, str) or not description.strip()):
        errors.append(f"{code}: description must be a non-empty string")
    status = entry.get("typical_status")
    if "typical_status" in entry and (not isinstance(status, int) or status < 400 or status > 599):
        errors.append(f"{code}: typical_status must be an int in 400..599")
    domain = entry.get("domain")
    if "domain" in entry and domain not in ALLOWED_DOMAINS:
        errors.append(f"{code}: domain must be one of {sorted(ALLOWED_DOMAINS)}")
    audience = entry.get("audience")
    if "audience" in entry and audience not in ALLOWED_AUDIENCES:
        errors.append(f"{code}: audience must be one of {sorted(ALLOWED_AUDIENCES)}")
    retryable = entry.get("retryable")
    if "retryable" in entry and not isinstance(retryable, bool):
        errors.append(f"{code}: retryable must be a boolean")


def main() -> int:
    if not CATALOG_PATH.is_file():
        return _fail([f"missing catalog file {CATALOG_PATH}"])

    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fail([f"invalid JSON in {CATALOG_PATH}: {exc}"])

    if not isinstance(payload, dict):
        return _fail(["catalog root must be an object"])

    errors_obj = payload.get("errors")
    if not isinstance(errors_obj, dict):
        return _fail(["catalog must contain an object field 'errors'"])

    enum_codes = {member.value for member in ErrorCode}
    catalog_codes = set(errors_obj.keys())

    messages: list[str] = []
    missing = sorted(enum_codes - catalog_codes)
    extra = sorted(catalog_codes - enum_codes)
    if missing:
        messages.append(f"codes in ErrorCode missing from catalog: {', '.join(missing)}")
    if extra:
        messages.append(f"codes in catalog not in ErrorCode: {', '.join(extra)}")

    for code in sorted(catalog_codes & enum_codes):
        _validate_entry(code, errors_obj[code], messages)

    if messages:
        return _fail(messages)

    print(f"error-codes catalog OK ({len(enum_codes)} codes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
