# Multi-Tenancy Model

MicroCoaching is deployed as a **multi-tenant** platform. Each SPICE tenant maps to a
platform UUID via `SPICE_TENANT_ID_MAP` (see [`tenant_mapping.py`](../services/platform/src/platform_service/tenant_mapping.py)).

## Data model

- `module.tenant_id` and related tables may be `NULL` (tenant-global corpus) or a specific
  tenant UUID (tenant-specific content).
- Repository queries use `tenant_scope_filter()` from
  [`tenant_scope.py`](../services/platform/src/platform_service/db/tenant_scope.py), which
  matches **both** tenant-global rows (`tenant_id IS NULL`) and rows for the active tenant.

## Planes

| Plane | Tenant resolution | Scope |
|-------|-------------------|--------|
| Device (`/sync`, `/coaching`, `/morning`, `/telemetry`) | `resolve_tenant_id_for_device_route` | Device principals are bound to their mapped tenant; admins may override |
| Dashboard (`/dashboard/*`) | `resolve_tenant_id_for_dashboard` | Admin may pass `tenant_id` query param when auth is enabled |
| Admin (`/admin/*`) | `resolve_tenant_id_for_admin` | Same rules as dashboard — scoped to mapped tenant unless admin overrides |

When `SPICE_AUTH_ENABLED=false` (local dev), tenant filters are optional: `tenant_id=None`
means no tenant filter (global view). In production, auth is required and admin operations
are scoped to the authenticated principal's tenant unless an explicit override is provided.

## Operations that must respect tenant scope

- `GET /admin/modules` — list modules
- `POST /admin/modules/search` — semantic search

Device-plane RAG already passes `tenant_id` to `search_by_embedding`; admin endpoints
mirror that behaviour.
