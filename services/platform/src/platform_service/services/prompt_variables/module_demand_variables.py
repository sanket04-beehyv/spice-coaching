"""Variable builders for module demand summary prompt."""

from __future__ import annotations


def build_module_demand_variables(*, demand_lines: list[str], top_k: int) -> dict[str, str]:
    body = "\n".join(demand_lines) if demand_lines else "(no module requests yet)"
    return {
        "top_k": str(top_k),
        "demand_body": body,
    }
