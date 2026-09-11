"""把结构化会话背景投影为各阶段可用的最小模型输入。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.datasources import SchemaSummaryRead

from agent_runtime.contracts import (
    AgentWorkingSetProjection,
    AnalysisPlanningDraft,
    ConversationContext,
    DatasourceIdentityProjection,
    DiscoveryObservation,
    DiscoveryObservationProjection,
    DiscoveryScopeDraft,
    DiscoveryScopeProjection,
    FinalAnswerProjection,
    OpeningContextProjection,
    OpeningRepairProjection,
    OpeningValidationIssue,
    PlanFinalizationProjection,
    PlanFollowupOutcome,
    SafeSchemaColumnProjection,
    SafeSchemaIndexProjection,
    SafeSchemaTableProjection,
)
from agent_runtime.physical_references import PhysicalReferenceError, parse_physical_reference


def safe_schema_index(
    schema: SchemaSummaryRead,
    *,
    table_names: set[str] | None = None,
    column_names: set[str] | None = None,
    columns_by_table: Mapping[str, set[str]] | None = None,
) -> SafeSchemaIndexProjection:
    """只保留结构性 Schema 信息，绝不把行数、样例或 DataSource ID 发给模型。"""

    allowed_tables = {value.casefold() for value in table_names} if table_names else None
    allowed_columns = {value.casefold() for value in column_names} if column_names else None
    allowed_columns_by_table = (
        {
            table.casefold(): {column.casefold() for column in columns}
            for table, columns in columns_by_table.items()
        }
        if columns_by_table is not None
        else None
    )
    selected_tables: list[SafeSchemaTableProjection] = []
    for table in schema.tables:
        if allowed_tables is not None and table.name.casefold() not in allowed_tables:
            continue
        table_columns = (
            allowed_columns_by_table.get(table.name.casefold(), set())
            if allowed_columns_by_table is not None
            else allowed_columns
        )
        columns = [
            SafeSchemaColumnProjection(
                name=column.name,
                type=column.type,
                nullable=column.nullable,
            )
            for column in table.columns
            if table_columns is None or column.name.casefold() in table_columns
        ]
        selected_column_names = {column.name.casefold() for column in columns}
        if allowed_columns_by_table is None and allowed_columns is None:
            primary_key = list(table.primary_key)
            foreign_keys = [
                {
                    "columns": list(foreign_key.columns),
                    "referenced_table": foreign_key.referenced_table,
                    "referenced_columns": list(foreign_key.referenced_columns),
                }
                for foreign_key in table.foreign_keys
            ]
        else:
            primary_key = [
                key for key in table.primary_key if key.casefold() in selected_column_names
            ]
            foreign_keys = [
                {
                    "columns": list(foreign_key.columns),
                    "referenced_table": foreign_key.referenced_table,
                    "referenced_columns": list(foreign_key.referenced_columns),
                }
                for foreign_key in table.foreign_keys
                if all(column.casefold() in selected_column_names for column in foreign_key.columns)
                and (
                    allowed_tables is None
                    or foreign_key.referenced_table.casefold() in allowed_tables
                )
                and (
                    allowed_columns_by_table is None
                    or all(
                        column.casefold()
                        in allowed_columns_by_table.get(
                            foreign_key.referenced_table.casefold(), set()
                        )
                        for column in foreign_key.referenced_columns
                    )
                )
            ]
        selected_tables.append(
            SafeSchemaTableProjection(
                name=table.name,
                columns=columns,
                primary_key=primary_key,
                foreign_keys=foreign_keys,
            )
        )
    return SafeSchemaIndexProjection(dialect=schema.dialect, tables=selected_tables)


def datasource_identity(
    *,
    name: str,
    source_type: str,
    schema: SchemaSummaryRead,
    schema_revision: int,
    description: str | None = None,
) -> DatasourceIdentityProjection:
    """构造不含内部 ID、source_ref、文件名和凭据的 DataSource 身份。"""

    cleaned = description.strip()[:2_000] if description and description.strip() else None
    return DatasourceIdentityProjection(
        name=name[:200],
        source_type=source_type[:40],
        dialect=schema.dialect[:40],
        schema_revision=schema_revision,
        description=cleaned,
    )


def opening_context_projection(
    *,
    context: ConversationContext,
    question: str,
    identity: DatasourceIdentityProjection,
    schema: SchemaSummaryRead,
) -> OpeningContextProjection:
    """Opening 的唯一上下文投影。"""

    return OpeningContextProjection(
        question=question,
        session_preferences=context.session_preferences,
        pending_clarification=context.pending_clarification,
        recent_user_turns=context.recent_user_turns[:6],
        historical_summaries=context.historical_summaries[:3],
        datasource_identity=identity,
        schema_index=safe_schema_index(schema),
    )


def opening_repair_projection(
    *,
    question: str,
    schema: SchemaSummaryRead,
    validation_issues: list[OpeningValidationIssue] | tuple[OpeningValidationIssue, ...],
    plan_skeleton: dict[str, object] | None = None,
) -> OpeningRepairProjection:
    """构造只包含形状诊断的单次 Opening repair 输入。"""

    issues = list(validation_issues)[:8]
    schema_projection = _repair_schema_projection(schema, issues)

    return OpeningRepairProjection(
        question=question,
        schema_index=schema_projection,
        qualified_columns=[
            f"{table.name}.{column.name}"
            for table in schema_projection.tables
            for column in table.columns
        ],
        validation_issues=issues,
        plan_skeleton=dict(plan_skeleton or {}),
    )


def _repair_schema_projection(
    schema: SchemaSummaryRead,
    issues: Sequence[OpeningValidationIssue],
) -> SafeSchemaIndexProjection:
    """投影 Repair 所需的最小 Schema；结构性错误仍需保留物理引用候选。

    字段/表引用错误可以缩小到 finding 涉及的候选，避免把无关字段带入模型。
    但 ``artifact_conflict``、``shape_invalid`` 等错误不包含物理引用；此时
    Repair 仍需重提交完整计划，返回空 Schema 会迫使模型猜表和字段。
    """

    known_tables = {table.name.casefold(): table.name for table in schema.tables}
    known_columns = {
        (table.name.casefold(), column.name.casefold()): column.name
        for table in schema.tables
        for column in table.columns
    }
    table_names: set[str] = set()
    columns_by_table: dict[str, set[str]] = {}
    qualified_pattern = re.compile(r"(?P<table>[A-Za-z_][\w]*)\.(?P<column>[A-Za-z_][\w]*)")
    for issue in issues:
        text = f"{issue.actual} {issue.expected} {issue.action}"
        for match in qualified_pattern.finditer(text):
            table_key = match.group("table").casefold()
            column_key = match.group("column").casefold()
            column_name = known_columns.get((table_key, column_key))
            table_name = known_tables.get(table_key)
            if table_name is None or column_name is None:
                continue
            table_names.add(table_name)
            columns_by_table.setdefault(table_name.casefold(), set()).add(column_name)
        actual_key = issue.actual.casefold()
        if actual_key and actual_key not in {"缺失", "未提供", "额外字段", "枚举值不合法"}:
            matches = [
                (table_name, column_name)
                for (table_key, column_key), column_name in known_columns.items()
                if column_key == actual_key
                for table_name in [known_tables[table_key]]
            ]
            if len(matches) == 1:
                table_name, column_name = matches[0]
                table_names.add(table_name)
                columns_by_table.setdefault(table_name.casefold(), set()).add(column_name)
    if not table_names:
        if any(issue.error_type == "unknown_table" for issue in issues):
            return safe_schema_index(
                schema,
                table_names=set(known_tables.values()),
                columns_by_table={table.casefold(): set() for table in known_tables.values()},
            )
        if any(issue.error_type in {"unknown_column", "invalid_reference"} for issue in issues):
            return safe_schema_index(schema)
        return safe_schema_index(schema)
    return safe_schema_index(schema, table_names=table_names, columns_by_table=columns_by_table)


def plan_finalization_projection(
    *,
    question: str,
    initial_plan: AnalysisPlanningDraft,
    schema: SchemaSummaryRead,
    followup_outcome: PlanFollowupOutcome,
    required_tables: set[str] | None = None,
    required_columns: set[str] | None = None,
    columns_by_table: Mapping[str, set[str]] | None = None,
    schema_projection: SafeSchemaIndexProjection | None = None,
) -> PlanFinalizationProjection:
    """计划定稿只继承初始计划和一次 typed follow-up，不重复带入会话历史。"""

    if required_tables is None and required_columns is None and columns_by_table is None:
        required_tables, columns_by_table = plan_schema_dependencies(schema, initial_plan)

    return PlanFinalizationProjection(
        question=question,
        initial_plan=initial_plan,
        required_schema=(
            schema_projection
            if schema_projection is not None
            else safe_schema_index(
                schema,
                table_names=required_tables,
                column_names=required_columns,
                columns_by_table=columns_by_table,
            )
        ),
        followup_outcome=followup_outcome,
    )


def plan_schema_dependencies(
    schema: SchemaSummaryRead,
    plan: AnalysisPlanningDraft,
) -> tuple[set[str] | None, dict[str, set[str]] | None]:
    """从初始计划提取可解析的物理表/字段依赖，忽略结果别名。"""

    required_tables: set[str] = set()
    columns_by_table: dict[str, set[str]] = {}

    def add_reference(
        value: str,
        *,
        require_column: bool,
        allowed_tables: Sequence[str] | None = None,
    ) -> None:
        try:
            reference = parse_physical_reference(
                schema,
                value,
                require_column=require_column,
                allowed_tables=allowed_tables,
            )
        except PhysicalReferenceError:
            return
        required_tables.add(reference.table)
        if reference.column:
            columns_by_table.setdefault(reference.table.casefold(), set()).add(reference.column)

    for requirement in plan.requirements:
        if requirement.fulfillment.mode != "evidence":
            continue
        for assertion in requirement.fulfillment.assertions:
            source_tables = tuple(assertion.source_tables)
            for table in assertion.source_tables:
                add_reference(table, require_column=False)
            for constraint in assertion.sql_constraints:
                if constraint.kind == "source":
                    add_reference(constraint.table, require_column=False)
                elif constraint.kind == "aggregate" and constraint.column not in {None, "*"}:
                    add_reference(
                        constraint.column,
                        require_column=True,
                        allowed_tables=source_tables,
                    )
                elif constraint.kind == "group_by":
                    for column in constraint.columns:
                        add_reference(
                            column,
                            require_column=True,
                            allowed_tables=source_tables,
                        )
                elif constraint.kind in {"column", "filter", "time_range"}:
                    add_reference(
                        constraint.column,
                        require_column=True,
                        allowed_tables=source_tables,
                    )
            for extraction in assertion.claim_extractions:
                if extraction.mode == "scalar":
                    add_reference(
                        extraction.field,
                        require_column=True,
                        allowed_tables=source_tables,
                    )
                    for selector in extraction.selector or {}:
                        add_reference(
                            selector,
                            require_column=True,
                            allowed_tables=source_tables,
                        )
                else:
                    add_reference(
                        extraction.value_field,
                        require_column=True,
                        allowed_tables=source_tables,
                    )
                    for column in extraction.dimension_fields:
                        add_reference(
                            column,
                            require_column=True,
                            allowed_tables=source_tables,
                        )
    if not required_tables:
        return None, None
    return required_tables, columns_by_table or None


def discovery_scope_projection(
    *,
    scope: DiscoveryScopeDraft,
    objective: str,
    acceptance_criteria: list[str] | tuple[str, ...],
) -> DiscoveryScopeProjection:
    """把 Discovery 目标和服务端 allowlist 编译成模型可见的安全投影。"""

    return DiscoveryScopeProjection(
        objective=objective,
        acceptance_criteria=list(acceptance_criteria)[:8],
        tables=list(scope.tables),
        columns=list(scope.columns),
        max_rows=scope.max_rows,
    )


def discovery_schema_index(
    schema: SchemaSummaryRead,
    scope: DiscoveryScopeDraft,
) -> SafeSchemaIndexProjection:
    """只把 Discovery 白名单对应的 Schema 子集交给探索模型。

    Discovery 的 allowlist 是服务端事实。完整 Schema 仍由服务端保留并在
    ``preflight_discovery_scope`` 中做最终校验，但探索模型不需要看到白名单外的
    字段，否则它很容易把“Schema 中存在”误认为“本次允许使用”。
    """

    allowed_tables = {table.casefold() for table in scope.tables}
    columns_by_table: dict[str, set[str]] = {}
    for value in scope.columns:
        try:
            reference = parse_physical_reference(
                schema,
                value,
                allowed_tables=scope.tables,
            )
        except PhysicalReferenceError:
            # 物化阶段已经拒绝非法 scope；这里安全地跳过异常值，避免在
            # 模型投影层重新发明一套错误处理或泄露原始输入。
            continue
        columns_by_table.setdefault(reference.table.casefold(), set()).add(
            reference.column.casefold()
        )

    return safe_schema_index(
        schema,
        table_names=allowed_tables,
        columns_by_table=columns_by_table,
    )


def discovery_observation_projection(
    observations: list[DiscoveryObservation] | tuple[DiscoveryObservation, ...],
) -> list[DiscoveryObservationProjection]:
    """去掉 Discovery 观察中的 Tool/Audit/Artifact 内部标识。"""

    return [
        DiscoveryObservationProjection(
            columns=list(item.columns),
            row_count=item.row_count,
            rows_truncated=item.rows_truncated,
        )
        for item in observations[:4]
    ]


def agent_working_set_projection(
    *,
    question: str,
    current_requirement_ids: list[str],
    verified_values: list[Any],
    incomplete_goals: list[str],
    pending_artifacts: dict[str, int],
    warnings: list[str],
) -> AgentWorkingSetProjection:
    """把结构化 Agent 状态投影成当前回合最小 Working Set。"""

    return AgentWorkingSetProjection(
        question=question,
        current_requirement_ids=list(dict.fromkeys(current_requirement_ids))[:16],
        verified_values=list(verified_values)[:500],
        incomplete_goals=incomplete_goals[:16],
        pending_artifacts=dict(pending_artifacts),
        warnings=warnings[:20],
    )


def final_answer_projection(
    *,
    question: str,
    accepted_goals: list[str],
    submitted_claims: list[Any],
    artifact_summaries: list[Any],
    incomplete_goals: list[str],
    warnings: list[str],
    schema: SchemaSummaryRead | None = None,
) -> FinalAnswerProjection:
    """最终答案只接收当前 Run 的已提交事实和产物摘要。"""

    return FinalAnswerProjection(
        question=question,
        accepted_goals=accepted_goals[:16],
        schema_index=safe_schema_index(schema) if schema is not None else None,
        submitted_claims=submitted_claims[:16],
        artifact_summaries=artifact_summaries[:50],
        incomplete_goals=incomplete_goals[:16],
        warnings=warnings[:20],
    )
