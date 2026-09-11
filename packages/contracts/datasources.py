from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_serializer

from contracts.status import DataSourceStatus, DataSourceType

ArtifactUsage = Literal["query_result", "discovery_observation"]


class DataSourceParameter(BaseModel):
    name: str
    label: str
    type: Literal["string", "integer", "boolean"] = "string"
    required: bool = True
    default: str | int | bool | None = None
    secret: bool = False


class MySqlConnectionConfig(BaseModel):
    """Allowlisted connection options; driver arguments and URLs are not accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f/\\]+$")
    username: str = Field(min_length=1, max_length=128)
    tls: bool = True
    connect_timeout_seconds: int = Field(default=10, ge=1, le=30)


class DataSourceCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=4096)


class DataSourceConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    type: Literal["mysql"] = "mysql"
    config: MySqlConnectionConfig
    credentials: DataSourceCredentials


class DataSourceConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_connection_revision: int = Field(ge=1)
    config: MySqlConnectionConfig
    credentials: DataSourceCredentials | None = None


class DataSourceTypeDescriptor(BaseModel):
    """说明当前产品允许创建的数据源类型，不暴露内部 Adapter 实现。"""

    type: DataSourceType
    label: str
    description: str
    enabled: bool
    accepted_extensions: list[str]
    dialect: str
    upload_mode: Literal["file", "connection"]
    parameters: list[DataSourceParameter] = Field(default_factory=list)
    capabilities: dict[str, bool] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):  # preserve legacy file descriptor wire shape
        data = handler(self)
        if self.upload_mode == "file":
            data.pop("parameters", None)
            data.pop("capabilities", None)
        return data


class DataSourceTypeList(BaseModel):
    items: list[DataSourceTypeDescriptor]


class SchemaColumnRead(BaseModel):
    name: str
    type: str
    nullable: bool


class ForeignKeyRead(BaseModel):
    columns: list[str]
    referenced_table: str
    referenced_columns: list[str]


class SchemaTableRead(BaseModel):
    name: str
    columns: list[SchemaColumnRead]
    row_count: int | None = Field(default=None, ge=0)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyRead] = Field(default_factory=list)


class SchemaSummaryRead(BaseModel):
    datasource_id: str
    dialect: str
    tables: list[SchemaTableRead]


class DataSourceRead(BaseModel):
    """DataSource 对外投影，不包含受控文件的实际保存位置。"""

    id: str
    name: str
    description: str | None
    type: DataSourceType
    status: DataSourceStatus
    schema_revision: int = Field(ge=0)
    source_kind: Literal["file", "connection"] = "file"
    connection_revision: int = Field(default=0, ge=0)
    has_credentials: bool = False
    connection_summary: MySqlConnectionConfig | None = None
    mask_fields: list[str] = Field(default_factory=list)
    mask_fields_confirmed: bool = False
    schema_summary: SchemaSummaryRead | None = Field(
        default=None,
        serialization_alias="schema",
    )
    datalink_build_id: str | None
    datalink_graph_version: str | None
    last_error_code: str | None
    last_error_message: str | None
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TableDataRead(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int = Field(ge=0)


class SqlExecutionRead(TableDataRead):
    audit_log_id: str
    artifact_id: str | None
    elapsed_ms: int = Field(ge=0)


class DataSourceDeleteResult(BaseModel):
    datasource_id: str
    status: DataSourceStatus


class DataSourceMaskFieldsUpdate(BaseModel):
    """数据源所有者明确确认的遮蔽列清单。"""

    model_config = ConfigDict(extra="forbid")

    mask_fields: list[str] = Field(default_factory=list, max_length=200)


class DataSourceDescriptionUpdate(BaseModel):
    """数据源补充说明；必须显式传字段，空字符串与 null 都表示清除说明。"""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(max_length=2_000)
