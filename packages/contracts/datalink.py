from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from contracts.datasources import MySqlConnectionConfig, SchemaSummaryRead
from contracts.status import DataSourceType


class DataLinkBuildStatus(StrEnum):
    """DataLink 图谱构建任务的跨进程状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DataLinkErrorCode(StrEnum):
    """DataLink 对主后端和 Agent 暴露的稳定错误码。"""

    DATALINK_UNAVAILABLE = "DATALINK_UNAVAILABLE"
    GRAPH_VERSION_NOT_FOUND = "GRAPH_VERSION_NOT_FOUND"
    DATASOURCE_MISMATCH = "DATASOURCE_MISMATCH"
    INVALID_QUERY = "INVALID_QUERY"
    PATH_OUTSIDE_ROOT = "PATH_OUTSIDE_ROOT"
    BUILD_ALREADY_RUNNING = "BUILD_ALREADY_RUNNING"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_INTERRUPTED = "BUILD_INTERRUPTED"
    MODEL_CONFIG_INVALID = "MODEL_CONFIG_INVALID"
    MODEL_REQUEST_TIMEOUT = "MODEL_REQUEST_TIMEOUT"
    MODEL_UPSTREAM_ERROR = "MODEL_UPSTREAM_ERROR"
    MODEL_RESPONSE_ERROR = "MODEL_RESPONSE_ERROR"
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    SEMANTIC_MAPPING_INVALID = "SEMANTIC_MAPPING_INVALID"
    DRAFT_STALE = "DRAFT_STALE"
    HEAD_STALE = "HEAD_STALE"
    REVISION_INVALID = "REVISION_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class DataLinkNodeType(StrEnum):
    """图谱中允许对外读取的节点类型。"""

    TABLE = "table"
    COLUMN = "column"
    CONCEPT = "concept"
    ENTITY = "entity"


class DataLinkEdgeType(StrEnum):
    """图谱中允许对外读取的关系类型。"""

    CONTAINS = "contains"
    FOREIGN_KEY = "foreign_key"
    JOINABLE = "joinable"
    SEMANTIC_SYNONYM = "semantic_synonym"
    REPRESENTS = "represents"
    HAS_CONCEPT = "has_concept"
    DISTRIBUTION_SIMILAR = "distribution_similar"
    CORRELATED = "correlated"


DataLinkExploreFocus = Literal["schema", "data_profile", "join_paths"]
DataLinkGraphEntryType = Literal["table", "entity"]
DataLinkScalar = str | int | float | bool
DataLinkProvenance = Literal[
    "structural",
    "database_foreign_key",
    "inferred_candidate",
    "semantic_mapping",
    "manual",
    "unknown",
]


class DataLinkContract(BaseModel):
    """DataLink 共享 DTO 的基础约束，拒绝未约定字段。"""

    model_config = ConfigDict(extra="forbid")


class DataLinkColumnProfileRead(DataLinkContract):
    """经脱敏后可用于展示和检索的字段画像。"""

    column_id: str = Field(min_length=1, max_length=300)
    dtype: str = Field(min_length=1, max_length=80)
    semantic_type: str | None = Field(default=None, max_length=120)
    null_rate: float = Field(ge=0, le=1)
    distinct_count: int = Field(ge=0)
    unique_rate: float = Field(ge=0, le=1)
    min_value: DataLinkScalar | None = None
    max_value: DataLinkScalar | None = None
    top_values: list[DataLinkScalar] = Field(default_factory=list, max_length=20)
    sample_values: list[DataLinkScalar] = Field(default_factory=list, max_length=20)


class DataLinkNodeRead(DataLinkContract):
    """图谱节点的受限读取模型，不承载原始数据源路径或向量。"""

    provenance: DataLinkProvenance = "unknown"

    id: str = Field(min_length=1, max_length=300)
    type: DataLinkNodeType
    name: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)
    semantic_type: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    profile: DataLinkColumnProfileRead | None = None


class DataLinkEdgeEvidenceRead(DataLinkContract):
    """关系的来源说明；只返回可展示的摘要，不暴露内部存储结构。"""

    kind: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1000)


class DataLinkEdgeRead(DataLinkContract):
    """图谱关系的受限读取模型。"""

    provenance: DataLinkProvenance = "unknown"
    enabled: bool = True

    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    type: DataLinkEdgeType
    confidence: float | None = Field(ge=0, le=1)
    evidence: DataLinkEdgeEvidenceRead | None = None


class DataLinkJoinPathStepRead(DataLinkContract):
    """一段可用于 Join 解释的受控关系。"""

    provenance: DataLinkProvenance = "unknown"

    source_table: str = Field(min_length=1, max_length=200)
    source_column: str = Field(min_length=1, max_length=200)
    target_table: str = Field(min_length=1, max_length=200)
    target_column: str = Field(min_length=1, max_length=200)
    edge_type: Literal[DataLinkEdgeType.FOREIGN_KEY, DataLinkEdgeType.JOINABLE]
    confidence: float | None = Field(ge=0, le=1)
    evidence: DataLinkEdgeEvidenceRead | None = None


class DataLinkJoinPathRead(DataLinkContract):
    """一条带关系证据的候选 Join 路径。"""

    provenance: DataLinkProvenance = "unknown"

    tables: list[str] = Field(min_length=2, max_length=12)
    steps: list[DataLinkJoinPathStepRead] = Field(min_length=1, max_length=10)
    confidence: float | None = Field(ge=0, le=1)
    evidence: str | None = Field(default=None, max_length=1000)


def validate_relative_ref(value: str) -> str:
    """校验传给 DataLink 的文件引用只能是安全的相对路径。"""

    normalized = value.strip()
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)

    if not normalized:
        raise ValueError("reference must not be empty")
    if "\\" in normalized:
        raise ValueError("reference must use forward slashes")
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("reference must be relative")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError("reference must not contain current or parent path segments")
    return normalized


class DataLinkRebuildRequest(DataLinkContract):
    """主后端请求 DataLink 创建一轮新图谱构建的契约。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    rebuild_key: str = Field(min_length=1, max_length=120)
    source_type: DataSourceType
    source_kind: Literal["file", "connection"] = "file"
    source_ref: str | None = Field(default=None, min_length=1, max_length=500)
    connection_grant: SecretStr | None = Field(default=None, repr=False)
    connection_revision: int = Field(default=0, ge=0)
    schema_revision: int = Field(ge=0)

    @field_validator("source_ref")
    @classmethod
    def _validate_relative_ref(cls, value: str | None) -> str | None:
        return validate_relative_ref(value) if value is not None else None

    @model_validator(mode="after")
    def validate_source(self) -> DataLinkRebuildRequest:
        if self.source_kind == "file":
            if (
                self.source_type not in {DataSourceType.CSV, DataSourceType.SQLITE}
                or self.source_ref is None
                or self.connection_grant is not None
                or self.connection_revision != 0
            ):
                raise ValueError("File builds require only a file source reference")
        elif (
            self.source_type.value != "mysql"
            or self.source_ref is not None
            or self.connection_grant is None
            or not self.connection_grant.get_secret_value().strip()
            or self.connection_revision < 1
        ):
            raise ValueError("Connection builds require a grant and connection revision")
        return self


class DataLinkConnectionGrantConsumeRequest(DataLinkContract):
    """Private service-to-service grant consumption; never log its payload."""

    token: SecretStr = Field(repr=False)
    rebuild_key: str = Field(min_length=1, max_length=120)
    build_id: str = Field(min_length=1, max_length=120)
    datasource_id: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(ge=1)


class DataLinkConnectionGrantRead(DataLinkContract):
    """Private response consumed in memory by the authorized Connector only."""

    datasource_id: str
    source_type: Literal["mysql"] = "mysql"
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(ge=1)
    config: MySqlConnectionConfig = Field(repr=False)
    password: SecretStr = Field(repr=False)
    schema_summary: SchemaSummaryRead = Field(alias="schema")

    @field_serializer("password", when_used="always")
    def serialize_password(self, value: SecretStr) -> str:
        """Internal grant JSON must carry the real secret for the authorized Connector."""

        return value.get_secret_value()


class DataLinkRebuildResult(DataLinkContract):
    """DataLink 接受或复用一轮 rebuild 后返回的受限构建摘要。"""

    build_id: str = Field(min_length=1, max_length=120)
    datasource_id: str = Field(min_length=1, max_length=120)
    status: DataLinkBuildStatus
    requested_schema_revision: int = Field(ge=0)
    connection_revision: int = Field(default=0, ge=0)
    graph_version: str | None = Field(default=None, max_length=120)
    publication_state: Literal["candidate", "published"] = "published"


class DataLinkBuildRead(DataLinkContract):
    """DataLink Build 的可读状态，用于状态查询和主后端投影。"""

    build_id: str = Field(min_length=1, max_length=120)
    datasource_id: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(default=0, ge=0)
    attempt_no: int = Field(ge=1)
    status: DataLinkBuildStatus
    graph_version: str | None = Field(default=None, max_length=120)
    error_code: DataLinkErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=1000)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    origin_kind: Literal["automated", "manual", "restore"] = "automated"
    base_graph_version: str | None = None
    publication_state: Literal["candidate", "published"] = "published"


class DataLinkStatusRead(DataLinkContract):
    """主后端查询某个 DataSource 当前图谱状态时使用的返回契约。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    current_graph_version: str | None = Field(default=None, max_length=120)
    current_build: DataLinkBuildRead | None = None
    head_build: DataLinkBuildRead | None = None
    last_error_code: DataLinkErrorCode | None = None
    last_error_message: str | None = Field(default=None, max_length=1000)


class DataLinkGraphEntryRead(DataLinkContract):
    """数据地图左侧入口的受限节点摘要，只允许表和实体。"""

    id: str = Field(min_length=1, max_length=300)
    type: DataLinkGraphEntryType
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class DataLinkBrowserProfileRead(DataLinkContract):
    """数据地图允许显示的字段统计，不包含任何原始取值或数值范围。"""

    dtype: str = Field(min_length=1, max_length=80)
    semantic_type: str | None = Field(default=None, max_length=120)
    null_rate: float = Field(ge=0, le=1)
    distinct_count: int = Field(ge=0)
    unique_rate: float = Field(ge=0, le=1)


class DataLinkBrowserNodeRead(DataLinkContract):
    """数据地图节点的浏览器投影，与 Agent 检索节点保持独立。"""

    provenance: DataLinkProvenance = "unknown"

    id: str = Field(min_length=1, max_length=300)
    type: DataLinkNodeType
    name: str = Field(min_length=1, max_length=200)
    table: str | None = Field(default=None, max_length=200)
    semantic_type: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    profile: DataLinkBrowserProfileRead | None = None


class DataLinkGraphRead(DataLinkContract):
    """旧整图读取也只使用数据地图安全投影，不返回字段实际取值。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str | None = Field(default=None, max_length=120)
    nodes: list[DataLinkBrowserNodeRead] = Field(default_factory=list, max_length=100)
    edges: list[DataLinkEdgeRead] = Field(default_factory=list, max_length=200)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class DataLinkGraphEntriesRead(DataLinkContract):
    """当前完成图谱版本中可作为浏览根节点的分页入口列表。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    items: list[DataLinkGraphEntryRead] = Field(default_factory=list, max_length=100)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class DataLinkSubgraphRead(DataLinkContract):
    """以一个根节点展开的受限局部图，明确说明是否因展示上限被截断。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    root_node_id: str = Field(min_length=1, max_length=300)
    total_node_count: int = Field(ge=0)
    total_edge_count: int = Field(ge=0)
    nodes: list[DataLinkBrowserNodeRead] = Field(default_factory=list, max_length=80)
    edges: list[DataLinkEdgeRead] = Field(default_factory=list, max_length=150)
    is_truncated: bool
    warnings: list[str] = Field(default_factory=list, max_length=20)


class DataLinkRemoveResult(DataLinkContract):
    """DataLink 删除某个 DataSource 图谱后的幂等返回结果。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    removed: bool


class DataLinkExploreRequest(DataLinkContract):
    """Agent 通过 MCP 调用 datalink_explore 时传入的参数契约。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=2000)
    focus: DataLinkExploreFocus | None = None
    max_nodes: int = Field(default=12, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized


class DataLinkExploreResult(DataLinkContract):
    """datalink_explore 返回的受限语义子图和 Join 证据。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=2000)
    nodes: list[DataLinkNodeRead] = Field(default_factory=list, max_length=50)
    edges: list[DataLinkEdgeRead] = Field(default_factory=list, max_length=100)
    join_paths: list[DataLinkJoinPathRead] = Field(default_factory=list, max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    retrieval_mode: Literal["keyword", "hybrid", "unknown"] = "unknown"
    is_truncated: bool = False


class DataLinkSemanticEvidence(DataLinkContract):
    """运行时允许交给模型的关系证据，不携带内部图谱属性。"""

    kind: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1_000)


class DataLinkSemanticEntity(DataLinkContract):
    """字段映射所涉及的业务实体，只保留模型可读的语义说明。"""

    provenance: DataLinkProvenance = "unknown"

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class DataLinkSemanticEntityMapping(DataLinkContract):
    """一个属性归属到业务实体的受限映射，不包含内部节点标识。"""

    provenance: DataLinkProvenance = "unknown"

    entity: DataLinkSemanticEntity
    entity_to_concept_confidence: float | None = Field(ge=0, le=1)


class DataLinkSemanticConcept(DataLinkContract):
    """字段实际表达的业务属性及其可读说明。"""

    provenance: DataLinkProvenance = "unknown"

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class DataLinkSemanticMapping(DataLinkContract):
    """物理字段到概念、再到实体的完整安全映射。"""

    provenance: DataLinkProvenance = "unknown"

    concept: DataLinkSemanticConcept
    field_to_concept_confidence: float | None = Field(ge=0, le=1)
    entities: list[DataLinkSemanticEntityMapping] = Field(default_factory=list, max_length=20)


class DataLinkSemanticCatalog(DataLinkContract):
    """当前受限结果中可被字段映射引用的概念和实体目录。"""

    concepts: list[DataLinkSemanticConcept] = Field(default_factory=list, max_length=50)
    entities: list[DataLinkSemanticEntity] = Field(default_factory=list, max_length=50)


class DataLinkSemanticField(DataLinkContract):
    """可直接回到物理 Schema 校验的字段语义，不暴露节点 ID 或画像。"""

    provenance: DataLinkProvenance = "unknown"

    table: str = Field(min_length=1, max_length=200)
    column: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)
    semantic_type: str | None = Field(default=None, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    semantic_mappings: list[DataLinkSemanticMapping] = Field(default_factory=list, max_length=20)


class DataLinkSemanticRelationship(DataLinkContract):
    """两端都能落到物理字段的候选关联，保留原始关系类型和证据。"""

    provenance: DataLinkProvenance = "unknown"

    source_table: str = Field(min_length=1, max_length=200)
    source_column: str = Field(min_length=1, max_length=200)
    target_table: str = Field(min_length=1, max_length=200)
    target_column: str = Field(min_length=1, max_length=200)
    edge_type: Literal["foreign_key", "joinable"]
    confidence: float | None = Field(ge=0, le=1)
    evidence: DataLinkSemanticEvidence | None = None


class DataLinkSemanticJoinPathStep(DataLinkContract):
    """一段已脱离内部节点 ID 的 Join 证据。"""

    provenance: DataLinkProvenance = "unknown"

    source_table: str = Field(min_length=1, max_length=200)
    source_column: str = Field(min_length=1, max_length=200)
    target_table: str = Field(min_length=1, max_length=200)
    target_column: str = Field(min_length=1, max_length=200)
    edge_type: Literal["foreign_key", "joinable"]
    confidence: float | None = Field(ge=0, le=1)
    evidence: DataLinkSemanticEvidence | None = None


class DataLinkSemanticJoinPath(DataLinkContract):
    """供合同生成器理解候选关联链的安全路径。"""

    provenance: DataLinkProvenance = "unknown"

    tables: list[str] = Field(min_length=2, max_length=12)
    steps: list[DataLinkSemanticJoinPathStep] = Field(min_length=1, max_length=10)
    confidence: float | None = Field(ge=0, le=1)
    evidence: str | None = Field(default=None, max_length=1_000)


class DataLinkSemanticContext(DataLinkContract):
    """DataLink 的唯一模型可见投影，绝不含样例值、画像或内部节点 ID。"""

    provider: Literal["datalink"] = "datalink"
    mode: Literal["live", "cached"]
    trust: Literal["inferred"] = "inferred"
    graph_version: str = Field(min_length=1, max_length=120)
    semantic_catalog: DataLinkSemanticCatalog = Field(default_factory=DataLinkSemanticCatalog)
    fields: list[DataLinkSemanticField] = Field(default_factory=list, max_length=50)
    relationships: list[DataLinkSemanticRelationship] = Field(default_factory=list, max_length=100)
    join_paths: list[DataLinkSemanticJoinPath] = Field(default_factory=list, max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    def has_schema_guidance(self) -> bool:
        """只有可落到物理 Schema 的内容才值得交给合同生成器。"""

        return bool(self.fields or self.relationships or self.join_paths)


class DataLinkHealthRead(DataLinkContract):
    """DataLink 健康检查的安全摘要，不含路径、密钥或模型响应。"""

    status: Literal["healthy", "degraded"]
    graph_database: Literal["ok", "unavailable"]
    source_root: Literal["ok", "missing"]
    model_configured: bool
    mcp_mounted: bool


class DataLinkAutomaticSemanticsRead(DataLinkContract):
    """人工覆盖前的自动语义值，不含画像样例或向量。"""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    semantic_type: str | None = Field(default=None, max_length=120)


class DataLinkCatalogPrimaryMappingRead(DataLinkContract):
    """列表行用的最高置信已启用 REPRESENTS 摘要，不含画像或内部边 ID。"""

    concept_name: str = Field(min_length=1, max_length=200)
    concept_description: str | None = Field(default=None, max_length=1000)
    entity_name: str | None = Field(default=None, max_length=200)
    field_to_concept_confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: DataLinkProvenance


class DataLinkCatalogMappedColumnRead(DataLinkContract):
    """概念/实体列表用的已映射物理列，不含画像、样例或内部 ID。"""

    table: str | None = Field(default=None, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class DataLinkCatalogItemRead(DataLinkContract):
    node: DataLinkBrowserNodeRead
    provenance: DataLinkProvenance
    mapping_count: int = Field(ge=0)
    relation_count: int = Field(ge=0)
    manual_created: bool = False
    can_reset_node: bool = False
    can_reset_mapping: bool = False
    automatic: DataLinkAutomaticSemanticsRead | None = None
    primary_mapping: DataLinkCatalogPrimaryMappingRead | None = None
    mapped_columns: list[DataLinkCatalogMappedColumnRead] = Field(
        default_factory=list, max_length=50
    )


class DataLinkCatalogRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    items: list[DataLinkCatalogItemRead] = Field(max_length=100)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class DataLinkCatalogMappingRead(DataLinkContract):
    field_to_concept_provenance: DataLinkProvenance = "unknown"
    entity_to_concept_provenance: DataLinkProvenance = "unknown"
    enabled: bool = True

    column: DataLinkBrowserNodeRead
    concept: DataLinkBrowserNodeRead
    entity: DataLinkBrowserNodeRead | None = None
    field_to_concept_confidence: float | None = Field(ge=0, le=1)
    entity_to_concept_confidence: float | None = Field(default=None, ge=0, le=1)


class DataLinkCatalogDetailRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    item: DataLinkCatalogItemRead
    mappings: list[DataLinkCatalogMappingRead] = Field(max_length=100)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class DataLinkRelationRead(DataLinkContract):
    id: str = Field(min_length=1, max_length=300)
    source: DataLinkBrowserNodeRead
    target: DataLinkBrowserNodeRead
    type: DataLinkEdgeType
    confidence: float | None = Field(ge=0, le=1)
    evidence: DataLinkEdgeEvidenceRead | None = None
    provenance: DataLinkProvenance
    enabled: bool = True
    join_eligible: bool


class DataLinkRelationsRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    items: list[DataLinkRelationRead] = Field(max_length=100)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class DataLinkRelationDetailRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    item: DataLinkRelationRead


class DataLinkPreviewRequest(DataLinkContract):
    graph_version: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=2000)
    focus: DataLinkExploreFocus | None = None
    max_nodes: int = Field(default=12, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()


class DataLinkPreviewRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    graph_version: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=2000)
    focus: DataLinkExploreFocus | None = None
    max_nodes: int = Field(ge=1, le=50)
    semantic_context: DataLinkSemanticContext
    retrieval_mode: Literal["keyword", "hybrid", "unknown"]
    is_truncated: bool


class DataLinkChangeType(StrEnum):
    """可持久化的人工语义修订类型。"""

    DISABLE_RELATION = "disable_relation"
    ENABLE_RELATION = "enable_relation"
    ADD_RELATION = "add_relation"
    REPOINT_RELATION = "repoint_relation"
    UPDATE_NODE = "update_node"
    ADD_NODE = "add_node"
    REPLACE_MAPPING = "replace_mapping"
    RESET_NODE = "reset_node"
    RESET_MAPPING = "reset_mapping"
    RESET_RELATION = "reset_relation"


class DataLinkDraftChange(DataLinkContract):
    """单条类型化草稿变更，不接受任意 JSON Patch。"""

    change_type: DataLinkChangeType
    object_key: str = Field(min_length=1, max_length=300)
    source_id: str | None = Field(default=None, max_length=300)
    target_id: str | None = Field(default=None, max_length=300)
    relation_type: Literal["joinable"] | None = None
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    semantic_type: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)
    node_type: Literal["concept", "entity"] | None = None
    target_ids: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_change_fields(self) -> DataLinkDraftChange:
        if self.name is not None and not self.name.strip():
            raise ValueError("Semantic names must not be blank")
        relation_fields = {"source_id", "target_id", "relation_type", "enabled"}
        node_fields = {"name", "description", "aliases", "semantic_type"}
        supplied = {name for name in self.model_fields_set if getattr(self, name) is not None}
        if self.change_type == DataLinkChangeType.ADD_NODE:
            if not self.node_type or not self.name or supplied & (relation_fields | {"target_ids"}):
                raise ValueError("New semantic nodes require name and concept/entity type")
            return self
        if self.change_type == DataLinkChangeType.REPLACE_MAPPING:
            if self.target_ids is None or supplied & (
                relation_fields | node_fields | {"node_type"}
            ):
                raise ValueError("Mapping replacement requires target_ids only")
            if any(not key or len(key) > 300 for key in self.target_ids) or len(
                set(self.target_ids)
            ) != len(self.target_ids):
                raise ValueError("Mapping targets must be unique nonempty node IDs")
            return self
        if supplied & {"node_type", "target_ids"}:
            raise ValueError("Node type and targets apply only to creation or mapping replacement")
        if self.change_type == DataLinkChangeType.UPDATE_NODE:
            if supplied & relation_fields or not supplied & node_fields:
                raise ValueError("Node updates require semantic fields only")
        elif self.change_type in {
            DataLinkChangeType.ADD_RELATION,
            DataLinkChangeType.REPOINT_RELATION,
        }:
            if not self.source_id or not self.target_id or supplied & node_fields:
                raise ValueError("Relation changes require both endpoints and no node fields")
            if self.enabled is None:
                self.enabled = False
        elif supplied & (relation_fields | node_fields):
            raise ValueError("Enable/disable operations require only an object key")
        return self


class DataLinkDraftRead(DataLinkContract):
    datasource_id: str
    base_graph_version: str
    schema_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)
    status: Literal["active", "published", "discarded"]
    changes: list[DataLinkDraftChange] = Field(default_factory=list, max_length=500)


class DataLinkDraftSaveRequest(DataLinkContract):
    base_graph_version: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    expected_draft_revision: int | None = Field(default=None, ge=1)
    changes: list[DataLinkDraftChange] = Field(max_length=500)


class DataLinkPublishRequest(DataLinkContract):
    expected_head: str = Field(min_length=1, max_length=120)
    expected_draft_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120)


class DataLinkPublishRead(DataLinkContract):
    datasource_id: str
    graph_version: str
    previous_graph_version: str
    draft_revision: int = Field(ge=1)
    idempotency_key: str


class DataLinkDraftPreviewRequest(DataLinkContract):
    expected_draft_revision: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=2000)
    focus: DataLinkExploreFocus | None = None
    max_nodes: int = Field(default=12, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()


class DataLinkDraftExploreRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    base_graph_version: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)
    result: DataLinkExploreResult


class DataLinkDraftPreviewRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    base_graph_version: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)
    semantic_context: DataLinkSemanticContext
    retrieval_mode: Literal["keyword"] = "keyword"
    is_truncated: bool


class DataLinkVersionRead(DataLinkContract):
    build_id: str
    graph_version: str
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(default=0, ge=0)
    status: DataLinkBuildStatus
    origin_kind: Literal["automated", "manual", "restore"]
    publication_state: Literal["candidate", "published"]
    base_graph_version: str | None = None
    source_graph_version: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    is_head: bool
    conflict_count: int = Field(ge=0)


class DataLinkVersionsRead(DataLinkContract):
    datasource_id: str
    items: list[DataLinkVersionRead] = Field(max_length=100)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class DataLinkVersionDiffKind(StrEnum):
    """已完成版本相对基线的可读语义差异种类。"""

    NODE_ADDED = "node_added"
    NODE_REMOVED = "node_removed"
    NODE_UPDATED = "node_updated"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"
    RELATION_UPDATED = "relation_updated"
    MAPPING_REPLACED = "mapping_replaced"


class DataLinkVersionDiffRelationRead(DataLinkContract):
    source_id: str = Field(min_length=1, max_length=300)
    source_name: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=300)
    target_name: str = Field(min_length=1, max_length=200)
    type: DataLinkEdgeType
    enabled: bool = True
    provenance: DataLinkProvenance = "unknown"


class DataLinkVersionDiffItemRead(DataLinkContract):
    kind: DataLinkVersionDiffKind
    object_key: str = Field(min_length=1, max_length=300)
    object_kind: Literal["node", "relation", "mapping"]
    name: str = Field(min_length=1, max_length=400)
    automatic: DataLinkAutomaticSemanticsRead | None = None
    effective: DataLinkAutomaticSemanticsRead | None = None
    before_relation: DataLinkVersionDiffRelationRead | None = None
    after_relation: DataLinkVersionDiffRelationRead | None = None
    before_targets: list[str] = Field(default_factory=list, max_length=100)
    after_targets: list[str] = Field(default_factory=list, max_length=100)


class DataLinkVersionDiffRead(DataLinkContract):
    datasource_id: str = Field(min_length=1, max_length=120)
    graph_version: str = Field(min_length=1, max_length=120)
    base_graph_version: str | None = Field(default=None, max_length=120)
    origin_kind: Literal["automated", "manual", "restore"]
    publication_state: Literal["candidate", "published"]
    items: list[DataLinkVersionDiffItemRead] = Field(max_length=500)
    truncated: bool = False


class DataLinkRestoreRequest(DataLinkContract):
    target_graph_version: str = Field(min_length=1, max_length=120)
    expected_head: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=120)


class DataLinkVersionPublishRead(DataLinkContract):
    datasource_id: str
    graph_version: str
    previous_graph_version: str
    source_graph_version: str
    origin_kind: Literal["manual", "restore"]
    idempotency_key: str


class DataLinkRebuildConflictRead(DataLinkContract):
    object_key: str
    object_kind: Literal["node", "relation", "mapping"]
    name: str
    reason: str
    node_type: DataLinkNodeType | None = None
    edge_type: DataLinkEdgeType | None = None


class DataLinkRebuildConflictsRead(DataLinkContract):
    datasource_id: str
    candidate_graph_version: str
    base_graph_version: str
    schema_revision: int = Field(ge=0)
    items: list[DataLinkRebuildConflictRead] = Field(max_length=500)


class DataLinkConflictResolution(DataLinkContract):
    object_key: str = Field(min_length=1, max_length=300)
    action: Literal["discard", "rebind"]
    node_id: str | None = Field(default=None, min_length=1, max_length=300)
    source_id: str | None = Field(default=None, min_length=1, max_length=300)
    target_id: str | None = Field(default=None, min_length=1, max_length=300)
    target_ids: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_binding(self) -> DataLinkConflictResolution:
        endpoints = self.source_id is not None or self.target_id is not None
        if self.target_ids is not None and self.action != "rebind":
            raise ValueError("Target mappings require rebind")
        if self.action == "discard":
            if self.node_id is not None or endpoints:
                raise ValueError("Discard does not accept a new binding")
        elif self.node_id is not None:
            if endpoints:
                raise ValueError("Bind either a node or relation endpoints")
        elif self.source_id is None or self.target_id is None:
            raise ValueError("Rebind requires a node or both relation endpoints")
        return self


class DataLinkResolveCandidateRequest(DataLinkContract):
    candidate_graph_version: str = Field(min_length=1, max_length=120)
    expected_head: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=0)
    connection_revision: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=120)
    resolutions: list[DataLinkConflictResolution] = Field(min_length=1, max_length=500)
