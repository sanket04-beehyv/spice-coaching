"""Alembic env.py for platform_service database migrations.

Run via deploy step (NOT on app startup):
    alembic -c infra/alembic.ini upgrade head
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from platform_service.db.base import Base
from platform_service.db.models import (  # noqa: F401 — register all models
    AttributionEvent,
    BehaviouralGap,
    CHWBehaviouralGapState,
    CHWLearningPointEvent,
    CHWModuleCompletion,
    CHWModuleQuizProgress,
    ConfigThreshold,
    ContentBlock,
    FileUpload,
    IngestionRun,
    IngestionRunStep,
    LlmCallCache,
    Module,
    ModuleBehaviouralGap,
    ModuleCandidateDraft,
    ModuleFamily,
    ModuleQuizQuestion,
    ModuleTriggerBinding,
    SourceDocument,
    SourcePage,
    TriggerDefinition,
)
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Optional: load a local .env so manual alembic invocations work outside docker.
# Compose/CI supply DATABASE_URL directly, so python-dotenv is not required.
try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL is not set")

config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
