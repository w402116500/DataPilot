"""服务端校验并物化 Opening 提交的开放式分析计划。"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.datasources import SchemaSummaryRead

from agent_runtime.contracts import (
    AgentFailure,
    AnalysisAssertionDraft,
    AnalysisClarificationDraft,
    AnalysisExecutionConstraints,
    AnalysisFilterConstraint,
    AnalysisPlanInvalidFailure,
    AnalysisPlanningDraft,
    AnalysisRequirement,
    AnalysisWarning,
    DataLinkSemanticContext,
    DiscoveryScopeDraft,
    OpeningValidationIssue,
    create_analysis_assertions,
)
from agent_runtime.physical_references import PhysicalReferenceError, parse_physical_reference
from agent_runtime.runtime_limits import AgentRuntimeLimits


@dataclass(frozen=True)
class MaterializedAnalysisPlan:
    """唯一可进入数据 Graph 的已验证计划，不保存模型自定义运行身份。"""

    mode: str
    requirements: tuple[AnalysisRequirement, ...]
    constraints: AnalysisExecutionConstraints
    clarification: AnalysisClarificationDraft | None = None
    semantic_context: DataLinkSemanticContext | None = None
    warnings: tuple[AnalysisWarning, ...] = ()
    consumes_pending: bool = False
    discovery_scope: DiscoveryScopeDraft | None = None


def materialize_analysis_plan(
    plan: AnalysisPlanningDraft,
    schema: SchemaSummaryRead,
    *,
    semantic_context: DataLinkSemanticContext | None = None,
    warnings: tuple[AnalysisWarning, ...] = (),
    limits: AgentRuntimeLimits | None = None,
) -> MaterializedAnalysisPlan | AgentFailure:
    """将模型草案约束为当前冻结 Schema 可执行的服务端对象。"""
    runtime_limits = limits or AgentRuntimeLimits()
    constraints = plan.execution_constraints
    findings: list[OpeningValidationIssue] = []
    required_kinds = {item.kind for item in constraints.required_artifacts}
    forbidden_kinds = set(constraints.forbidden_artifact_kinds)
    for kind in sorted(required_kinds & forbidden_kinds):
        index = next(
            index for index, item in enumerate(constraints.required_artifacts) if item.kind == kind
        )
        findings.append(
            _issue(
                path=f"execution_constraints.required_artifacts[{index}].kind",
                error_type="artifact_conflict",
                actual=kind,
                expected="required_artifacts 与 forbidden_artifact_kinds 不得包含相同类型",
                rule="同一种 Artifact 不能同时是必需交付物和禁止产物",
                action=(
                    "删除冲突的 required_artifacts 或 forbidden_artifact_kinds 条目后"
                    "重新提交完整 plan。"
                ),
                repair_reason="artifact_conflict",
            )
        )

    if plan.mode == "clarification":
        if plan.consumes_pending:
            findings.append(
                _issue(
                    path="consumes_pending",
                    error_type="protocol_direction_conflict",
                    actual="true",
                    expected="false",
                    rule="clarification 只建立或更新 pending，不能在用户补充前清除 pending",
                    action="将 consumes_pending 改为 false 后重新提交完整 clarification plan。",
                    repair_reason="protocol_direction_conflict",
                    suggested_mode="clarification",
                )
            )
        if findings:
            return _invalid_findings("澄清计划没有通过结构合同校验。", findings)
        return MaterializedAnalysisPlan(
            mode=plan.mode,
            consumes_pending=plan.consumes_pending,
            requirements=(),
            constraints=constraints,
            clarification=plan.clarification,
            semantic_context=semantic_context,
            warnings=warnings,
            discovery_scope=None,
        )

    has_evidence_fulfillment = any(
        item.fulfillment.mode == "evidence" for item in plan.requirements
    )
    if constraints.required_artifacts and plan.requirements and not has_evidence_fulfillment:
        findings.append(
            _issue(
                path="execution_constraints.required_artifacts[0]",
                error_type="artifact_conflict",
                actual="没有 evidence fulfillment",
                expected="至少一个 requirement 使用 evidence fulfillment",
                rule="context_only 不创建 Artifact Writer，也不能用没有数据证据的路径生成正式文件",
                action="删除 required_artifacts，或将需要文件的目标完整改为 evidence plan。",
                repair_reason="artifact_conflict",
                suggested_mode="ready",
            )
        )
    if has_evidence_fulfillment and "run_sql_readonly" in constraints.forbidden_tools:
        findings.append(
            _issue(
                path="execution_constraints.forbidden_tools",
                error_type="capability_conflict",
                actual="run_sql_readonly",
                expected="evidence fulfillment 需要可用的 run_sql_readonly",
                rule="需要当前数据证据的目标不能同时禁止 SQL 查询",
                action="移除 run_sql_readonly 禁止项，或将该目标完整改为 context_only/blocked。",
                repair_reason="shape_invalid",
                suggested_mode="ready",
            )
        )
    if constraints.required_artifacts and "run_python" in constraints.forbidden_tools:
        findings.append(
            _issue(
                path="execution_constraints.forbidden_tools",
                error_type="capability_conflict",
                actual="run_python",
                expected="required_artifacts 需要可用的 run_python",
                rule="正式 Artifact 不能在禁止 Python 时完成",
                action="移除 run_python 禁止项，或删除 required_artifacts。",
                repair_reason="shape_invalid",
                suggested_mode="ready",
            )
        )

    discovery_scope = plan.discovery_scope
    if plan.mode == "discovery" and discovery_scope is not None:
        for index, table in enumerate(discovery_scope.tables):
            try:
                parse_physical_reference(schema, table, require_column=False)
            except PhysicalReferenceError as error:
                findings.append(
                    _physical_issue(
                        error,
                        path=f"discovery_scope.tables[{index}]",
                        action="只填写当前冻结 Schema 中允许探索的物理表名。",
                        repair_reason="discovery_scope_missing",
                    )
                )
        for index, column in enumerate(discovery_scope.columns):
            try:
                parse_physical_reference(schema, column)
            except PhysicalReferenceError as error:
                findings.append(
                    _physical_issue(
                        error,
                        path=f"discovery_scope.columns[{index}]",
                        action="只填写当前冻结 Schema 中允许探索的物理字段名。",
                        repair_reason="discovery_scope_missing",
                    )
                )

    table_names = {table.name.casefold() for table in schema.tables}
    column_names = {column.name.casefold() for table in schema.tables for column in table.columns}
    requirements: list[AnalysisRequirement] = []
    descriptions: set[str] = set()
    total_assertions = 0
    for index, draft in enumerate(plan.requirements):
        plan_path = f"requirements[{index}]"
        description = draft.description.strip()
        criteria = [item.strip() for item in draft.acceptance_criteria if item.strip()]
        if not description:
            findings.append(
                _issue(
                    path=f"{plan_path}.description",
                    error_type="missing",
                    actual="空",
                    expected="非空目标描述",
                    rule="每个 requirement 必须有可执行的 description",
                    action="补充该目标的具体描述后重新提交完整 plan。",
                    repair_reason="scope_incomplete",
                    suggested_mode="clarification",
                )
            )
            continue
        if not criteria:
            findings.append(
                _issue(
                    path=f"{plan_path}.acceptance_criteria",
                    error_type="missing",
                    actual="0 项",
                    expected="至少 1 条验收条件",
                    rule="每个 requirement 必须说明怎样算完成",
                    action="补充 acceptance_criteria 后重新提交完整 plan。",
                    repair_reason="scope_incomplete",
                )
            )
            continue
        if description.casefold() in descriptions:
            findings.append(
                _issue(
                    path=f"{plan_path}.description",
                    error_type="duplicate",
                    actual="重复目标",
                    expected="与其它 requirement 不重复",
                    rule="独立目标必须拆分且每个目标描述唯一",
                    action="删除重复目标，或把它改成独立且可验证的目标后重新提交。",
                    repair_reason="shape_invalid",
                )
            )
            continue
        descriptions.add(description.casefold())
        requirement_id = f"R{index + 1}"
        fulfillment = draft.fulfillment
        if fulfillment.mode == "evidence":
            # Older model prompts represented NULL checks as ``eq`` with a
            # null value.  Accept that wire shape during the compatibility
            # window, but persist only the explicit SQL semantics understood
            # by the Guard and query-contract checker.
            normalized_assertions = [
                _normalize_null_filter_constraints(assertion)
                for assertion in fulfillment.assertions
            ]
            if len(normalized_assertions) > runtime_limits.max_assertions_per_requirement:
                findings.append(
                    _issue(
                        path=f"{plan_path}.fulfillment.assertions",
                        error_type="complexity_limit",
                        actual=f"{len(normalized_assertions)} 项",
                        expected=f"不超过 {runtime_limits.max_assertions_per_requirement} 项",
                        rule="单个用户目标只能包含有限数量的核心结构化检查项",
                        action=(
                            "只保留完成用户目标所必需的核心检查项；低优先级质量检查"
                            "应删除或改为非硬性说明后重新提交。"
                        ),
                        repair_reason="shape_invalid",
                    )
                )
                continue
            if total_assertions + len(normalized_assertions) > runtime_limits.max_total_assertions:
                findings.append(
                    _issue(
                        path=f"{plan_path}.fulfillment.assertions",
                        error_type="complexity_limit",
                        actual=f"累计至少 {total_assertions + len(normalized_assertions)} 项",
                        expected=f"单个 Run 不超过 {runtime_limits.max_total_assertions} 项",
                        rule="单个分析 Run 的结构化检查项总数必须有界",
                        action=(
                            "合并重复检查或拆掉低优先级检查，只保留能直接支撑用户问题"
                            "的核心证据后重新提交完整 plan。"
                        ),
                        repair_reason="shape_invalid",
                    )
                )
                continue
            assertion_findings = _assertions_match_schema(
                normalized_assertions,
                table_names,
                column_names,
                schema=schema,
                base_path=f"{plan_path}.fulfillment.assertions",
            )
            if assertion_findings:
                findings.extend(assertion_findings)
                continue
            total_assertions += len(normalized_assertions)
            requirements.append(
                AnalysisRequirement(
                    id=requirement_id,
                    description=description,
                    acceptance_criteria=criteria,
                    assertions=create_analysis_assertions(requirement_id, normalized_assertions),
                    required=True,
                    fulfillment_mode="evidence",
                )
            )
        elif fulfillment.mode == "context_only":
            requirements.append(
                AnalysisRequirement(
                    id=requirement_id,
                    description=description,
                    acceptance_criteria=criteria,
                    required=True,
                    fulfillment_mode="context_only",
                    context_sources=list(fulfillment.sources),
                    status="context_ready",
                )
            )
        else:
            requirements.append(
                AnalysisRequirement(
                    id=requirement_id,
                    description=description,
                    acceptance_criteria=criteria,
                    required=True,
                    fulfillment_mode="blocked",
                    block_reason=fulfillment.reason,
                    status="blocked",
                )
            )

    if findings:
        findings.sort(
            key=lambda issue: (issue.path, issue.error_type, issue.repair_reason, issue.action)
        )
        return _invalid_findings("分析计划没有通过字段和来源合同校验。", findings)
    return MaterializedAnalysisPlan(
        mode=plan.mode,
        consumes_pending=plan.consumes_pending,
        requirements=tuple(requirements),
        constraints=constraints,
        semantic_context=semantic_context,
        warnings=warnings,
        discovery_scope=discovery_scope,
    )


def _normalize_null_filter_constraints(assertion: AnalysisAssertionDraft) -> AnalysisAssertionDraft:
    """Normalize the legacy ``eq`` + ``null`` filter representation."""

    constraints = [
        constraint.model_copy(update={"operator": "is_null"})
        if isinstance(constraint, AnalysisFilterConstraint)
        and constraint.operator == "eq"
        and constraint.value is None
        else constraint
        for constraint in assertion.sql_constraints
    ]
    if constraints == assertion.sql_constraints:
        return assertion
    return assertion.model_copy(update={"sql_constraints": constraints})


def _assertions_match_schema(
    assertions: list[AnalysisAssertionDraft],
    table_names: set[str],
    column_names: set[str],
    *,
    schema: SchemaSummaryRead | None = None,
    base_path: str = "requirements[0].fulfillment.assertions",
) -> tuple[OpeningValidationIssue, ...]:
    if not assertions:
        return (
            _issue(
                path=base_path,
                error_type="missing",
                actual="0 项",
                expected="至少 1 个 assertion",
                rule="evidence fulfillment 必须至少包含一个可验证 assertion",
                action="在 fulfillment.assertions 中提交至少一个完整 assertion。",
                repair_reason="scope_incomplete",
            ),
        )
    findings: list[OpeningValidationIssue] = []
    for index, assertion in enumerate(assertions):
        findings.extend(
            _assertion_matches_schema(
                assertion,
                table_names,
                column_names,
                schema=schema,
                base_path=f"{base_path}[{index}]",
            )
        )
    return tuple(findings[:12])


def _assertion_matches_schema(
    assertion: AnalysisAssertionDraft,
    table_names: set[str],
    column_names: set[str],
    *,
    schema: SchemaSummaryRead | None = None,
    base_path: str = "assertion",
) -> tuple[OpeningValidationIssue, ...]:
    del column_names
    findings: list[OpeningValidationIssue] = []
    normalized_sources = [_identifier_key(value) for value in assertion.source_tables]
    if len(normalized_sources) != len(set(normalized_sources)):
        findings.append(
            _issue(
                path=f"{base_path}.source_tables",
                error_type="duplicate",
                actual="重复表名",
                expected="每个物理来源表只出现一次",
                rule="source_tables 是当前 assertion 的最小物理来源集合",
                action="删除重复 source_tables 条目后重新提交。",
                repair_reason="shape_invalid",
            )
        )
    for index, table in enumerate(assertion.source_tables):
        if _identifier_key(table) not in table_names:
            findings.append(
                _issue(
                    path=f"{base_path}.source_tables[{index}]",
                    error_type="unknown_table",
                    actual=table,
                    expected="当前冻结 Schema 中存在的表名",
                    rule="assertion.source_tables 只能引用当前 Schema 的物理表",
                    action="改用当前 Schema 中存在的表名。",
                    repair_reason="schema_reference_invalid",
                )
            )
    result_columns = assertion.result_columns
    if any(not isinstance(column, str) or not column.strip() for column in result_columns):
        findings.append(
            _issue(
                path=f"{base_path}.result_columns",
                error_type="empty",
                actual="存在空结果列名",
                expected="非空且唯一的结果列别名",
                rule="result_columns 只能声明 SQL 输出的非空派生列或别名",
                action="删除空结果列名，并为派生输出填写稳定别名。",
                repair_reason="shape_invalid",
            )
        )
    if len({_identifier_key(column) for column in result_columns}) != len(result_columns):
        findings.append(
            _issue(
                path=f"{base_path}.result_columns",
                error_type="duplicate",
                actual="重复结果列别名",
                expected="每个 result_columns 别名唯一",
                rule="同一 assertion 内结果别名不能重复",
                action="保留一个结果别名或改成不同的输出列名。",
                repair_reason="shape_invalid",
            )
        )

    if schema is not None:
        findings.extend(_physical_references_valid(assertion, schema, base_path=base_path))

    for index, extraction in enumerate(assertion.claim_extractions):
        if extraction.name.casefold() in {"chart", "chart_generated"}:
            findings.append(
                _issue(
                    path=f"{base_path}.claim_extractions[{index}].name",
                    error_type="artifact_conflict",
                    actual=extraction.name,
                    expected="非产物名称的事实提取名",
                    rule="图表是 Artifact，不是 Claim extraction",
                    action=(
                        "删除该 Claim extraction，使用 execution_constraints.required_artifacts"
                        " 声明图表。"
                    ),
                    repair_reason="artifact_conflict",
                )
            )
    constraint_kinds = {constraint.kind for constraint in assertion.sql_constraints}
    has_grouping = "aggregate" in constraint_kinds or "group_by" in constraint_kinds
    for index, extraction in enumerate(assertion.claim_extractions):
        if extraction.mode != "series" or has_grouping:
            continue
        findings.append(
            _issue(
                path=f"{base_path}.claim_extractions[{index}]",
                error_type="shape_invalid",
                actual="series 缺少 aggregate 或 group_by",
                expected="series 必须带 aggregate 或 group_by 约束",
                rule="未聚合明细不能作为关系或分组证据；结构问题应改为 context_only",
                action="为该 series 补充 aggregate 或 group_by，或将该目标改为 context_only。",
                repair_reason="shape_invalid",
            )
        )
    return tuple(findings[:12])


def _physical_references_valid(
    assertion: AnalysisAssertionDraft,
    schema: SchemaSummaryRead,
    *,
    base_path: str,
) -> tuple[OpeningValidationIssue, ...]:
    """所有物理表/字段入口统一经过共享 parser，并返回字段级 finding。"""

    findings: list[OpeningValidationIssue] = []
    source_tables = tuple(assertion.source_tables)
    allowed_tables = source_tables or None
    for index, constraint in enumerate(assertion.sql_constraints):
        if constraint.kind == "source":
            if not source_tables:
                findings.append(
                    _issue(
                        path=f"{base_path}.sql_constraints[{index}].table",
                        error_type="source_scope_conflict",
                        actual=constraint.table,
                        expected="assertion.source_tables 必须包含该 source constraint 的表",
                        rule=(
                            "source constraint 必须属于同一 assertion.source_tables；"
                            "未声明来源表时不能指定物理来源"
                        ),
                        action=(
                            "在 assertion.source_tables 中补充该表，或删除该 source constraint，"
                            "然后重新提交完整 plan。"
                        ),
                        repair_reason="source_scope_conflict",
                    )
                )
                continue
            try:
                parse_physical_reference(
                    schema,
                    constraint.table,
                    require_column=False,
                    allowed_tables=allowed_tables,
                )
            except PhysicalReferenceError as error:
                findings.append(
                    _physical_issue(
                        error,
                        path=f"{base_path}.sql_constraints[{index}].table",
                        action=(
                            "source constraint 只能引用当前 Schema 中存在且属于"
                            " assertion.source_tables 的表。"
                        ),
                        repair_reason=(
                            "source_scope_conflict"
                            if error.reason_code in {"table_not_allowed", "column_not_allowed"}
                            else "schema_reference_invalid"
                        ),
                    )
                )
        elif constraint.kind == "group_by":
            result_names = _result_names(assertion)
            for column_index, column in enumerate(constraint.columns):
                if _identifier_key(column) in result_names and not _is_qualified_reference(column):
                    continue
                try:
                    parse_physical_reference(schema, column, allowed_tables=allowed_tables)
                except PhysicalReferenceError as error:
                    findings.append(
                        _physical_issue(
                            error,
                            path=f"{base_path}.sql_constraints[{index}].columns[{column_index}]",
                            action=(
                                "改用当前 assertion.source_tables 内的物理字段或"
                                "本 assertion 已声明的结果别名。"
                            ),
                            repair_reason="schema_reference_invalid",
                        )
                    )
        else:
            for column_index, column in enumerate(_constraint_columns(constraint)):
                if column == "*":
                    continue
                field_path = (
                    f"{base_path}.sql_constraints[{index}].columns[{column_index}]"
                    if constraint.kind == "group_by"
                    else f"{base_path}.sql_constraints[{index}].column"
                )
                try:
                    parse_physical_reference(schema, column, allowed_tables=allowed_tables)
                except PhysicalReferenceError as error:
                    findings.append(
                        _physical_issue(
                            error,
                            path=field_path,
                            action="改用当前 assertion.source_tables 内的物理字段。",
                            repair_reason="schema_reference_invalid",
                        )
                    )
    result_names = _result_names(assertion)
    for index, dimension in enumerate(assertion.dimensions):
        if _identifier_key(dimension) in result_names and not _is_qualified_reference(dimension):
            continue
        try:
            parse_physical_reference(schema, dimension, allowed_tables=allowed_tables)
        except PhysicalReferenceError as error:
            findings.append(
                _physical_issue(
                    error,
                    path=f"{base_path}.dimensions[{index}]",
                    action="改用当前 assertion.source_tables 内的物理字段或已声明的结果别名。",
                    repair_reason="schema_reference_invalid",
                )
            )
    for index, extraction in enumerate(assertion.claim_extractions):
        fields = _claim_extraction_fields(extraction, base_path, index)
        for path, field in fields:
            if _identifier_key(field) in result_names and not _is_qualified_reference(field):
                continue
            try:
                parse_physical_reference(schema, field, allowed_tables=allowed_tables)
            except PhysicalReferenceError as error:
                findings.append(
                    _physical_issue(
                        error,
                        path=path,
                        action="改用当前 assertion.source_tables 内的物理字段或已声明的结果别名。",
                        repair_reason="schema_reference_invalid",
                    )
                )
    for path, field in _result_check_fields(assertion, base_path):
        if _identifier_key(field) in result_names and not _is_qualified_reference(field):
            continue
        try:
            parse_physical_reference(schema, field, allowed_tables=allowed_tables)
        except PhysicalReferenceError as error:
            findings.append(
                _physical_issue(
                    error,
                    path=path,
                    action="改用当前 assertion.source_tables 内的物理字段或已声明的结果别名。",
                    repair_reason="schema_reference_invalid",
                )
            )
    return tuple(findings[:12])


def _result_names(assertion: AnalysisAssertionDraft) -> set[str]:
    """返回该 assertion 自己拥有的结果列/聚合别名，不混入全局 Schema 字段。"""

    return {
        _identifier_key(value) for value in assertion.result_columns if isinstance(value, str)
    } | {
        _identifier_key(item.alias)
        for item in assertion.sql_constraints
        if item.kind == "aggregate" and item.alias
    }


def _result_or_physical_issue(
    value: str,
    *,
    schema: SchemaSummaryRead,
    source_tables: list[str],
    result_names: set[str],
    path: str,
) -> OpeningValidationIssue | None:
    """结果别名只能属于当前 assertion；否则必须是其来源表内的物理字段。"""

    if not isinstance(value, str) or not value.strip():
        return _issue(
            path=path,
            error_type="missing",
            actual="空",
            expected="物理字段或当前 assertion 的结果别名",
            rule="dimensions、Claim extraction 和 result check 必须引用可验证字段",
            action="填写当前 assertion 的物理字段或先声明对应 result_columns。",
            repair_reason="scope_incomplete",
        )
    if _identifier_key(value) in result_names and not _is_qualified_reference(value):
        return None
    try:
        parse_physical_reference(schema, value, allowed_tables=source_tables or None)
    except PhysicalReferenceError as error:
        return _physical_issue(
            error,
            path=path,
            action="改用当前 assertion.source_tables 内的物理字段，或先声明合法结果别名。",
            repair_reason="schema_reference_invalid",
        )
    return None


def _claim_extraction_fields(
    extraction: object, base_path: str, index: int
) -> tuple[tuple[str, str], ...]:
    """返回 Claim extraction 中每个字段及其最小合同路径。"""

    prefix = f"{base_path}.claim_extractions[{index}]"
    if getattr(extraction, "mode", None) == "scalar":
        values: list[tuple[str, str]] = [(f"{prefix}.field", extraction.field)]
        values.extend((f"{prefix}.selector.{name}", name) for name in (extraction.selector or {}))
        return tuple(values)
    values = [(f"{prefix}.value_field", extraction.value_field)]
    values.extend(
        (f"{prefix}.dimension_fields[{field_index}]", field)
        for field_index, field in enumerate(extraction.dimension_fields)
    )
    return tuple(values)


def _result_check_fields(
    assertion: AnalysisAssertionDraft, base_path: str
) -> tuple[tuple[str, str], ...]:
    """展开结果检查字段，同时保留原始字段所在的最小路径。"""

    result: list[tuple[str, str]] = []
    for index, check in enumerate(assertion.result_checks):
        prefix = f"{base_path}.result_checks[{index}]"
        if check.kind == "column_sum_equals":
            result.extend(
                (
                    (f"{prefix}.value_field", check.value_field),
                    (f"{prefix}.total_field", check.total_field),
                )
            )
            continue
        for field_index, field in enumerate(getattr(check, "fields", ())):
            result.append((f"{prefix}.fields[{field_index}]", field))
        for name in ("left", "right", "total", "value", "numerator", "denominator"):
            operand = getattr(check, name, None)
            if operand is not None:
                if operand.field:
                    result.append((f"{prefix}.{name}.field", operand.field))
                result.extend(
                    (f"{prefix}.{name}.selector.{selector_name}", selector_name)
                    for selector_name in (operand.selector or {})
                )
        for part_index, operand in enumerate(getattr(check, "parts", ())):
            if operand.field:
                result.append((f"{prefix}.parts[{part_index}].field", operand.field))
            result.extend(
                (
                    f"{prefix}.parts[{part_index}].selector.{selector_name}",
                    selector_name,
                )
                for selector_name in (operand.selector or {})
            )
    return tuple(result)


def _constraint_columns(constraint: object) -> tuple[str, ...]:
    kind = getattr(constraint, "kind", None)
    if kind in {"column", "filter", "time_range"}:
        return (constraint.column,)
    if kind == "aggregate" and constraint.column:
        return (constraint.column,)
    if kind == "group_by":
        return tuple(constraint.columns)
    return ()


def _physical_issue(
    error: PhysicalReferenceError,
    *,
    path: str,
    action: str,
    repair_reason: str,
) -> OpeningValidationIssue:
    reason = repair_reason
    error_type = error.reason_code
    if error.reason_code in {"unknown_table", "unknown_column", "invalid_reference"}:
        reason = "schema_reference_invalid"
    elif error.reason_code in {"table_not_allowed", "column_not_allowed"}:
        reason = "source_scope_conflict"
        error_type = "source_scope_conflict"
    actual = error.safe_value or error.column or error.table or "未提供"
    candidate_text = ", ".join(error.candidates[:4])
    expected = "当前冻结 Schema 中的物理引用"
    if candidate_text:
        expected = f"当前冻结 Schema 中的合法引用；候选：{candidate_text}"
    if error.reason_code == "ambiguous_column":
        action = (
            f"{action} 该字段在多个表中存在，请从候选中选择一个完整的 table.column，"
            "不要继续填写裸字段。"
        )
    return _issue(
        path=path,
        error_type=error_type,
        actual=actual,
        expected=expected,
        rule="物理表/字段必须存在且属于当前 assertion 或 Discovery allowlist 的来源范围",
        action=action,
        repair_reason=reason,
    )


def _issue(
    *,
    path: str,
    error_type: str,
    actual: str,
    expected: str,
    rule: str,
    action: str,
    repair_reason: str,
    suggested_mode: str | None = None,
) -> OpeningValidationIssue:
    return OpeningValidationIssue(
        path=path,
        error_type=error_type,
        repair_reason=repair_reason,
        actual=_safe_issue_text(actual, fallback="未提供", limit=240),
        expected=_safe_issue_text(expected, fallback="符合当前计划合同", limit=400),
        rule=_safe_issue_text(rule, fallback="必须符合当前计划合同", limit=400),
        action=_safe_issue_text(action, fallback="修正该字段后重新提交完整 plan。", limit=400),
        suggested_mode=suggested_mode,
    )


def _safe_issue_text(value: object, *, fallback: str, limit: int) -> str:
    """限制 finding 文本为短、脱敏的诊断标量。"""

    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split()).strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        return fallback
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "password",
            "passwd",
            "api_key",
            "apikey",
            "secret",
            "token",
            "authorization",
            "credential",
            "bearer ",
            "select ",
            "insert ",
            "update ",
            "delete ",
            "drop ",
            " from ",
            " where ",
        )
    ):
        return fallback
    if text.startswith(("/", "\\")) or "://" in text or "\\\\" in text or ";" in text:
        return fallback
    return text


def _invalid(
    message: str,
    *,
    path: str,
    actual: str,
    expected: str,
    rule: str,
    action: str,
    repair_reason: str,
    suggested_mode: str | None = None,
) -> AnalysisPlanInvalidFailure:
    return AnalysisPlanInvalidFailure(
        message=message,
        validation_issues=[
            _issue(
                path=path,
                error_type=repair_reason,
                actual=actual,
                expected=expected,
                rule=rule,
                action=action,
                repair_reason=repair_reason,
                suggested_mode=suggested_mode,
            )
        ],
    )


def _invalid_findings(
    message: str,
    findings: list[OpeningValidationIssue] | tuple[OpeningValidationIssue, ...],
) -> AnalysisPlanInvalidFailure:
    return AnalysisPlanInvalidFailure(message=message, validation_issues=list(findings)[:12])


def _is_qualified_reference(value: str) -> bool:
    return isinstance(value, str) and value.strip().count(".") == 1


def _identifier_key(value: str) -> str:
    return value.strip('"`[] ').casefold()
