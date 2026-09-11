"""Run 终态、正式答案与终态事件的唯一写入入口。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from agent_runtime.contracts import AnalysisClaimAuditSummary, AnalysisOutcome
from contracts.ids import make_id
from contracts.run_events import RunEventCreate, RunEventType
from contracts.status import MessageRole, RunStatus
from metadata.repositories import MessageRepository, RunRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.conversation_memory import ConversationMemoryService
from runtime.run_event_pipeline import RunEventNotifier, RunEventPipeline

logger = logging.getLogger(__name__)
CompletionKind = Literal["completed", "partial", "clarification"]
_MAX_CLAIM_AUDIT_EVENT_BYTES = 7_000
_MAX_CLAIM_AUDIT_EVENT_FACTS = 64


class RunFinalizer:
    """所有终态都经过这一处，保证终态事件与正式 Assistant Message 唯一。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: RunEventNotifier,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier

    async def complete(self, run_id: str, outcome: AnalysisOutcome) -> bool:
        """写入完整回答；缺少完整证据时收为 partial，而不是伪装为失败。"""

        if outcome.completion_kind == "partial":
            return await self.complete_partial(
                run_id,
                incomplete_reason=outcome.incomplete_reason or "OUTCOME_INCOMPLETE",
                outcome=outcome,
            )
        if outcome.completion_kind == "clarification":
            answer = _formal_answer(outcome)
            if answer is None:
                return False
            return await self._complete_success(
                run_id,
                answer=answer,
                outcome=outcome,
                completion_kind="clarification",
                incomplete_reason=None,
            )
        answer = _formal_answer(outcome)
        if answer is None:
            return await self.complete_partial(
                run_id,
                incomplete_reason="OUTCOME_INCOMPLETE",
                outcome=outcome,
            )
        return await self._complete_success(
            run_id,
            answer=answer,
            outcome=outcome,
            completion_kind="completed",
            incomplete_reason=None,
        )

    async def complete_partial(
        self,
        run_id: str,
        *,
        incomplete_reason: str,
        outcome: AnalysisOutcome | None = None,
    ) -> bool:
        """总时限或额度耗尽时保存候选答案，并以成功终态结束 Run。"""

        safe_reason = _safe_incomplete_reason(incomplete_reason)
        answer = _partial_answer(outcome)
        partial_outcome = outcome or AnalysisOutcome(
            answer=answer,
            completion_kind="partial",
            incomplete_reason=safe_reason,
        )
        return await self._complete_success(
            run_id,
            answer=answer,
            outcome=partial_outcome,
            completion_kind="partial",
            incomplete_reason=safe_reason,
        )

    async def _complete_success(
        self,
        run_id: str,
        *,
        answer: str,
        outcome: AnalysisOutcome,
        completion_kind: CompletionKind,
        incomplete_reason: str | None,
    ) -> bool:
        protocol_error = await self._validate_outcome_protocol(run_id, outcome)
        if protocol_error is not None:
            await self.fail(
                run_id,
                error_code=protocol_error[0],
                error_message=protocol_error[1],
            )
            return False
        summary_session_id: str | None = None
        summary_datasource_id: str | None = None
        claim_audit_summary_json, claim_audit_truncated = _claim_audit_event_payload(
            outcome.claim_audits
        )
        async with self._session_factory() as db:
            events = RunEventPipeline(db, notify=self._notifier.notify)
            async with events.transaction(run_id):
                run = await RunRepository(db).get(run_id)
                if run is None or _is_terminal(run.status):
                    return False
                assistant = await MessageRepository(db).create(
                    session_id=run.session_id,
                    datasource_id=run.datasource_id,
                    run_id=run.id,
                    role=MessageRole.ASSISTANT,
                    content_text=answer,
                    answer_evidence_refs=outcome.evidence_refs or None,
                )
                run.assistant_message_id = assistant.id
                run.status = RunStatus.SUCCEEDED.value
                run.completion_kind = completion_kind
                run.incomplete_reason = incomplete_reason
                run.error_code = None
                run.error_message = None
                run.finished_at = datetime.now(UTC)
                run.answer_data_freshness = _answer_data_freshness(outcome)
                memory = ConversationMemoryService(db)
                if completion_kind == "clarification" and outcome.clarification is not None:
                    updated = await memory.set_pending(
                        session_id=run.session_id,
                        datasource_id=run.datasource_id,
                        pending=outcome.clarification,
                        pending_id=make_id("pending"),
                        expected_revision=run.datasource_context_revision,
                    )
                    if not updated:
                        logger.warning(
                            "Pending clarification changed while Run was executing",
                            extra={
                                "run_id": run.id,
                                "error_code": "CONTEXT_REVISION_CONFLICT",
                            },
                        )
                elif (
                    completion_kind == "completed"
                    and outcome.protocol_id == "data-analysis"
                    and outcome.consumes_pending
                ):
                    cleared = await memory.clear_pending(
                        session_id=run.session_id,
                        datasource_id=run.datasource_id,
                        expected_revision=run.datasource_context_revision,
                    )
                    if not cleared:
                        logger.warning(
                            "Pending clarification was kept after context revision conflict",
                            extra={
                                "run_id": run.id,
                                "error_code": "CONTEXT_REVISION_CONFLICT",
                            },
                        )
                await events.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.ANSWER_READY,
                        payload={
                            "assistant_message_id": assistant.id,
                            "evidence_count": len(outcome.evidence_refs),
                            "artifact_count": len(outcome.artifact_refs),
                            "answer_format": "markdown",
                            "completion_kind": completion_kind,
                            "claim_audit_summary_json": claim_audit_summary_json,
                            "claim_audit_truncated": claim_audit_truncated,
                            "answer_data_freshness": run.answer_data_freshness,
                            "historical_context_injected": bool(run.historical_context_injected),
                            "historical_summary_count": run.historical_summary_count or 0,
                            **(
                                {"incomplete_reason": incomplete_reason}
                                if incomplete_reason is not None
                                else {}
                            ),
                        },
                    )
                )
                await events.append(RunEventCreate(run_id=run.id, type=RunEventType.RUN_SUCCEEDED))
                await db.commit()
                if completion_kind in {"completed", "partial"}:
                    summary_session_id = run.session_id
                    summary_datasource_id = run.datasource_id
        await self._notifier.notify(run_id)
        if summary_session_id is not None and summary_datasource_id is not None:
            try:
                # 历史摘要在正式终态提交后独立幂等写入；失败不影响 Answer/terminal。
                historical_text = _historical_summary_text(outcome)
                if historical_text is not None:
                    async with self._session_factory() as summary_db:
                        await ConversationMemoryService(summary_db).project_historical_answer(
                            session_id=summary_session_id,
                            datasource_id=summary_datasource_id,
                            source_run_id=run_id,
                            topic=_historical_topic(historical_text, outcome.answer),
                            content_text=historical_text,
                            data_freshness=(
                                "not_queried"
                                if outcome.protocol_id == "general-task"
                                else "historical_not_current"
                            ),
                        )
                        await summary_db.commit()
                else:
                    async with self._session_factory() as summary_db:
                        await RunRepository(summary_db).mark_historical_summary_projection(
                            run_id, status="not_eligible"
                        )
                        await summary_db.commit()
            except Exception:
                # 正式答案和终态已经提交，摘要只能降级，不能反向改变 Run 成败。
                logger.warning("Conversation summary update failed", exc_info=True)
                try:
                    async with self._session_factory() as status_db:
                        await RunRepository(status_db).mark_historical_summary_projection(
                            run_id, status="failed"
                        )
                        await status_db.commit()
                except Exception:
                    logger.warning(
                        "Historical summary projection status update failed", exc_info=True
                    )
        return True

    async def _validate_outcome_protocol(
        self,
        run_id: str,
        outcome: AnalysisOutcome,
    ) -> tuple[str, str] | None:
        """成功收尾前确认 Run 事实与 Runtime 结果使用同一协议。"""

        async with self._session_factory() as db:
            run = await RunRepository(db).get(run_id)
            if run is None:
                return ("RUN_PROTOCOL_UNRESOLVED", "Run 不存在，无法确认运行协议")
            if run.protocol_id is None:
                return ("RUN_PROTOCOL_UNRESOLVED", "Run 尚未选定协议，不能成功收尾")
            if run.protocol_id != outcome.protocol_id:
                return ("RUN_PROTOCOL_CONFLICT", "Run 协议与运行结果不一致")
        return None

    async def fail(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        canceled: bool = False,
    ) -> bool:
        """写唯一失败或取消终态和简短系统说明；调用方不能自行补写终态事件。"""

        safe_error_message = _safe_failure_detail(error_message)
        user_message = _failure_message(
            error_code,
            canceled=canceled,
            detail=safe_error_message,
        )
        async with self._session_factory() as db:
            events = RunEventPipeline(db, notify=self._notifier.notify)
            async with events.transaction(run_id):
                run = await RunRepository(db).get(run_id)
                if run is None or _is_terminal(run.status):
                    return False
                assistant = await MessageRepository(db).create(
                    session_id=run.session_id,
                    datasource_id=run.datasource_id,
                    run_id=run.id,
                    role=MessageRole.ASSISTANT,
                    content_text=user_message,
                )
                run.assistant_message_id = assistant.id
                run.status = RunStatus.CANCELED.value if canceled else RunStatus.FAILED.value
                run.completion_kind = None
                run.incomplete_reason = None
                run.error_code = error_code
                run.error_message = user_message[:500]
                run.finished_at = datetime.now(UTC)
                await events.finalize_pending_projections(run.id, error_code=error_code)
                await events.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.RUN_CANCELED if canceled else RunEventType.RUN_FAILED,
                        payload={"error_code": error_code, "error_message": user_message},
                    )
                )
                await db.commit()
        await self._notifier.notify(run_id)
        return True


def _formal_answer(outcome: AnalysisOutcome) -> str | None:
    """完整答案直接保存 Graph 已校验过的单份 Markdown。"""

    answer = outcome.answer.strip()
    return answer or None


def _partial_answer(outcome: AnalysisOutcome | None) -> str:
    """不完整回答保留已经生成的候选段落；没有候选时给出明确的安全说明。"""

    if outcome is not None:
        answer = outcome.answer.strip()
        if answer:
            return answer
    return "本次分析未形成完整结论。"


def _historical_topic(historical_text: str | None, answer: str) -> str:
    """历史摘要标题优先使用 Claim 摘要，避免把 partial 占位句带入下一轮。"""

    return (historical_text or answer)[:300]


def _historical_summary_text(outcome: AnalysisOutcome) -> str | None:
    """仅把安全答案或已通过 Claim 的摘要写入下一 Run 的历史上下文。"""

    if outcome.protocol_id == "general-task":
        return outcome.answer.strip()[:1_200] or None
    if not outcome.claim_audits:
        return None
    parts = [
        item.target_summary.strip()
        for item in outcome.claim_audits
        if item.commit_status == "passed"
        and item.fact_validation_status == "passed"
        and item.target_summary.strip()
    ]
    return "；".join(dict.fromkeys(parts))[:1_200] or None


def _answer_data_freshness(outcome: AnalysisOutcome) -> str:
    if outcome.protocol_id == "general-task":
        return "not_queried"
    if any(
        item.commit_status == "passed" and item.fact_validation_status == "passed"
        for item in outcome.claim_audits
    ):
        return "current_run_evidence"
    if not outcome.observations_performed and (
        not outcome.evidence_refs or set(outcome.evidence_refs) <= {"schema"}
    ):
        return "current_schema"
    return "current_run_observation_only"


def _safe_incomplete_reason(value: str) -> str:
    if not value or len(value) > 120:
        return "OUTCOME_INCOMPLETE"
    for char in value:
        if not (char.isupper() or char.isdigit() or char == "_"):
            return "OUTCOME_INCOMPLETE"
    return value


def _is_terminal(status: str) -> bool:
    return status in {
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELED.value,
    }


def _failure_message(error_code: str, *, canceled: bool, detail: str | None = None) -> str:
    """由稳定错误码和已安全化的结果层细节组成用户文案。"""

    if canceled:
        base = "本次分析已取消。"
        return f"{base} {detail}" if detail and detail not in base else base
    if error_code == "RUN_TIMEOUT":
        base = "本次分析超时，已停止并完成清理。"
    elif error_code == "PROCESS_RESTARTED":
        base = "服务重启导致本次分析未完成。"
    else:
        base = "本次分析未完成。"
    return f"{base} {detail}" if detail and detail not in base else base


def _safe_failure_detail(value: str | None) -> str | None:
    """过滤底层异常，只保留可展示的短结果层说明。"""

    if not isinstance(value, str):
        return None
    detail = " ".join(value.split())[:500]
    if not detail:
        return None
    lowered = detail.casefold()
    if any(
        marker in lowered
        for marker in (
            "select ",
            "insert ",
            "update ",
            "delete ",
            "tool_call",
            "run_id",
            "api_key",
            "password",
            "c:\\",
            "/",
        )
    ):
        return None
    return detail


def _claim_audit_event_payload(
    audits: Sequence[AnalysisClaimAuditSummary],
) -> tuple[str, bool]:
    """把 Claim 摘要限制在事件预算内，并显式标记审计摘要是否被截断。"""

    projected: list[dict[str, object]] = []
    truncated = len(audits) > 16
    for item in audits[:16]:
        value = item.model_dump(mode="json")
        facts = value.get("facts")
        if isinstance(facts, list) and len(facts) > _MAX_CLAIM_AUDIT_EVENT_FACTS:
            value["facts"] = facts[:_MAX_CLAIM_AUDIT_EVENT_FACTS]
            truncated = True
        projected.append(value)

    def encoded() -> str:
        return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))

    while len(encoded().encode("utf-8")) > _MAX_CLAIM_AUDIT_EVENT_BYTES:
        candidates = [
            (index, len(item.get("facts", [])))
            for index, item in enumerate(projected)
            if isinstance(item.get("facts"), list) and item["facts"]
        ]
        if candidates:
            index, _ = max(candidates, key=lambda item: item[1])
            projected[index]["facts"].pop()
            truncated = True
            continue
        if len(projected) > 1:
            projected.pop()
            truncated = True
            continue
        if projected and len(projected[0].get("target_summary", "")) > 120:
            projected[0]["target_summary"] = projected[0]["target_summary"][:120]
            truncated = True
            continue
        break
    return encoded(), truncated
