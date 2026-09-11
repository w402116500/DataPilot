"""Run Opening 的一次决策和唯一形状修复预算。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from contracts.datasources import SchemaSummaryRead

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    AnalysisPlanInvalidFailure,
    AnalysisPlanningDraft,
    OpeningContextProjection,
    OpeningValidationIssue,
    RunOpeningDecision,
    RunOpeningInvalidFailure,
)
from agent_runtime.ports import CancellationSignal, ModelClientPort


@dataclass(frozen=True)
class RunOpeningResult:
    """Opening 结果及调用计数，供日志和 LangSmith metadata 使用。"""

    decision: RunOpeningDecision | AgentFailure
    opening_model_calls: int
    opening_repair_calls: int
    validation_issues: tuple[OpeningValidationIssue, ...] = ()


async def open_run(
    model: ModelClientPort,
    *,
    question: str,
    schema: SchemaSummaryRead,
    cancellation: CancellationSignal,
    opening_context: OpeningContextProjection,
    plan_validator: Callable[[AnalysisPlanningDraft], AgentFailure | None] | None = None,
) -> RunOpeningResult:
    """首轮只允许普通文本或一个分析 action，形状错误最多修复一次。"""

    first = await _invoke_model_opening(
        model,
        question,
        schema,
        cancellation,
        opening_context=opening_context,
        repair_issues=(),
    )
    first_decision: RunOpeningDecision | None = None
    if not isinstance(first, AgentFailure):
        first_decision = first
        first_protocol = first.protocol_id
        validation_failure = _validate_plan(first, plan_validator)
        if validation_failure is None:
            return RunOpeningResult(first, opening_model_calls=1, opening_repair_calls=0)
        first = validation_failure
    else:
        first_protocol = (
            "data-analysis"
            if isinstance(first, RunOpeningInvalidFailure)
            and first.attempt_summary is not None
            and first.attempt_summary.attempted_protocol == "data-analysis"
            else None
        )
    if first.code is AgentErrorCode.RUN_CANCELED:
        return RunOpeningResult(first, opening_model_calls=1, opening_repair_calls=0)
    if first.code not in {
        AgentErrorCode.MODEL_RUN_OPENING_INVALID,
        AgentErrorCode.ANALYSIS_PLAN_INVALID,
    }:
        return RunOpeningResult(first, opening_model_calls=1, opening_repair_calls=0)

    repaired = await _invoke_model_opening(
        model,
        question,
        schema,
        cancellation,
        opening_context=opening_context,
        repair=True,
        repair_issues=(
            tuple(first.validation_issues)
            if isinstance(first, RunOpeningInvalidFailure) or hasattr(first, "validation_issues")
            else ()
        ),
        repair_plan_skeleton=_plan_skeleton(first_decision or first),
    )
    if not isinstance(repaired, AgentFailure):
        if first_protocol == "data-analysis" and repaired.protocol_id != "data-analysis":
            direction_issue = OpeningValidationIssue(
                path="protocol_id",
                error_type="protocol_direction_conflict",
                repair_reason="protocol_direction_conflict",
                actual=repaired.protocol_id,
                expected="data-analysis",
                rule="首轮已经选择数据分析协议时，Repair 只能重新提交 data-analysis action",
                action=(
                    "保留 data-analysis 协议，并完整重新提交 start_data_analysis plan；"
                    "不要改成普通文本。"
                ),
                suggested_mode="ready",
            )
            failure = AnalysisPlanInvalidFailure(
                message="Opening Repair 改变了已选择的数据分析协议",
                validation_issues=[direction_issue],
            )
            return RunOpeningResult(
                failure,
                opening_model_calls=2,
                opening_repair_calls=1,
                validation_issues=(direction_issue,),
            )
        if first_protocol == "data-analysis" and repaired.protocol_id == "data-analysis":
            mode_issue = _mode_transition_issue(first, repaired, first_decision)
            if mode_issue is not None:
                failure = AnalysisPlanInvalidFailure(
                    message="Opening Repair 改变了不允许的分析模式",
                    validation_issues=[mode_issue],
                )
                return RunOpeningResult(
                    failure,
                    opening_model_calls=2,
                    opening_repair_calls=1,
                    validation_issues=(mode_issue,),
                )
        validation_failure = _validate_plan(repaired, plan_validator)
        if validation_failure is None:
            return RunOpeningResult(
                repaired,
                opening_model_calls=2,
                opening_repair_calls=1,
                validation_issues=_failure_issues(first),
            )
        return RunOpeningResult(
            validation_failure,
            opening_model_calls=2,
            opening_repair_calls=1,
            validation_issues=_failure_issues(validation_failure),
        )
    if repaired.code is AgentErrorCode.RUN_CANCELED:
        return RunOpeningResult(repaired, opening_model_calls=2, opening_repair_calls=1)
    if repaired.code is not AgentErrorCode.MODEL_RUN_OPENING_INVALID:
        return RunOpeningResult(repaired, opening_model_calls=2, opening_repair_calls=1)
    return RunOpeningResult(
        AgentFailure(
            code=AgentErrorCode.MODEL_OUTPUT_REPAIR_EXHAUSTED,
            message="模型 Opening 输出连续两次不符合协议",
        ),
        opening_model_calls=2,
        opening_repair_calls=1,
        validation_issues=_failure_issues(first) or _failure_issues(repaired),
    )


def _validate_plan(
    decision: RunOpeningDecision,
    validator: Callable[[AnalysisPlanningDraft], AgentFailure | None] | None,
) -> AgentFailure | None:
    if validator is None or decision.protocol_id != "data-analysis":
        return None
    if decision.plan is None:
        return AgentFailure(
            code=AgentErrorCode.ANALYSIS_PLAN_INVALID,
            message="数据分析 Opening 缺少计划",
        )
    return validator(decision.plan)


def _failure_issues(value: object) -> tuple[OpeningValidationIssue, ...]:
    issues = getattr(value, "validation_issues", ())
    if not isinstance(issues, (list, tuple)):
        return ()
    return tuple(issue for issue in issues if isinstance(issue, OpeningValidationIssue))[:12]


async def _invoke_model_opening(
    model: ModelClientPort,
    question: str,
    schema: SchemaSummaryRead,
    cancellation: CancellationSignal,
    **kwargs: object,
) -> RunOpeningDecision | AgentFailure:
    """调用唯一的 Opening 投影合同。"""

    return await model.open_run(question, schema, cancellation, **kwargs)


def _plan_skeleton(value: object) -> dict[str, object]:
    """投影首轮计划的安全形状摘要，不复制描述、字段或模型原文。"""

    if isinstance(value, RunOpeningInvalidFailure) and value.attempt_summary is not None:
        summary = value.attempt_summary
        return {
            "protocol_id": summary.attempted_protocol,
            "mode": summary.trusted_mode or "unknown",
            "requirement_count": summary.requirement_count,
            "fulfillment_count": summary.fulfillment_count,
            "assertion_count": summary.assertion_count,
            "artifact_count": summary.artifact_count,
            "allowed_action": summary.allowed_action,
        }
    if not isinstance(value, RunOpeningDecision) or value.protocol_id != "data-analysis":
        return {"allowed_action": "start_data_analysis", "text": True}
    plan = value.plan
    if plan is None:
        return {
            "protocol_id": value.protocol_id,
            "mode": "missing",
            "requirement_count": 0,
            "allowed_action": "start_data_analysis",
        }
    return {
        "protocol_id": value.protocol_id,
        "mode": plan.mode,
        "requirement_count": len(plan.requirements),
        "requirements": [
            {
                "index": index,
                "fulfillment_mode": item.fulfillment.mode,
                "assertion_count": len(item.fulfillment.assertions)
                if hasattr(item.fulfillment, "assertions")
                else 0,
            }
            for index, item in enumerate(plan.requirements)
        ],
        "allowed_action": "start_data_analysis",
    }


def _mode_transition_issue(
    first_failure: AgentFailure,
    repaired: RunOpeningDecision,
    first_decision: RunOpeningDecision | None = None,
) -> OpeningValidationIssue | None:
    """Allow only the explicit permission-lowering transition during Repair."""

    first_mode: str | None = (
        first_decision.plan.mode
        if first_decision is not None
        and first_decision.protocol_id == "data-analysis"
        and first_decision.plan is not None
        else None
    )
    if isinstance(first_failure, RunOpeningInvalidFailure):
        summary = first_failure.attempt_summary
        first_mode = summary.trusted_mode if summary is not None else None
    if first_mode is None:
        return None
    repaired_mode = repaired.plan.mode if repaired.plan is not None else None
    allowed = {first_mode}
    if first_mode == "ready":
        allowed.add("discovery")
    if repaired_mode in allowed:
        return None
    return OpeningValidationIssue(
        path="plan.mode",
        error_type="mode_transition_conflict",
        repair_reason="protocol_direction_conflict",
        actual=repaired_mode or "missing",
        expected="|".join(sorted(allowed)),
        rule="Repair 只能保持首轮可信 mode；ready 失败时唯一允许降级为 discovery。",
        action="保持首轮可信 mode；只有 ready 无法形成最小合同才可改为 discovery。",
        suggested_mode=(
            first_mode
            if first_mode in {"ready", "discovery", "clarification", "context_only"}
            else None
        ),
    )
