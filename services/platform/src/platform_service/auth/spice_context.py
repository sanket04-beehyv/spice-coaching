"""Pydantic models for SPICE auth-service ``ContextsDTO`` responses."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SpiceRole(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = None
    name: str | None = None
    level: int | None = None
    suite_access_name: str | None = Field(None, alias="suiteAccessName")


class SpiceUserContext(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = None
    first_name: str | None = Field(None, alias="firstName")
    last_name: str | None = Field(None, alias="lastName")
    username: str | None = None
    tenant_id: int | None = Field(None, alias="tenantId")
    is_super_user: bool = Field(False, alias="isSuperUser")
    is_job_user: bool = Field(False, alias="isJobUser")
    organization_ids: list[int] | None = Field(None, alias="organizationIds")
    roles: list[SpiceRole] = Field(default_factory=list)
    suite_access: list[str] | None = Field(None, alias="suiteAccess")
    client: str | None = None


class SpiceContexts(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_detail: SpiceUserContext | None = Field(None, alias="userDetail")
    tenants: object | None = None
