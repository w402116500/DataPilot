from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    metadata: str
    datasource_root: str
    artifact_root: str
    datalink: str
