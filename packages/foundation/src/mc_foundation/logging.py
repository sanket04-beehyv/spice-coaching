"""Structured JSON logging — shared setup for all services."""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger.json import JsonFormatter


def setup_logging(
    *,
    service_name: str = "microcoaching",
    log_level: str = "INFO",
    json_logs: bool = True,
    app_env: str = "development",
) -> None:
    """Configure the root logger once per process.

    Safe to call multiple times: the first call wins and subsequent calls are
    no-ops. Services should call this at startup before any log records are
    emitted. Applies to every ``logging.getLogger(__name__)`` in the process.
    """
    root = logging.getLogger()
    root.setLevel(log_level.upper())

    if getattr(root, "_mc_configured", False):
        return

    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(
            JsonFormatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level"},
                static_fields={"service": service_name, "env": app_env},
            )
        )
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    # Replace any handlers installed by libraries (e.g. uvicorn's defaults) so
    # output is uniform.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root._mc_configured = True  # type: ignore[attr-defined]
