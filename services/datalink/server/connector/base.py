from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from contracts.datalink import DataLinkErrorCode
from contracts.status import DataSourceType
from pydantic import BaseModel, ConfigDict, Field

MAX_SAMPLE_ROWS = 1_000
type SourceScalar = str | int | float | bool
type SourceRow = Mapping[str, SourceScalar | None]


class SourceColumn(BaseModel):
    """Connector 从实际数据文件读取到的字段定义。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    dtype: str = Field(min_length=1, max_length=80)
    nullable: bool
    is_primary_key: bool


class SourceForeignKey(BaseModel):
    """Connector 从 SQLite 元数据读取到的显式外键。"""

    model_config = ConfigDict(extra="forbid")

    constraint_name: str = Field(min_length=1, max_length=200)
    source_table: str = Field(min_length=1, max_length=200)
    source_column: str = Field(min_length=1, max_length=200)
    target_table: str = Field(min_length=1, max_length=200)
    target_column: str = Field(min_length=1, max_length=200)
    ordinal: int = Field(default=0, ge=0)
    column_count: int = Field(default=1, ge=1)


class SourceTable(BaseModel):
    """数据源中一张真实存在的表及其结构事实。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    columns: list[SourceColumn] = Field(min_length=1, max_length=500)
    foreign_keys: list[SourceForeignKey] = Field(default_factory=list, max_length=500)
    row_count: int | None = Field(default=None, ge=0)


class DatasourceInfo(BaseModel):
    """后续 Extract 与 Profile 共享的受控数据源结构。"""

    model_config = ConfigDict(extra="forbid")

    datasource_id: str = Field(min_length=1, max_length=120)
    source_type: DataSourceType
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(default=0, ge=0)
    tables: list[SourceTable] = Field(min_length=1, max_length=200)


class ConnectorError(Exception):
    """Connector 的安全失败，不携带绝对路径或底层数据库异常文本。"""

    def __init__(self, code: DataLinkErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ReadOnlyConnector(Protocol):
    """CSV 与 SQLite Connector 必须遵守的只读读取接口。"""

    def inspect(self) -> DatasourceInfo:
        """读取完整结构事实，但不暴露受控文件路径。"""

    def sample_rows(self, table_name: str, limit: int = MAX_SAMPLE_ROWS) -> list[SourceRow]:
        """读取有限行样本，供后续画像使用。"""

    def close(self) -> None:
        """Release connection resources on every Build outcome."""
