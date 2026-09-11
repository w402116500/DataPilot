from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Literal, Protocol

from contracts.datasources import ArtifactUsage, SchemaSummaryRead, TableDataRead
from contracts.status import DataSourceType


@dataclass(frozen=True)
class FileSourceAccess:
    path: Path


@dataclass(frozen=True, repr=False)
class RelationalSourceAccess:
    host: str
    port: int
    database: str
    username: str
    password: str
    tls: bool = True
    connect_timeout_seconds: int = 10


SourceAccess = FileSourceAccess | RelationalSourceAccess


@dataclass(frozen=True)
class GatewaySourceSnapshot:
    """Run-private immutable metadata; JSON is decoded afresh for each consumer."""

    id: str
    type: str
    status: str
    schema_revision: int
    connection_revision: int
    _schema_json: str = field(repr=False)
    mask_fields_json: tuple[str, ...]
    mask_fields_confirmed: bool
    content_hash: str | None = None

    @property
    def schema_cache_json(self) -> dict:
        return json.loads(self._schema_json)

    @classmethod
    def from_source(cls, source) -> GatewaySourceSnapshot:
        return cls(
            id=source.id,
            type=source.type,
            status=source.status,
            schema_revision=source.schema_revision,
            connection_revision=source.connection_revision or 0,
            _schema_json=json.dumps(source.schema_cache_json),
            mask_fields_json=tuple(source.mask_fields_json or ()),
            mask_fields_confirmed=source.mask_fields_confirmed,
            content_hash=source.content_hash,
        )


@dataclass(frozen=True)
class SourceHandle:
    """Adapter 访问的受控数据源，不接受调用方提供的任意路径。"""

    datasource_id: str
    datasource_type: DataSourceType
    source_path: Path | None = None
    schema: SchemaSummaryRead | None = None
    access: SourceAccess | None = field(default=None, repr=False)
    connection_revision: int = 0

    def __post_init__(self) -> None:
        if self.access is None and self.source_path is not None:
            object.__setattr__(self, "access", FileSourceAccess(self.source_path))
        elif isinstance(self.access, FileSourceAccess):
            object.__setattr__(self, "source_path", self.access.path)
        if self.access is None:
            raise ValueError("Source access is required")
        if isinstance(self.access, RelationalSourceAccess) and self.source_path is not None:
            raise ValueError("Relational access cannot contain a file path")


@dataclass(frozen=True)
class SqlGuardLocation:
    """解析器已确认的 SQL 位置；不存在时不编造坐标。"""

    line: int
    column: int


@dataclass(frozen=True)
class SqlGuardIssue:
    """Guard 对外可传递的脱敏问题说明，不保存底层异常或完整 SQL。"""

    reason_code: str
    subject_kind: Literal["sql_fragment", "table", "column", "function", "statement", "limit"]
    subject: str | None
    location: SqlGuardLocation | None
    message: str
    hint: str | None
    retryable: bool

    def display_message(self) -> str:
        """生成可以进入 Audit 和 ToolCall 的简短人话说明。"""

        parts = [self.message]
        if self.subject:
            parts.append(f"问题位置：{self.subject}")
        if self.hint:
            parts.append(self.hint)
        return "。".join(parts)


@dataclass(frozen=True)
class SqlGuardResult:
    """SQL Guard 的判定结果，包含可审计的规范 SQL 或稳定拒绝原因。"""

    allowed: bool
    normalized_sql: str | None
    reason_code: str | None
    reason: str | None
    statement_type: str | None
    referenced_tables: list[str]
    issue: SqlGuardIssue | None


class QueryCancelToken:
    """由调用方持有的内部停止标记，供网关与 Adapter 中断同一条查询。"""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        """请求停止查询；重复调用不会改变结果。"""

        self._event.set()

    def is_cancelled(self) -> bool:
        """返回当前查询是否已经收到停止请求。"""

        return self._event.is_set()


class DataSourceAdapter(Protocol):
    """不同数据源的最小读取接口；所有实现只能处理受控 SourceHandle。"""

    def inspect_schema(self) -> SchemaSummaryRead: ...

    def preview_table(self, table_name: str, limit: int) -> TableDataRead: ...

    def run_sql(
        self,
        sql: str,
        timeout_seconds: int,
        cancel_token: QueryCancelToken,
    ) -> TableDataRead: ...


class TableArtifactStore(Protocol):
    """Data Gateway 保存脱敏表格结果时依赖的最小 Artifact 能力。"""

    async def create_table_artifact(
        self,
        *,
        result: TableDataRead,
        title: str,
        run_id: str | None,
        session_id: str | None,
        tool_call_id: str | None,
        artifact_usage: ArtifactUsage = "query_result",
    ) -> str: ...
