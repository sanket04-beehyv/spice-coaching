"""Generic async HTTP client helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx


@asynccontextmanager
async def managed_client(
    base_url: str = "",
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Context manager that yields a configured AsyncClient and closes it on exit."""
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        headers=headers or {},
    ) as client:
        yield client


async def post_json(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
    *,
    expected_status: int = 200,
) -> dict:
    """POST JSON and return parsed response, raising on unexpected status."""
    response = await client.post(path, json=payload)
    if response.status_code != expected_status:
        raise httpx.HTTPStatusError(
            f"Unexpected status {response.status_code} from {path}",
            request=response.request,
            response=response,
        )
    return response.json()
