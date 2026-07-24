"""Shared prompt fragments for symbol/range verbalization in localized content.

Used by card_drafter_prompt and related system prompts so verbalization
rules stay in one place.
"""

from __future__ import annotations

from mc_foundation.locale import get_locale_metadata, locale_display_name

_SYMBOL_VERBALIZATION_BASE = """\
NUMERICAL VALUES AND SYMBOL NOTATION (resolves conflicts with "verbatim" rules):
- "Verbatim" means preserve clinical phrasing and terminology from source blocks —
  NOT raw symbol characters when verbalization is required below.
- Preserve every digit and unit from the source (BP 140/90, 8 g/dL, 2 tablets)
  across languages — do NOT change numeric values.
- Render mathematical symbols (`>=`, `<=`, `≧`, `≦`, `>`, `<`, `-` ranges, `/`, `±`, `%`)
  in natural spoken language, not as literal characters — even when the source block uses
  symbol notation.
- For `/`, choose phrasing from clinical context — do NOT default to "divided by":
  - Blood-pressure / vital-sign pairs (`BP 140/90 mmHg`) -> "over" (e.g. `140 over 90 mmHg`)
  - Medication doses (`1/2 tablet`, `1/4 tab`) -> spoken fraction (e.g. `half a tablet`)
  - Rates or visit cadence when `/` means "per" (`visits/month`) -> "per"
  - When unsure, follow the source block's spoken clinical usage; never leave a bare `/`
- Examples: `Hb < 8 g/dL` -> verbalize "<" as "less than" / equivalent in the target
  locale while keeping `8 g/dL`.
"""


def render_symbol_verbalization_rules(
    *,
    primary_locale: str,
) -> str:
    """Build locale-specific symbol verbalization examples for prompts."""
    primary_meta = get_locale_metadata(primary_locale)
    lines = [
        _SYMBOL_VERBALIZATION_BASE.rstrip(),
        f"- {primary_meta.display_name} examples:",
    ]
    lines.extend(f"  {example}" for example in primary_meta.symbol_verbalization_examples)
    return "\n".join(lines) + "\n"


def render_locale_map_field_schema(
    field_name: str,
    *,
    primary_locale: str,
    value_type: str = "string",
    description: str = "",
    primary_required: bool = False,
) -> str:
    """Render a JSON-schema fragment for one locale-keyed string map field."""
    primary_label = locale_display_name(primary_locale)
    req = " (REQUIRED)" if primary_required else ""
    desc_suffix = f"; {description}" if description else ""
    lines = [f'      "{field_name}": {{']
    lines.append(f'        "{primary_locale}": "{value_type}{req} — {primary_label}{desc_suffix}",')
    lines.append("      },")
    return "\n".join(lines)


def render_locale_synonym_map_field_schema(
    field_name: str,
    *,
    primary_locale: str,
    max_items: int,
    description: str = "",
) -> str:
    """Render a JSON-schema fragment for one locale-keyed synonym abbrev map."""
    primary_label = locale_display_name(primary_locale)
    desc_suffix = f"; {description}" if description else ""
    lines = [f'  "{field_name}": {{']
    lines.append(
        f'    "{primary_locale}": {{"ABBREV": "expanded form"}} '
        f"(≤ {max_items} — {primary_label}{desc_suffix}),"
    )
    lines.append("  },")
    return "\n".join(lines)


def render_locale_list_map_field_schema(
    field_name: str,
    *,
    primary_locale: str,
    max_items: int,
    description: str = "",
) -> str:
    """Render a JSON-schema fragment for one locale-keyed list map field."""
    primary_label = locale_display_name(primary_locale)
    desc_suffix = f"; {description}" if description else ""
    lines = [f'  "{field_name}": {{']
    lines.append(f'    "{primary_locale}": ["..."] (≤ {max_items} — {primary_label}{desc_suffix}),')
    lines.append("  },")
    return "\n".join(lines)


# Default rules for backward compatibility (default bn deployment).
SYMBOL_VERBALIZATION_RULES = render_symbol_verbalization_rules(primary_locale="bn")
