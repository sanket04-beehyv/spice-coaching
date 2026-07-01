#!/usr/bin/env python3
"""Resume a partially-failed PipelineOrchestrator run for an existing
source_document. Used when the upstream stages succeeded but a downstream
stage needs to be re-attempted (e.g. after a fix to a prompt or provider
config that doesn't change the cached upstream responses).

Optional --invalidate-truncated-cache: deletes llm_call_cache rows whose
output token count is suspiciously small (<100) — this is the signature
of the Gemini 2.5 thinking-budget bug. Use it once after fixing the
provider, then drop the flag.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "services" / "platform" / "src"))

from platform_service.config import get_settings  # noqa: E402
from platform_service.db.models.source_document import SourceDocument  # noqa: E402
from platform_service.workers.pipeline_orchestrator import PipelineOrchestrator  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402


async def _resume(doc_id: UUID, *, invalidate_cache: bool) -> None:
    s = get_settings()
    url = make_url(s.database_url)
    if s.database_password:
        url = url.set(password=s.database_password)
    engine = create_async_engine(url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        if invalidate_cache:
            res = await session.execute(
                text("""
                    DELETE FROM llm_call_cache
                    WHERE response_jsonb->>'generation_type' IN ('outline_inference',
                                                                 'module_identification',
                                                                 'card_drafting',
                                                                 'quiz_drafting',
                                                                 'distractor_critique',
                                                                 'bilingual_translation')
                      AND ((response_jsonb->'token_usage'->>'output')::int) < 100
                    RETURNING id
                """)
            )
            deleted = len(list(res))
            await session.commit()
            print(f"Invalidated {deleted} truncated cache rows.")

        doc = await session.get(SourceDocument, doc_id)
        if doc is None:
            print(f"source_document {doc_id} not found.", file=sys.stderr)
            sys.exit(1)
        print(f"Resuming pipeline for {doc.title!r} (path={doc.original_storage_path})")
        orch = PipelineOrchestrator(session)
        result = await orch.run(
            source_document_id=doc.id,
            source_path=doc.original_storage_path,
            source_type=doc.source_type,
            primary_language=doc.primary_language,
            resume=True,
        )
        await session.commit()
        print()
        print(f"  run_id          = {result.run_id}")
        print(f"  final_status    = {result.final_status}")
        print(f"  candidates      = {result.candidates_emitted}")
        print(f"  drafts produced = {result.drafts_produced}")
        print()
        for s in result.stages:
            print(f"  · {s.stage:18s} {s.status:10s} summary={s.summary} error={s.error}")
    await engine.dispose()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source_document_id", type=UUID, help="UUID of the source_document to resume")
    p.add_argument(
        "--invalidate-truncated-cache",
        action="store_true",
        help="Delete cache rows with <100 output tokens before resuming",
    )
    args = p.parse_args()
    asyncio.run(_resume(args.source_document_id, invalidate_cache=args.invalidate_truncated_cache))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
