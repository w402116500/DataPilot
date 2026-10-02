from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiEnvelope[T](BaseModel):
    data: T | None
    request_id: str
    error: ErrorBody | None


class Page(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class PageResult[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


class DeleteResult(BaseModel):
    deleted: bool
