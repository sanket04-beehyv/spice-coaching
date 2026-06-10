"""Platform models module data (seed) — NEUTERED.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12

Originally loaded `infra/sql/platform_models_module_data.sql` (a 7800-line
pg_dump). That seed has two structural defects:

1. The dump was captured from a DB *after* later schema migrations had
   run; its INSERT statements reference columns (e.g. `content_domain`,
   `assessment_mode`) that don't yet exist when this migration applies.
   Result: `alembic upgrade head` fails on any fresh database.
2. The seed is BUSINESS DATA, not schema. A migration that performs
   ``DELETE FROM source_document, module, source_page, ...`` and reloads
   it from disk every time the chain is re-applied is a foot-gun against
   any environment that holds real content.

The seed loader has been moved out of the migration chain. ``upgrade``
is now a no-op so the migration sequence stays valid against fresh DBs
and existing deployments alike. The original SQL file is preserved in
``infra/sql/platform_models_module_data.sql`` and can be applied as a
one-shot dev helper outside of alembic (e.g. ``psql -f`` for a local
demo environment); production paths should ingest real documents
through the v3.3 pipeline.
"""

# pylint: disable=no-member

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _iter_sql_statements(sql: str) -> Iterator[str]:
    """Split a SQL script into individual statements.

    This is intentionally small and tailored for our seed dumps:
    - skips ``-- ...`` line comments and ``/* ... */`` block comments
      (so any semicolons inside them do not terminate a statement)
    - handles single-quoted strings with doubled quotes ('')
    - handles dollar-quoted blocks ($tag$...$tag$) if present
    - treats semicolons outside strings/blocks as statement terminators
    """

    buf: list[str] = []
    in_single_quote = False
    dollar_tag: str | None = None

    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # Line comment: "-- ..." through end of line. Strip from input so
        # semicolons inside pg_dump header comments don't split statements.
        if not in_single_quote and dollar_tag is None and ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            if j == -1:
                i = n
            else:
                i = j  # keep the newline so token boundaries stay sane
            continue

        # Block comment: "/* ... */" (may not nest in our dumps).
        if not in_single_quote and dollar_tag is None and ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue

        # Start/end single-quoted string, with '' escape.
        if dollar_tag is None and ch == "'":
            if in_single_quote and i + 1 < n and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
            buf.append(ch)
            i += 1
            continue

        # Start/end dollar-quoted block. (Only when not inside a '...' string.)
        if not in_single_quote and ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i : j + 1]  # includes both $...$
                if dollar_tag is None:
                    dollar_tag = tag
                    buf.append(tag)
                    i = j + 1
                    continue
                if tag == dollar_tag:
                    dollar_tag = None
                    buf.append(tag)
                    i = j + 1
                    continue

        # Statement terminator.
        if not in_single_quote and dollar_tag is None and ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                yield stmt
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        yield tail


# pg_dump prepends session-state statements (SET ..., set_config('search_path', '', false), etc.)
# that are meant for pg_restore. Inside an Alembic transaction they leak — most importantly,
# clearing search_path breaks Alembic's own UPDATE of the unqualified `alembic_version` table.
_PGDUMP_PREAMBLE_RE = re.compile(
    r"^(SET\s|SELECT\s+pg_catalog\.set_config\s*\()",
    re.IGNORECASE,
)


def upgrade() -> None:
    # No-op. See module docstring: seed loading does not belong in a
    # migration. Apply the SQL file manually for dev environments.
    return


def downgrade() -> None:
    # No-op. See module docstring.
    return


def _legacy_downgrade() -> None:  # pragma: no cover — kept for reference only
    # The seed dump begins by clearing these tables; we mirror that to remove
    # rows introduced by the seed.
    conn = op.get_bind()
    for stmt in (
        "DELETE FROM public.source_page",
        "DELETE FROM public.content_block",
        "DELETE FROM public.module_quiz_question",
        "DELETE FROM public.module_candidate_draft",
        "DELETE FROM public.behavioural_gap",
        "DELETE FROM public.module",
        "DELETE FROM public.module_family",
        "DELETE FROM public.ingestion_run",
        "DELETE FROM public.source_document",
    ):
        conn.exec_driver_sql(stmt)
