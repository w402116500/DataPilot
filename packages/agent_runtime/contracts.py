"""Agent Runtime 内部业务 DTO，不承载基础设施对象或敏感内容。"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Final, Literal

from contracts.datalink import DataLinkExploreResult
from contracts.datalink import (
    DataLinkSemanticCatalog as DataLinkSemanticCatalog,
)
from contracts.datalink import (
    DataLinkSemanticConcept as DataLinkSemanticConcept,
)
from contracts.datalink import (
    DataLinkSemanticContext as DataLinkSemanticContext,
)
from contracts.datalink import (
    DataLinkSemanticEntity as DataLinkSemanticEntity,
)
from contracts.datalink import (
    DataLinkSemanticEntityMapping as DataLinkSemanticEntityMapping,
)
from contracts.datalink import (
    DataLinkSemanticEvidence as DataLinkSemanticEvidence,
)
from contracts.datalink import (
    DataLinkSemanticField as DataLinkSemanticField,
)
from contracts.datalink import (
    DataLinkSemanticJoinPath as DataLinkSemanticJoinPath,
)
from contracts.datalink import (
    DataLinkSemanticJoinPathStep as DataLinkSemanticJoinPathStep,
)
from contracts.datalink import (
    DataLinkSemanticMapping as DataLinkSemanticMapping,
)
from contracts.datalink import (
    DataLinkSemanticRelationship as DataLinkSemanticRelationship,
)
from contracts.datasources import ArtifactUsage, SchemaSummaryRead, TableDataRead
from contracts.runs import CompletionKind, RunProtocolId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

CONTEXT_PROJECTION_VERSION: Final[str] = "context-v2"


class AgentRuntimeContract(BaseModel):
    """统一拒绝模型和外层服务传入的未约定字段。"""

    model_config = ConfigDict(extra="forbid")


class AgentErrorCode(StrEnum):
    """Agent Runtime 可安全传播给调用方的分类失败码。"""

    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_OUTPUT_REPAIR_EXHAUSTED = "MODEL_OUTPUT_REPAIR_EXHAUSTED"
    ANALYSIS_SQL_CONTRACT_INVALID = "ANALYSIS_SQL_CONTRACT_INVALID"
    ANALYSIS_CLAIM_VALUE_REQUIRED = "ANALYSIS_CLAIM_VALUE_REQUIRED"
    ANALYSIS_RESULT_INVALID = "ANALYSIS_RESULT_INVALID"
    MODEL_REQUEST_FAILED = "MODEL_REQUEST_FAILED"
    DATALINK_UNAVAILABLE = "DATALINK_UNAVAILABLE"
    DATALINK_REQUEST_INVALID = "DATALINK_REQUEST_INVALID"
    DATA_GATEWAY_BLOCKED = "DATA_GATEWAY_BLOCKED"
    DATA_GATEWAY_FAILED = "DATA_GATEWAY_FAILED"
    SANDBOX_REJECTED = "SANDBOX_REJECTED"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SANDBOX_NETWORK_DENIED = "SANDBOX_NETWORK_DENIED"
    SANDBOX_FAILED = "SANDBOX_FAILED"
    ARTIFACT_REJECTED = "ARTIFACT_REJECTED"
    RUN_CANCELED = "RUN_CANCELED"
    ANALYSIS_LIMIT_REACHED = "ANALYSIS_LIMIT_REACHED"
    ANALYSIS_CLAIM_COMMIT_TIMEOUT = "ANALYSIS_CLAIM_COMMIT_TIMEOUT"
    ANALYSIS_AGENT_TURN_TIMEOUT = "ANALYSIS_AGENT_TURN_TIMEOUT"
    FINAL_ANSWER_TIMEOUT = "FINAL_ANSWER_TIMEOUT"
    FINAL_ANSWER_FACT_MISMATCH = "FINAL_ANSWER_FACT_MISMATCH"
    SCHEMA_REDUNDANT_METADATA_QUERY = "SCHEMA_REDUNDANT_METADATA_QUERY"
    SQL_REPAIR_SAME_STATEMENT = "SQL_REPAIR_SAME_STATEMENT"
    SQL_REPAIR_LIMIT_REACHED = "SQL_REPAIR_LIMIT_REACHED"
    MODEL_RUN_OPENING_INVALID = "MODEL_RUN_OPENING_INVALID"
    RUN_OPENING_TIMEOUT = "RUN_OPENING_TIMEOUT"
    ANALYSIS_PLAN_INVALID = "ANALYSIS_PLAN_INVALID"
    ANALYSIS_PREPARATION_BUDGET_EXHAUSTED = "ANALYSIS_PREPARATION_BUDGET_EXHAUSTED"
    RUN_PROTOCOL_CONFLICT = "RUN_PROTOCOL_CONFLICT"
    RUN_PROTOCOL_UNRESOLVED = "RUN_PROTOCOL_UNRESOLVED"
    CONTEXT_BUDGET_EXHAUSTED = "CONTEXT_BUDGET_EXHAUSTED"


PYTHON_OUTPUT_PATH_RULES = (
    "Python 产物只能声明以下相对路径：charts/*.png、charts/*.svg、"
    "outputs/*.csv、outputs/*.json、outputs/*.md、outputs/*.tsv、outputs/*.txt、"
    "outputs/*.xlsx，或 report.md；每个 output_paths 项必须与脚本实际写入的路径"
    "逐字一致（目录、文件名和扩展名都一致），不能使用工作区根目录文件名。"
    "例如脚本 savefig('charts/result.png') 时必须声明"
    " charts/result.png。"
)

AnalysisRequirementStatus = Literal[
    "pending",
    "queried",
    "validated",
    "evidenced",
    "reported",
    "context_ready",
    "blocked",
]

AnalysisScalar = str | int | float | bool | None
AnalysisSelector = dict[str, str | int | float | bool]
ANALYSIS_VERIFIED_VALUE_LIMIT: Final = 500
AnalysisCapabilityKey = Literal[
    "run_sql_readonly",
    "run_python",
    "artifact_writer",
    "explore_datalink",
    "commit_analysis_claims",
]


class AnalysisSourceConstraint(AgentRuntimeContract):
    """要求查询读取指定的真实数据表。"""

    kind: Literal["source"]
    table: str = Field(min_length=1, max_length=120)


class AnalysisColumnConstraint(AgentRuntimeContract):
    """要求查询使用指定的真实字段。"""

    kind: Literal["column"]
    column: str = Field(min_length=1, max_length=120)

    @field_validator("column")
    @classmethod
    def _reject_wildcard_column(cls, value: str) -> str:
        if value == "*":
            raise ValueError("wildcard is only valid for aggregate columns")
        return value


class AnalysisAggregateConstraint(AgentRuntimeContract):
    """要求查询使用指定聚合及其可选别名。"""

    kind: Literal["aggregate"]
    function: str = Field(min_length=1, max_length=40)
    column: str | None = Field(default=None, max_length=120)
    alias: str | None = Field(default=None, max_length=120)


class AnalysisGroupByConstraint(AgentRuntimeContract):
    """要求查询按指定字段分组。"""

    kind: Literal["group_by"]
    columns: list[str] = Field(min_length=1, max_length=32)


class AnalysisFilterConstraint(AgentRuntimeContract):
    """要求查询包含固定字段过滤。"""

    kind: Literal["filter"]
    column: str = Field(min_length=1, max_length=120)
    operator: Literal["eq", "gt", "gte", "lt", "lte", "is_null", "is_not_null"]
    value: AnalysisScalar

    @model_validator(mode="after")
    def _validate_null_operator(self) -> AnalysisFilterConstraint:
        """NULL 判断不携带比较值，旧的 eq + null 由计划物化阶段兼容。"""

        if self.operator in {"is_null", "is_not_null"} and self.value is not None:
            raise ValueError("NULL filter operators require value=null")
        return self


class AnalysisTimeRangeConstraint(AgentRuntimeContract):
    """要求查询使用固定时间范围。"""

    kind: Literal["time_range"]
    column: str = Field(min_length=1, max_length=120)
    start: str = Field(min_length=1, max_length=80)
    end: str = Field(min_length=1, max_length=80)
    end_inclusive: bool


AnalysisSqlConstraint = Annotated[
    AnalysisSourceConstraint
    | AnalysisColumnConstraint
    | AnalysisAggregateConstraint
    | AnalysisGroupByConstraint
    | AnalysisFilterConstraint
    | AnalysisTimeRangeConstraint,
    Field(discriminator="kind"),
]


class AnalysisValueOperand(AgentRuntimeContract):
    """结果检查使用的字段、字面量或带筛选条件的字段。"""

    field: str | None = Field(default=None, max_length=120)
    literal: AnalysisScalar = None
    selector: AnalysisSelector | None = None

    @model_validator(mode="after")
    def _require_field_or_literal(self) -> AnalysisValueOperand:
        if self.field is None and "literal" not in self.model_fields_set:
            raise ValueError("an operand requires field or literal")
        return self


class AnalysisNonEmptyCheck(AgentRuntimeContract):
    """要求查询结果至少有一行。"""

    kind: Literal["non_empty"]
    required: bool


class AnalysisRowCountCheck(AgentRuntimeContract):
    """要求查询结果行数落在可选范围内。"""

    kind: Literal["row_count"]
    required: bool
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)


class AnalysisNotNullCheck(AgentRuntimeContract):
    """要求指定结果字段不能出现空值。"""

    kind: Literal["not_null"]
    required: bool
    fields: list[str] = Field(min_length=1, max_length=100)


class AnalysisUniqueCheck(AgentRuntimeContract):
    """要求指定结果字段组合唯一。"""

    kind: Literal["unique"]
    required: bool
    fields: list[str] = Field(min_length=1, max_length=100)


class _AnalysisBinaryResultCheck(AgentRuntimeContract):
    """DataFoundry 二元结果检查的共享字段。"""

    required: bool
    left: AnalysisValueOperand
    right: AnalysisValueOperand
    tolerance: float | None = Field(default=None, ge=0)


class AnalysisEqualsCheck(_AnalysisBinaryResultCheck):
    """要求两个结果值相等。"""

    kind: Literal["equals"]


class AnalysisComparisonCheck(_AnalysisBinaryResultCheck):
    """要求两个结果值满足比较关系。"""

    kind: Literal["comparison"]
    operator: Literal["eq", "gt", "gte", "lt", "lte"]


class AnalysisBudgetConservationCheck(_AnalysisBinaryResultCheck):
    """要求两个金额或数量保持守恒。"""

    kind: Literal["budget_conservation"]


class AnalysisSumCheck(AgentRuntimeContract):
    """要求总值等于各分项之和。"""

    kind: Literal["sum"]
    required: bool
    total: AnalysisValueOperand
    parts: list[AnalysisValueOperand] = Field(min_length=1, max_length=100)
    tolerance: float | None = Field(default=None, ge=0)


class AnalysisRatioCheck(AgentRuntimeContract):
    """要求结果比例等于分子除以分母。"""

    kind: Literal["ratio"]
    required: bool
    value: AnalysisValueOperand
    numerator: AnalysisValueOperand
    denominator: AnalysisValueOperand
    scale: float | None = None
    tolerance: float | None = Field(default=None, ge=0)


class AnalysisColumnSumEqualsCheck(AgentRuntimeContract):
    """检查结果列分项之和是否等于每行重复给出的总值。"""

    kind: Literal["column_sum_equals"]
    required: bool
    value_field: str = Field(min_length=1, max_length=120)
    total_field: str = Field(min_length=1, max_length=120)
    tolerance: float = Field(default=0, ge=0)


AnalysisResultCheck = Annotated[
    AnalysisNonEmptyCheck
    | AnalysisRowCountCheck
    | AnalysisNotNullCheck
    | AnalysisUniqueCheck
    | AnalysisEqualsCheck
    | AnalysisSumCheck
    | AnalysisComparisonCheck
    | AnalysisBudgetConservationCheck
    | AnalysisRatioCheck
    | AnalysisColumnSumEqualsCheck,
    Field(discriminator="kind"),
]


class AnalysisScalarClaimExtraction(AgentRuntimeContract):
    """从唯一结果行提取一个可验证事实。"""

    mode: Literal["scalar"]
    name: str = Field(min_length=1, max_length=120)
    field: str = Field(min_length=1, max_length=120)
    selector: AnalysisSelector | None = None
    unit: str | None = Field(default=None, max_length=40)
    required: bool
    tolerance: float | None = Field(default=None, ge=0)


class AnalysisSeriesClaimExtraction(AgentRuntimeContract):
    """从每一条真实结果行展开带维度的事实。"""

    mode: Literal["series"]
    name: str = Field(min_length=1, max_length=120)
    value_field: str = Field(min_length=1, max_length=120)
    dimension_fields: list[str] = Field(min_length=1, max_length=8)
    unit: str | None = Field(default=None, max_length=40)
    required: bool = True
    max_items: int = Field(default=100, ge=1, le=500)
    tolerance: float = Field(default=0, ge=0)


AnalysisClaimExtraction = Annotated[
    AnalysisScalarClaimExtraction | AnalysisSeriesClaimExtraction,
    Field(discriminator="mode"),
]


def analysis_fact_key(field: str, selector: AnalysisSelector | None = None) -> str:
    """为结果字段和精确维度选择生成稳定、可读且不含内部 ID 的事实键。"""

    dimensions = selector or {}
    dimension_text = "|".join(f"{name}={dimensions[name]}" for name in sorted(dimensions))
    return f"{field}|{dimension_text}" if dimension_text else field


class AnalysisAssertionDraft(AgentRuntimeContract):
    """检查项生成器提出的草稿，不负责生成服务端 ID。"""

    description: str = Field(min_length=1, max_length=500)
    required: bool = True
    source_tables: list[str] = Field(default_factory=list, max_length=32)
    dimensions: list[str] = Field(default_factory=list, max_length=32)
    result_columns: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="由 SQL SELECT 投影产生的别名或派生结果列，不是物理 Schema 字段。",
    )
    sql_constraints: list[AnalysisSqlConstraint] = Field(default_factory=list, max_length=32)
    result_checks: list[AnalysisResultCheck] = Field(default_factory=list, max_length=32)
    claim_extractions: list[AnalysisClaimExtraction] = Field(min_length=1, max_length=32)


class AnalysisAssertion(AgentRuntimeContract):
    """一次分析目标的服务端拥有 ID 的可验证检查项。"""

    id: str = Field(pattern=r"^R[1-9][0-9]{0,2}\.A[1-9][0-9]{0,2}$", max_length=120)
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]{0,2}$", max_length=4)
    description: str = Field(min_length=1, max_length=500)
    required: bool = True
    source_tables: list[str] = Field(default_factory=list, max_length=32)
    dimensions: list[str] = Field(default_factory=list, max_length=32)
    result_columns: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="由 SQL SELECT 投影产生的别名或派生结果列，不是物理 Schema 字段。",
    )
    sql_constraints: list[AnalysisSqlConstraint] = Field(default_factory=list, max_length=32)
    result_checks: list[AnalysisResultCheck] = Field(default_factory=list, max_length=32)
    claim_extractions: list[AnalysisClaimExtraction] = Field(min_length=1, max_length=32)


class AnalysisValidationFinding(AgentRuntimeContract):
    """查询或结果校验产生的安全发现。"""

    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)
    severity: Literal["error", "warning"]
    assertion_id: str | None = Field(default=None, max_length=120)


class AnalysisVerifiedValue(AgentRuntimeContract):
    """通过结果检查、可供结论提交引用的事实值。"""

    name: str = Field(min_length=1, max_length=120)
    value: AnalysisScalar
    unit: str | None = Field(default=None, max_length=40)
    tolerance: float = Field(default=0, ge=0)
    assertion_id: str = Field(min_length=1, max_length=120)
    fact_key: str = Field(default="", max_length=300)
    dimensions: AnalysisSelector = Field(default_factory=dict, max_length=16)


class AnalysisClaimValue(AgentRuntimeContract):
    """最终结论提交时携带的数值。"""

    name: str = Field(min_length=1, max_length=120)
    value: AnalysisScalar
    unit: str | None = Field(default=None, max_length=40)
    fact_key: str = Field(default="", max_length=300)
    dimensions: AnalysisSelector = Field(default_factory=dict, max_length=16)


class AnalysisClaimAuditFact(AgentRuntimeContract):
    """Claim 审计摘要中的有限事实，不保存原始结果行。"""

    fact_key: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=120)
    value: AnalysisScalar
    unit: str | None = Field(default=None, max_length=40)
    dimensions: AnalysisSelector = Field(default_factory=dict, max_length=16)


class AnalysisClaimAuditSummary(AgentRuntimeContract):
    """可通过历史事件回放的 Claim 提交摘要。"""

    claim_id: str = Field(pattern=r"^C[1-9][0-9]{0,2}$", max_length=120)
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]{0,2}$", max_length=4)
    target_summary: str = Field(min_length=1, max_length=500)
    evidence_count: int = Field(ge=1, le=64)
    facts: list[AnalysisClaimAuditFact] = Field(
        default_factory=list,
        max_length=ANALYSIS_VERIFIED_VALUE_LIMIT,
    )
    commit_status: Literal["passed"] = "passed"
    fact_validation_status: Literal["passed", "not_checked", "mismatch"] = "passed"


def create_analysis_assertions(
    requirement_id: str,
    drafts: list[AnalysisAssertionDraft],
) -> list[AnalysisAssertion]:
    """按目标分配稳定的服务端检查项 ID。"""

    return [
        AnalysisAssertion(
            id=f"{requirement_id}.A{index}",
            requirement_id=requirement_id,
            description=draft.description.strip(),
            required=draft.required,
            source_tables=list(draft.source_tables),
            dimensions=list(draft.dimensions),
            result_columns=list(draft.result_columns),
            sql_constraints=list(draft.sql_constraints),
            result_checks=list(draft.result_checks),
            claim_extractions=list(draft.claim_extractions),
        )
        for index, draft in enumerate(drafts, start=1)
    ]


class AnalysisClarification(AgentRuntimeContract):
    """用户目标不足以执行时，由目标提取模型生成的一条反问。"""

    reason: Literal["scope_ambiguous"] = "scope_ambiguous"
    question: str = Field(min_length=1, max_length=1_000)


class AnalysisRequirement(AgentRuntimeContract):
    """一次 Run 内由服务端分配 ID 并追踪状态的用户分析目标。"""

    id: str = Field(pattern=r"^R[1-9][0-9]{0,2}$", max_length=4)
    description: str = Field(min_length=1, max_length=500)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)
    assertions: list[AnalysisAssertion] = Field(default_factory=list, max_length=32)
    required: bool = True
    fulfillment_mode: Literal["evidence", "context_only", "blocked"] = "evidence"
    context_sources: list[Literal["schema", "semantic_context"]] = Field(
        default_factory=list, max_length=2
    )
    block_reason: str | None = Field(default=None, max_length=80)
    status: AnalysisRequirementStatus = "pending"
    task_ids: list[str] = Field(default_factory=list, max_length=32)
    query_attempt_ids: list[str] = Field(default_factory=list, max_length=64)
    evidence_binding_ids: list[str] = Field(default_factory=list, max_length=64)
    reported_claim_ids: list[str] = Field(default_factory=list, max_length=64)


class AnalysisQueryAttempt(AgentRuntimeContract):
    """一次查询尝试及其目标、检查、验证和产物关联。"""

    id: str = Field(pattern=r"^Q[1-9][0-9]{0,2}$", max_length=120)
    requirement_ids: list[str] = Field(min_length=1, max_length=16)
    assertion_ids: list[str] = Field(default_factory=list, max_length=32)
    assertions: list[AnalysisAssertion] = Field(default_factory=list, max_length=32)
    sql: str | None = Field(default=None, max_length=20_000)
    expected_columns: list[str] = Field(default_factory=list, max_length=100)
    status: Literal["planned", "validated", "executed", "evidenced"] = "planned"
    valid: bool = False
    artifact_id: str | None = Field(default=None, max_length=120)
    audit_log_id: str | None = Field(default=None, max_length=120)
    result_fields: list[str] = Field(default_factory=list, max_length=100)
    validation_findings: list[AnalysisValidationFinding] = Field(
        default_factory=list,
        max_length=64,
    )
    result_validation_findings: list[AnalysisValidationFinding] = Field(
        default_factory=list,
        max_length=64,
    )
    verified_values: list[AnalysisVerifiedValue] = Field(
        default_factory=list,
        max_length=ANALYSIS_VERIFIED_VALUE_LIMIT,
    )


class AnalysisEvidenceBinding(AgentRuntimeContract):
    """一条查询证据绑定到单个目标的记录。"""

    id: str = Field(min_length=1, max_length=120)
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]{0,2}$", max_length=4)
    query_attempt_id: str = Field(pattern=r"^Q[1-9][0-9]{0,2}$", max_length=120)
    artifact_id: str = Field(min_length=1, max_length=120)
    audit_log_id: str = Field(min_length=1, max_length=120)
    result_fields: list[str] = Field(default_factory=list, max_length=100)
    validation_status: Literal["passed"] = "passed"


class AnalysisReportedClaim(AgentRuntimeContract):
    """已绑定证据并提交给最终答案的目标结论。"""

    id: str = Field(pattern=r"^C[1-9][0-9]{0,2}$", max_length=120)
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]{0,2}$", max_length=4)
    claim: str = Field(min_length=1, max_length=2_000)
    evidence_binding_ids: list[str] = Field(min_length=1, max_length=64)
    values: list[AnalysisClaimValue] = Field(
        default_factory=list,
        max_length=ANALYSIS_VERIFIED_VALUE_LIMIT,
    )


class TaskRequirementLink(AgentRuntimeContract):
    """任务产物与用户目标的内部关联。"""

    task_id: str = Field(min_length=1, max_length=120)
    requirement_ids: list[str] = Field(min_length=1, max_length=16)


class AgentFailure(AgentRuntimeContract):
    """不会回显原始输入、路径、密钥或上游响应正文的失败摘要。"""

    code: AgentErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    context_retry_allowed: bool = False


class OpeningValidationIssue(AgentRuntimeContract):
    """只携带可用于 Opening 修复的脱敏字段级诊断。"""

    path: str = Field(min_length=1, max_length=240)
    error_type: str = Field(min_length=1, max_length=80)
    repair_reason: Literal[
        "shape_invalid",
        "scope_incomplete",
        "discovery_scope_missing",
        "schema_reference_invalid",
        "source_scope_conflict",
        "artifact_conflict",
        "protocol_direction_conflict",
    ] = "shape_invalid"
    actual: str = Field(default="未提供", min_length=1, max_length=240)
    expected: str = Field(default="符合当前计划合同", min_length=1, max_length=400)
    rule: str = Field(default="必须符合当前计划合同", min_length=1, max_length=400)
    action: str = Field(min_length=1, max_length=400)
    suggested_mode: Literal["ready", "discovery", "clarification", "context_only"] | None = None
    occurrences: int = Field(default=1, ge=1, le=64)


class RunOpeningAttemptSummary(AgentRuntimeContract):
    """无效分析 action 的安全形状摘要，不携带模型业务正文。"""

    attempted_protocol: Literal["data-analysis"]
    trusted_mode: (
        Literal["ready", "discovery", "needs_semantic_context", "clarification"] | None
    ) = None
    requirement_count: int = Field(default=0, ge=0, le=64)
    fulfillment_count: int = Field(default=0, ge=0, le=64)
    assertion_count: int = Field(default=0, ge=0, le=256)
    artifact_count: int = Field(default=0, ge=0, le=64)
    allowed_action: Literal["start_data_analysis"] = "start_data_analysis"


class RunOpeningInvalidFailure(AgentFailure):
    """模型返回了 Opening action，但参数没有通过严格合同。"""

    code: Literal[AgentErrorCode.MODEL_RUN_OPENING_INVALID] = (
        AgentErrorCode.MODEL_RUN_OPENING_INVALID
    )
    validation_issues: list[OpeningValidationIssue] = Field(default_factory=list, max_length=12)
    attempt_summary: RunOpeningAttemptSummary | None = None


class AnalysisPlanInvalidFailure(AgentFailure):
    """分析计划未通过服务端语义合同，并携带脱敏修复诊断。"""

    code: Literal[AgentErrorCode.ANALYSIS_PLAN_INVALID] = AgentErrorCode.ANALYSIS_PLAN_INVALID
    validation_issues: list[OpeningValidationIssue] = Field(default_factory=list, max_length=12)


class SqlFailureLocation(AgentRuntimeContract):
    """SQL 解析器确实提供时才写入的安全位置。"""

    line: int = Field(ge=1)
    column: int = Field(ge=1)


class SqlExecutionFailure(AgentRuntimeContract):
    """仅用于 SQL Port 的可审计失败，不混入其他基础设施失败。"""

    code: AgentErrorCode
    reason_code: str = Field(min_length=1, max_length=80)
    subject_kind: Literal["sql_fragment", "table", "column", "function", "statement", "limit"]
    subject: str | None = Field(default=None, max_length=160)
    location: SqlFailureLocation | None = None
    message: str = Field(min_length=1, max_length=500)
    hint: str | None = Field(default=None, max_length=500)
    audit_log_id: str = Field(min_length=1, max_length=120)
    execution_status: Literal["not_started"] = "not_started"
    retryable: bool


class WarningCode(StrEnum):
    """不会改变 Run 成败、但应进入最终回答的安全警告类型。"""

    DATALINK_SCHEMA_ONLY = "DATALINK_SCHEMA_ONLY"
    DATALINK_NO_MATCH_SCHEMA_ONLY = "DATALINK_NO_MATCH_SCHEMA_ONLY"
    SCRIPT_ENHANCEMENT_FAILED = "SCRIPT_ENHANCEMENT_FAILED"
    SCRIPT_OUTPUT_REJECTED = "SCRIPT_OUTPUT_REJECTED"
    OUTCOME_INCOMPLETE = "OUTCOME_INCOMPLETE"
    ASSERTION_BLOCKED = "ASSERTION_BLOCKED"


class AnalysisWarning(AgentRuntimeContract):
    """最终回答可以展示的短警告，不带底层异常或原始数据。"""

    code: WarningCode
    message: str = Field(min_length=1, max_length=500)


class AgentArtifactType(StrEnum):
    """阶段四内部可引用的正式产物类型。"""

    TABLE = "table"
    CHART = "chart"
    MARKDOWN = "markdown"
    FILE = "file"


class ArtifactRef(AgentRuntimeContract):
    """Runtime 只持有 Artifact 标识和安全摘要，不持有文件地址。"""

    artifact_id: str = Field(min_length=1, max_length=120)
    type: AgentArtifactType
    title: str = Field(min_length=1, max_length=200)
    source_tool_call_id: str | None = Field(default=None, min_length=1, max_length=120)


class AnalysisClarificationDraft(AgentRuntimeContract):
    """跨 Run 传递的待澄清事项；不让 Finalizer 再解析自然语言。"""

    question: str = Field(min_length=1, max_length=1_000)
    missing_items: list[
        Literal[
            "metric",
            "time_range",
            "group_by",
            "entity",
            "comparison_baseline",
            "exploration_focus",
            "data_scope",
            "output_format",
        ]
    ] = Field(min_length=1, max_length=12)
    scope_summary: str | None = Field(default=None, max_length=1_000)


class HistoricalAnswerSummary(AgentRuntimeContract):
    """历史回答的安全摘要，只用于理解追问，不能作为当前 Run 证据。"""

    topic: str = Field(min_length=1, max_length=300)
    content_text: str = Field(min_length=1, max_length=1_200)
    provenance: Literal["historical_answer_summary"] = "historical_answer_summary"
    data_freshness: Literal["historical_not_current", "not_queried"] = "historical_not_current"


class SessionPreferenceProjection(AgentRuntimeContract):
    """Session 级、可跨 DataSource 继承的有限偏好。"""

    response_language: Literal["zh-CN", "en-US"] | None = None
    verbosity: Literal["concise", "balanced", "detailed"] | None = None
    answer_format: Literal["plain", "markdown", "table"] | None = None
    chart_preference: Literal["allow", "avoid", "required"] | None = None


class RecentUserTurnProjection(AgentRuntimeContract):
    """近期用户原话；assistant 原文不能复用此类型。"""

    role: Literal["user"] = "user"
    content_text: str = Field(min_length=1, max_length=800)


class DatasourceIdentityProjection(AgentRuntimeContract):
    """安全 DataSource 身份，不包含内部 ID、路径或凭据。"""

    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=40)
    dialect: str = Field(min_length=1, max_length=40)
    schema_revision: int = Field(ge=1)
    description: str | None = Field(default=None, max_length=2_000)


class SafeSchemaColumnProjection(AgentRuntimeContract):
    """模型可见的字段索引，不包含样例和值域。"""

    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=120)
    nullable: bool


class SafeSchemaTableProjection(AgentRuntimeContract):
    """模型可见的表索引，只保留结构性元数据。"""

    name: str = Field(min_length=1, max_length=200)
    columns: list[SafeSchemaColumnProjection] = Field(max_length=256)
    primary_key: list[str] = Field(default_factory=list, max_length=32)
    foreign_keys: list[dict[str, object]] = Field(default_factory=list, max_length=64)


class SafeSchemaIndexProjection(AgentRuntimeContract):
    """去掉 DataSource ID、row_count 和样例数据后的 Schema。"""

    dialect: str = Field(min_length=1, max_length=40)
    tables: list[SafeSchemaTableProjection] = Field(max_length=128)


class ConversationContext(AgentRuntimeContract):
    """Run 固定的当前会话背景；它只能帮助理解追问，不能充当数据证据。"""

    session_preferences: SessionPreferenceProjection = Field(
        default_factory=SessionPreferenceProjection
    )
    recent_user_turns: list[RecentUserTurnProjection] = Field(default_factory=list, max_length=6)
    datasource_identity: DatasourceIdentityProjection | None = None
    pending_clarification: AnalysisClarificationDraft | None = None
    historical_summaries: list[HistoricalAnswerSummary] = Field(default_factory=list, max_length=3)
    load_status: Literal["empty", "ready", "degraded"] = "empty"


class OpeningContextProjection(AgentRuntimeContract):
    """Opening 唯一输入投影；不与其它阶段共享完整上下文。"""

    question: str = Field(min_length=1, max_length=10_000)
    session_preferences: SessionPreferenceProjection = Field(
        default_factory=SessionPreferenceProjection
    )
    pending_clarification: AnalysisClarificationDraft | None = None
    recent_user_turns: list[RecentUserTurnProjection] = Field(default_factory=list, max_length=6)
    historical_summaries: list[HistoricalAnswerSummary] = Field(default_factory=list, max_length=3)
    datasource_identity: DatasourceIdentityProjection
    schema_index: SafeSchemaIndexProjection


class OpeningRepairProjection(AgentRuntimeContract):
    """Opening 形状修复的最小安全输入。"""

    question: str = Field(min_length=1, max_length=10_000)
    schema_index: SafeSchemaIndexProjection
    qualified_columns: list[str] = Field(
        default_factory=list,
        max_length=512,
        description="当前冻结 Schema 中可直接复制的完整 table.column 字段清单。",
    )
    validation_issues: list[OpeningValidationIssue] = Field(max_length=8)
    response_shape: Literal["text_or_single_start_data_analysis"] = (
        "text_or_single_start_data_analysis"
    )
    plan_skeleton: dict[str, object] = Field(default_factory=dict, max_length=16)


class SemanticResolvedFollowup(AgentRuntimeContract):
    """一次成功的 DataLink 语义补充。"""

    kind: Literal["semantic_resolved"] = "semantic_resolved"
    semantic_context: DataLinkSemanticContext


class SemanticDegradedFollowup(AgentRuntimeContract):
    """DataLink 不可用时的一次安全降级结果。"""

    kind: Literal["semantic_degraded"] = "semantic_degraded"
    warning: AnalysisWarning


class DiscoveryObservationProjection(AgentRuntimeContract):
    """Discovery 定稿可见的观察摘要，不暴露 Tool/Audit/Artifact ID。"""

    columns: list[str] = Field(default_factory=list, max_length=100)
    row_count: int = Field(ge=0)
    rows_truncated: bool = False


class DiscoveryObservedFollowup(AgentRuntimeContract):
    """一次受限 Discovery 的观察摘要。"""

    kind: Literal["discovery_observed"] = "discovery_observed"
    observations: list[DiscoveryObservationProjection] = Field(min_length=1, max_length=4)


PlanFollowupOutcome = Annotated[
    SemanticResolvedFollowup | SemanticDegradedFollowup | DiscoveryObservedFollowup,
    Field(discriminator="kind"),
]


class PlanFinalizationProjection(AgentRuntimeContract):
    """计划定稿只接收初始计划、必要 Schema 和恰好一个 follow-up。"""

    question: str = Field(min_length=1, max_length=10_000)
    initial_plan: AnalysisPlanningDraft
    required_schema: SafeSchemaIndexProjection
    followup_outcome: PlanFollowupOutcome


class AgentWorkingSetProjection(AgentRuntimeContract):
    """Agent 每轮从结构化 Run 状态重建的最小输入。"""

    question: str = Field(min_length=1, max_length=10_000)
    current_requirement_ids: list[str] = Field(default_factory=list, max_length=16)
    verified_values: list[AnalysisVerifiedValue] = Field(default_factory=list, max_length=500)
    incomplete_goals: list[str] = Field(default_factory=list, max_length=16)
    pending_artifacts: dict[str, int] = Field(default_factory=dict, max_length=8)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class FinalAnswerProjection(AgentRuntimeContract):
    """最终答案只接收当前 Run 的已提交事实和安全摘要。"""

    question: str = Field(min_length=1, max_length=10_000)
    accepted_goals: list[str] = Field(default_factory=list, max_length=16)
    schema_index: SafeSchemaIndexProjection | None = None
    submitted_claims: list[AnalysisClaimAuditSummary] = Field(default_factory=list, max_length=16)
    artifact_summaries: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    incomplete_goals: list[str] = Field(default_factory=list, max_length=16)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class RunContext(AgentRuntimeContract):
    """Run 启动时固定的只读分析上下文。"""

    run_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
    datasource_id: str = Field(min_length=1, max_length=120)
    model_profile_id: str = Field(min_length=1, max_length=120)
    model_name: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=1)
    connection_revision: int = Field(default=0, ge=0)
    datasource_display_name: str | None = Field(default=None, max_length=200)
    datasource_description: str | None = Field(default=None, max_length=2_000)
    datasource_type: str | None = Field(default=None, max_length=40)
    query_dialect: str | None = Field(default=None, max_length=40)
    datalink_graph_version: str | None = Field(default=None, max_length=120)
    input_snapshot_ref: str | None = Field(default=None, min_length=1, max_length=500)
    input_filename: str | None = Field(default=None, min_length=1, max_length=200)
    mask_fields: list[str] = Field(default_factory=list, max_length=200)
    question: str = Field(min_length=1, max_length=10_000)
    context_projection_version: str = Field(
        default=CONTEXT_PROJECTION_VERSION, min_length=1, max_length=40
    )
    context_through_message_position: int = Field(default=0, ge=0)
    session_preferences_revision: int = Field(default=0, ge=0)
    datasource_context_revision: int = Field(default=0, ge=0)
    pending_id: str | None = Field(default=None, max_length=120)
    pending_revision: int = Field(default=0, ge=0)
    historical_summary_ids: list[str] = Field(default_factory=list, max_length=3)
    context_snapshot_hash: str | None = Field(default=None, max_length=128)
    context_load_status: Literal["empty", "ready", "degraded"] = "empty"
    # 为阶段四已有的测试和内部构造保留空默认值；阶段五 Resolver 始终会显式冻结它。
    conversation_context: ConversationContext = Field(default_factory=ConversationContext)


class SchemaContext(AgentRuntimeContract):
    """与当前 Run 版本一致的 Schema 快照。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=1)
    schema_summary: SchemaSummaryRead

    @field_validator("schema_summary", mode="before")
    @classmethod
    def _require_validated_schema(cls, value: object) -> SchemaSummaryRead:
        """只接受 Data Gateway 已验证的共享 Schema DTO，拒绝原始字典绕过边界。"""

        if not isinstance(value, SchemaSummaryRead):
            raise ValueError("schema_summary must be a validated SchemaSummaryRead")
        return value

    @model_validator(mode="after")
    def _validate_datasource_match(self) -> SchemaContext:
        """阻止其他数据源的 Schema 快照混入当前 Run。"""

        if self.schema_summary.datasource_id != self.datasource_id:
            raise ValueError("schema_summary does not belong to datasource_id")
        return self


class SqlExecutionResult(AgentRuntimeContract):
    """Data Gateway 成功执行后的脱敏结果摘要和可追溯引用。"""

    result: TableDataRead
    audit_log_id: str = Field(min_length=1, max_length=120)
    artifact_id: str = Field(min_length=1, max_length=120)
    elapsed_ms: int = Field(ge=0)

    @field_validator("result", mode="before")
    @classmethod
    def _require_validated_result(cls, value: object) -> TableDataRead:
        """查询结果必须来自 Data Gateway 的脱敏 DTO，不能由原始字典直接注入。"""

        if not isinstance(value, TableDataRead):
            raise ValueError("result must be a validated TableDataRead")
        return value


class AnalysisOutcome(AgentRuntimeContract):
    """Graph 的最终返回值；Run 终态和 SSE 不在这里决定。"""

    protocol_id: RunProtocolId = "data-analysis"
    answer: str = Field(min_length=1, max_length=10_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    warnings: list[AnalysisWarning] = Field(default_factory=list, max_length=20)
    claim_audits: list[AnalysisClaimAuditSummary] = Field(default_factory=list, max_length=16)
    clarification: AnalysisClarificationDraft | None = None
    consumes_pending: bool = False
    observations_performed: bool = False
    completion_kind: CompletionKind = "completed"
    incomplete_reason: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _validate_completion_kind(self) -> AnalysisOutcome:
        if self.completion_kind == "partial" and not self.incomplete_reason:
            raise ValueError("partial outcome requires incomplete_reason")
        if (
            self.completion_kind in {"completed", "clarification"}
            and self.incomplete_reason is not None
        ):
            raise ValueError("completed or clarification outcome cannot have incomplete_reason")
        if self.completion_kind == "clarification" and self.clarification is None:
            raise ValueError("clarification outcome requires a clarification draft")
        if self.completion_kind != "clarification" and self.clarification is not None:
            raise ValueError("only clarification outcome may contain a clarification draft")
        return self


class DiscoveryObservation(AgentRuntimeContract):
    """受限探索查询的安全摘要，不包含 SQL、原始行或可提交事实。"""

    tool_call_id: str = Field(min_length=1, max_length=120)
    audit_log_id: str = Field(min_length=1, max_length=120)
    artifact_id: str | None = Field(default=None, max_length=120)
    columns: list[str] = Field(default_factory=list, max_length=100)
    row_count: int = Field(ge=0)
    rows_truncated: bool = False


class GraphState(AgentRuntimeContract):
    """动态工具循环的只读运行结果，不保存固定步骤状态。"""

    run_context: RunContext
    schema_context: SchemaContext
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    warnings: list[AnalysisWarning] = Field(default_factory=list, max_length=20)
    discovery_observations: list[DiscoveryObservation] = Field(default_factory=list, max_length=4)
    outcome: AnalysisOutcome
    context_retry_count: int = Field(default=0, ge=0, le=1)
    context_compaction_count: int = Field(default=0, ge=0, le=64)
    working_set_count: int = Field(default=0, ge=0, le=64)
    blocked_assertion_count: int = Field(default=0, ge=0, le=32)


class SchemaLoadRequest(AgentRuntimeContract):
    """读取当前 Run 固定 Schema 的请求。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=1)


class DataLinkExploreCommand(AgentRuntimeContract):
    """通过 MCP 查询指定图谱版本的业务请求，适配层自行管理缓存。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    schema_revision: int = Field(ge=1)
    graph_version: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=2_000)
    focus: Literal["schema", "data_profile", "join_paths"] | None = None
    max_nodes: int = Field(default=12, ge=1, le=50)


class DataLinkExploreResponse(AgentRuntimeContract):
    """DataLink 检索的安全结果，明确标记是实时结果还是固定版本缓存。"""

    result: DataLinkExploreResult
    cache_hit: bool


class SqlExecutionRequest(AgentRuntimeContract):
    """Agent 对 Data Gateway 的唯一分析查询请求。"""

    datasource_id: str = Field(min_length=1, max_length=120)
    run_id: str = Field(min_length=1, max_length=120)
    tool_call_id: str | None = Field(default=None, max_length=120)
    repaired_from_audit_id: str | None = Field(default=None, max_length=120)
    sql: str = Field(min_length=1, max_length=20_000)
    max_rows: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="仅用于 Discovery 的服务端结果行上限；普通分析由 Gateway 默认策略治理。",
    )
    artifact_usage: ArtifactUsage = Field(
        default="query_result",
        description="仅由服务端 Discovery 路径标记观察型表格产物；模型不能填写。",
    )
    requirement_ids: list[str] = Field(default_factory=list, max_length=16)
    assertion_ids: list[str] = Field(default_factory=list, max_length=32)
    expected_columns: list[str] = Field(default_factory=list, max_length=100)


class SandboxExecutionRequest(AgentRuntimeContract):
    """提交给沙盒的唯一脚本入口，不携带容器参数、路径或原始输出需求。"""

    workspace_id: str = Field(min_length=1, max_length=120)
    command: tuple[Literal["python"], Literal["analysis.py"]]
    output_paths: list[str] = Field(default_factory=list, max_length=20)
    purpose: str = Field(min_length=1, max_length=500)


class SandboxExecutionStatus(StrEnum):
    """沙盒执行完成后可由 Graph 分支处理的状态。"""

    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    NETWORK_DENIED = "network_denied"
    CANCELED = "canceled"


class SandboxExecutionResult(AgentRuntimeContract):
    """沙盒只返回状态、耗时和安全结果相对引用，不回传原始 stdout/stderr。"""

    status: SandboxExecutionStatus
    elapsed_ms: int = Field(ge=0)
    failure: AgentFailure | None = None
    exit_code: int | None = Field(default=None, ge=0, le=255)
    stdout: str = Field(default="", max_length=8_000)
    stderr: str = Field(default="", max_length=8_000)

    @model_validator(mode="after")
    def _validate_status_failure_pair(self) -> SandboxExecutionResult:
        """让 Graph 可只按稳定状态和失败码分支，不依赖底层异常文本。"""

        expected_codes = {
            SandboxExecutionStatus.FAILED: AgentErrorCode.SANDBOX_FAILED,
            SandboxExecutionStatus.REJECTED: AgentErrorCode.SANDBOX_REJECTED,
            SandboxExecutionStatus.TIMED_OUT: AgentErrorCode.SANDBOX_TIMEOUT,
            SandboxExecutionStatus.NETWORK_DENIED: AgentErrorCode.SANDBOX_NETWORK_DENIED,
            SandboxExecutionStatus.CANCELED: AgentErrorCode.RUN_CANCELED,
        }
        if self.status is SandboxExecutionStatus.COMPLETED:
            if self.failure is not None:
                raise ValueError("completed sandbox execution must not contain a failure")
            return self
        if self.failure is None or self.failure.code is not expected_codes[self.status]:
            raise ValueError("sandbox failure must match the execution status")
        return self


class ArtifactRegistration(AgentRuntimeContract):
    """登记模型明确声明、且已经通过工作区检查的 Python 产物。"""

    workspace_id: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(min_length=1, max_length=500)
    type: Literal[AgentArtifactType.CHART, AgentArtifactType.MARKDOWN, AgentArtifactType.FILE]
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    source_tool_call_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        """只允许模型声明的输出目录相对引用，输入和脚本永远不能登记。"""

        return _validate_workspace_relative_ref(value)

    @model_validator(mode="after")
    def _validate_artifact_path_for_type(self) -> ArtifactRegistration:
        """按 Artifact 类型收紧可登记的安全目录。"""

        if not _is_allowed_artifact_path(self.relative_path, self.type):
            raise ValueError("relative_path is not allowed for the artifact type")
        return self


def _is_safe_workspace_path_candidate(value: str) -> bool:
    """路径参数只能是工作区内的正斜杠相对引用，普通命令名也按此规则通过。"""

    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    return not (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or value.startswith("~")
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    )


def _validate_workspace_relative_ref(value: str) -> str:
    """规范化并校验运行工作区内的正斜杠相对文件引用。"""

    normalized = value.strip()
    if (
        not normalized
        or normalized.endswith("/")
        or not _is_safe_workspace_path_candidate(normalized)
        or "\\" in normalized
    ):
        raise ValueError("reference must be a safe relative path")
    return normalized


def validate_python_output_path(value: str) -> str:
    """校验模型声明的 Python 输出路径，并保持 Graph 与工作区规则一致。"""

    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise ValueError("Python 输出路径必须是无首尾空白的正斜杠相对路径")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Python 输出路径必须是安全的相对路径")
    suffix = path.suffix
    if value == "report.md":
        return value
    if value.startswith("charts/") and suffix in {".png", ".svg"}:
        return value
    if value.startswith("outputs/") and suffix in _OUTPUT_FILE_SUFFIXES:
        return value
    raise ValueError("Python 输出路径不在允许的目录或格式范围内")


def _is_allowed_artifact_path(path: str, artifact_type: AgentArtifactType) -> bool:
    """按类型限制模型声明的输出目录和文件后缀。"""

    if artifact_type is AgentArtifactType.CHART:
        return path.startswith("charts/") and path.endswith((".png", ".svg"))
    if artifact_type is AgentArtifactType.MARKDOWN:
        return path == "report.md" or (path.startswith("outputs/") and path.endswith(".md"))
    return path.startswith("outputs/") and path.endswith(_OUTPUT_FILE_SUFFIXES)


_SAFE_EVENT_STRING = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_OUTPUT_FILE_SUFFIXES = (
    ".csv",
    ".json",
    ".md",
    ".png",
    ".svg",
    ".tsv",
    ".txt",
    ".xlsx",
)


def _is_safe_event_string(value: str) -> bool:
    """事件字符串只保留稳定 ID、类型、方言和错误码可用的有限字符集。"""

    return _SAFE_EVENT_STRING.fullmatch(value) is not None


class AgentToolName(StrEnum):
    """模型可以按需选择的受控工具。"""

    SQL = "run_sql_readonly"
    PYTHON = "run_python"
    DATALINK = "explore_datalink"
    ANALYSIS_COMMIT = "commit_analysis_claims"
    UNKNOWN = "unknown_tool"


class FinalMarkdownPayload(AgentRuntimeContract):
    """所有最终答案输出模式共享的完整 Markdown 正文。"""

    markdown: str = Field(min_length=1, max_length=10_000)


class ToolObservation(AgentRuntimeContract):
    """写回模型的有限工具观察，不承载完整数据或文件正文。"""

    tool_call_id: str = Field(min_length=1, max_length=120)
    tool_name: AgentToolName
    status: Literal["succeeded", "failed"]
    summary: dict[str, object] = Field(default_factory=dict, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class AgentLoopResult(AgentRuntimeContract):
    """动态工具循环的内部结果。"""

    answer: str = Field(default="", max_length=10_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    warnings: list[AnalysisWarning] = Field(default_factory=list, max_length=20)


AnalysisOutcome.model_rebuild()


class AnalysisGoalDraft(AgentRuntimeContract):
    """开放目标的共享字段，不绑定行业分类或固定标题。"""

    description: str = Field(min_length=1, max_length=500)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)


class AnalysisEvidenceFulfillmentDraft(AgentRuntimeContract):
    mode: Literal["evidence"]
    assertions: list[AnalysisAssertionDraft] = Field(min_length=1, max_length=12)


class AnalysisContextOnlyFulfillmentDraft(AgentRuntimeContract):
    mode: Literal["context_only"]
    sources: list[Literal["schema", "semantic_context"]] = Field(min_length=1, max_length=2)


class AnalysisBlockedFulfillmentDraft(AgentRuntimeContract):
    mode: Literal["blocked"]
    reason: Literal[
        "required_data_unavailable",
        "operation_not_allowed",
        "capability_unavailable",
        "user_constraint_conflict",
    ]
    detail: str = Field(min_length=1, max_length=500)


AnalysisFulfillmentDraft = Annotated[
    AnalysisEvidenceFulfillmentDraft
    | AnalysisContextOnlyFulfillmentDraft
    | AnalysisBlockedFulfillmentDraft,
    Field(discriminator="mode"),
]


class AnalysisRequirementPlanDraft(AnalysisGoalDraft):
    fulfillment: AnalysisFulfillmentDraft


class AnalysisArtifactRequirementDraft(AgentRuntimeContract):
    kind: Literal["chart", "markdown", "file"]
    minimum_count: int = Field(default=1, ge=1, le=8)
    description: str = Field(min_length=1, max_length=300)


class AnalysisExecutionConstraints(AgentRuntimeContract):
    """Opening 可声明的执行约束；实际能力仍由 Runtime 再次校验。"""

    forbidden_tools: list[Literal["run_sql_readonly", "run_python", "explore_datalink"]] = Field(
        default_factory=list, max_length=3
    )
    required_artifacts: list[AnalysisArtifactRequirementDraft] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "每条产物要求描述一种产物类型或交付物；最多 8 条，"
            "同一 kind 的数量由 minimum_count 表示。"
        ),
    )
    forbidden_artifact_kinds: list[Literal["chart", "markdown", "file"]] = Field(
        default_factory=list, max_length=3
    )


class DiscoveryScopeDraft(AgentRuntimeContract):
    """Discovery 只能在服务端校验过的表/字段白名单内探索一次。"""

    tables: list[str] = Field(min_length=1, max_length=8)
    columns: list[str] = Field(min_length=1, max_length=64)
    max_rows: int = Field(default=100, ge=1, le=500)


class DiscoveryScopeProjection(AgentRuntimeContract):
    """Discovery 模型可见的目标和 allowlist，不携带内部标识。"""

    objective: str = Field(min_length=1, max_length=1_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)
    tables: list[str] = Field(min_length=1, max_length=8)
    columns: list[str] = Field(min_length=1, max_length=64)
    max_rows: int = Field(default=100, ge=1, le=500)


class AnalysisSemanticRequest(AgentRuntimeContract):
    """需要 DataLink 时只保留受限检索意图，不允许模型提交内部标识。"""

    query: str = Field(min_length=1, max_length=2_000)
    focus: Literal["schema", "data_profile", "join_paths"] | None = None
    max_nodes: int = Field(default=12, ge=1, le=50)


class AnalysisPlanningDraft(AgentRuntimeContract):
    """开放式分析计划；mode 是执行方式，不是业务意图分类。"""

    mode: Literal["ready", "discovery", "needs_semantic_context", "clarification"]
    # Runtime only accepts this flag when the frozen Session context actually
    # contains a pending clarification for the same DataSource.
    consumes_pending: bool = False
    requirements: list[AnalysisRequirementPlanDraft] = Field(
        default_factory=list,
        max_length=16,
        description=(
            "ready、discovery 和 needs_semantic_context 必须至少包含一个目标；"
            "只有 clarification 可以不包含目标。每个目标必须有 acceptance_criteria。"
        ),
    )
    execution_constraints: AnalysisExecutionConstraints = Field(
        default_factory=AnalysisExecutionConstraints
    )
    discovery_scope: DiscoveryScopeDraft | None = None
    semantic_request: AnalysisSemanticRequest | None = None
    clarification: AnalysisClarificationDraft | None = None

    @model_validator(mode="after")
    def _validate_mode_shape(self) -> AnalysisPlanningDraft:
        if self.mode == "clarification":
            if self.requirements or self.clarification is None:
                raise ValueError("clarification plan requires only a clarification")
            if self.semantic_request is not None:
                raise ValueError("clarification plan cannot request semantic context")
            if self.discovery_scope is not None:
                raise ValueError("clarification plan cannot include discovery scope")
            if self.execution_constraints.required_artifacts:
                raise ValueError("clarification plan cannot require artifacts")
            return self
        if not self.requirements:
            raise ValueError("analysis plan requires at least one goal")
        if self.clarification is not None:
            raise ValueError("non-clarification plan cannot contain clarification")
        if self.mode != "needs_semantic_context" and self.semantic_request is not None:
            raise ValueError("semantic request is only valid for needs_semantic_context")
        if self.mode != "discovery" and self.discovery_scope is not None:
            raise ValueError("discovery scope is only valid for discovery mode")
        if self.mode == "needs_semantic_context" and self.semantic_request is None:
            raise ValueError("semantic context plan requires a semantic request")
        if self.mode == "discovery" and self.discovery_scope is None:
            raise ValueError("discovery plan requires a discovery scope")
        if self.mode == "discovery" and any(
            item.fulfillment.mode == "evidence" for item in self.requirements
        ):
            raise ValueError(
                "discovery plan cannot contain evidence assertions; "
                "use ready for executable targets"
            )
        if self.mode == "discovery" and self.execution_constraints.required_artifacts:
            raise ValueError("discovery plan cannot require artifacts")
        return self


class StartDataAnalysisArguments(AgentRuntimeContract):
    """唯一的分析 Opening action；协议名称由 Adapter 根据调用形状决定。"""

    plan: AnalysisPlanningDraft


class AnalysisPlanFinalizationRequest(AgentRuntimeContract):
    """语义检索或受限 Discovery 后唯一一次计划定稿的安全上下文。"""

    question: str = Field(min_length=1, max_length=10_000)
    physical_schema: SchemaSummaryRead
    datasource_revision: int = Field(ge=1)
    initial_plan: AnalysisPlanningDraft
    semantic_resolution: DataLinkSemanticContext | None = None
    semantic_warning: AnalysisWarning | None = None
    discovery_observations: list[DiscoveryObservation] = Field(default_factory=list, max_length=4)
    available_capabilities: list[AnalysisCapabilityKey] = Field(max_length=8)

    @model_validator(mode="after")
    def _require_one_followup_source(self) -> AnalysisPlanFinalizationRequest:
        if self.initial_plan.mode == "needs_semantic_context":
            if self.discovery_observations:
                raise ValueError("semantic plan finalization cannot include discovery observations")
            if (self.semantic_resolution is None) == (self.semantic_warning is None):
                raise ValueError(
                    "semantic plan finalization requires exactly one result or warning"
                )
            return self
        if self.initial_plan.mode == "discovery":
            if not self.discovery_observations:
                raise ValueError("discovery plan finalization requires an observation")
            if self.semantic_resolution is not None or self.semantic_warning is not None:
                raise ValueError("discovery plan finalization cannot include semantic results")
            return self
        raise ValueError("plan finalization requires a semantic-context or discovery plan")


class AnalysisPlanFinalizationArguments(AgentRuntimeContract):
    """定稿只能结束为 ready 或 clarification，不能形成循环。"""

    plan: AnalysisPlanningDraft

    @model_validator(mode="after")
    def _require_terminal_planning_mode(self) -> AnalysisPlanFinalizationArguments:
        if self.plan.mode not in {"ready", "clarification"}:
            raise ValueError("finalized plan must be ready or clarification")
        return self


class GeneralTaskOpening(AgentRuntimeContract):
    protocol_id: Literal["general-task"]
    answer: str = Field(min_length=1, max_length=10_000)


class DataAnalysisOpening(AgentRuntimeContract):
    protocol_id: Literal["data-analysis"]
    plan: AnalysisPlanningDraft


class RunOpeningDecision(AgentRuntimeContract):
    """Adapter 已将模型返回形状归一化为两个顶层协议之一。"""

    protocol_id: Literal["general-task", "data-analysis"]
    answer: str | None = Field(default=None, max_length=10_000)
    plan: AnalysisPlanningDraft | None = None

    @model_validator(mode="after")
    def _validate_protocol_payload(self) -> RunOpeningDecision:
        if self.protocol_id == "general-task":
            if not self.answer or self.plan is not None:
                raise ValueError("general-task requires answer and no plan")
        elif self.plan is None or self.answer is not None:
            raise ValueError("data-analysis requires plan and no answer")
        return self
