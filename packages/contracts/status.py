from __future__ import annotations

from enum import StrEnum


class DataSourceType(StrEnum):
    CSV = "csv"
    SQLITE = "sqlite"
    MYSQL = "mysql"


class DataSourceStatus(StrEnum):
    UPLOADED = "uploaded"
    INSPECTING = "inspecting"
    SCHEMA_READY = "schema_ready"
    BUILDING_DATALINK = "building_datalink"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class ModelProfileStatus(StrEnum):
    CREATED = "created"
    TESTED = "tested"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class ArtifactType(StrEnum):
    TABLE = "table"
    CHART = "chart"
    MARKDOWN = "markdown"
    FILE = "file"
