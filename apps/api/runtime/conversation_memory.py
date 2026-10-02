"""当前 Session/DataSource 上下文的安全读取与历史答案投影。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from agent_runtime.contracts import (
    AnalysisClarificationDraft,
    ConversationContext,
    HistoricalAnswerSummary,
    RecentUserTurnProjection,
    SessionPreferenceProjection,
)
from contracts.status import MessageRole
from metadata.models import MessageModel
from metadata.repositories import (
    DatasourceConversationStateRepository,
    HistoricalAnswerSummaryRepository,
    MessageRepository,
    RunRepository,
    SessionPreferencesRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_RECENT_MESSAGE_LIMIT = 6
_RECENT_MESSAGE_SCAN_LIMIT = 72
_RECENT_MESSAGE_MAX_CHARS = 4_000
_MAX_CONTEXT_MESSAGE_CHARS = 800

_SENSITIVE_MEMORY_RE = re.compile(
    r"(?:api[_-]?key|secret|password|credential|access[_-]?token|authorization|"
    r"密钥|密码|凭证|令牌|令牌|token)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:"
    r"[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])*[^\\/\s]*"
    r"|\\\\[^\\/\s]+[\\/][^\\/\s]+(?:[\\/][^\\/\s]+)*"
    r"|/(?:Users|home|tmp|var|etc|workspace)(?:/[^\s/]+)+"
    r")"
)
_RAW_SQL_RE = re.compile(
    r"(?ix)(?:^|(?<=[\s(`]))"
    r"(?:"
    r"(?:select|insert|update|delete|drop|alter|create|pragma)\s+[A-Za-z0-9_*\"'`(]"
    r"|with(?:\s+recursive)?\s+[A-Za-z_][A-Za-z0-9_$]*\s+as\s*\("
    r")"
)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_PREFERENCE_RE = re.compile(
    r"(?:偏好|希望|喜欢|以后|后续|默认|请.*(?:简短|简洁|图表|渠道|拆分)|不要.*(?:冗长|复杂|图表))"
)


@dataclass(frozen=True)
class ConversationContextSnapshot:
    """一次读取得到的安全上下文及其内部版本诊断。"""

    context: ConversationContext
    preference_revision: int
    datasource_context_revision: int
    pending_id: str | None
    pending_revision: int
    historical_summary_ids: list[str]


class ConversationMemoryService:
    """只组装当前会话背景，绝不读取其他会话、Data Gateway、DataLink 或 Docker。"""

    def __init__(self, db: AsyncSession) -> None:
        self._messages = MessageRepository(db)
        self._preferences = SessionPreferencesRepository(db)
        self._states = DatasourceConversationStateRepository(db)
        self._historical = HistoricalAnswerSummaryRepository(db)

    async def build_context(
        self,
        *,
        session_id: str,
        datasource_id: str,
        high_water_before_run: int | None = None,
    ) -> ConversationContext:
        """读取当前会话的安全背景；版本和摘要 ID 由快照读取器一并冻结。"""

        snapshot = await self.build_context_snapshot(
            session_id=session_id,
            datasource_id=datasource_id,
            high_water_before_run=high_water_before_run,
        )
        return snapshot.context

    async def build_context_snapshot(
        self,
        *,
        session_id: str,
        datasource_id: str,
        high_water_before_run: int | None = None,
    ) -> ConversationContextSnapshot:
        """一次性读取上下文、版本和安全摘要 ID，避免 Resolver 二次读取漂移。"""

        degraded = False
        try:
            message_models = await self._messages.list_recent(
                session_id,
                limit=_RECENT_MESSAGE_SCAN_LIMIT,
                datasource_id=datasource_id,
                before_position=high_water_before_run,
            )
        except Exception:
            logger.warning(
                "Conversation message projection unavailable",
                extra={"session_id": session_id, "datasource_id": datasource_id},
            )
            message_models = []
            degraded = True
        recent_user_turns = _bound_recent_user_turns(message_models)

        try:
            preferences = await self._preferences.get(session_id)
        except Exception:
            logger.warning(
                "Session preferences unavailable",
                extra={"session_id": session_id},
            )
            preferences = None
            degraded = True
        preference_revision = preferences.revision if preferences is not None else 0
        preference_values = _preference_projection(
            preferences.preferences_json if preferences else None
        )
        try:
            state = await self._states.get(session_id, datasource_id)
        except Exception:
            logger.warning(
                "Datasource conversation state unavailable",
                extra={"session_id": session_id, "datasource_id": datasource_id},
            )
            state = None
            degraded = True
        datasource_context_revision = state.revision if state is not None else 0
        pending = None
        pending_id: str | None = None
        pending_revision = datasource_context_revision
        if state is not None:
            pending_id = state.pending_id
            pending_revision = state.revision
            has_pending_id = state.pending_id is not None
            has_pending_payload = state.pending_json is not None
            if has_pending_id != has_pending_payload:
                # 状态行内部不一致，不能伪装成“没有 pending”。
                degraded = True
                pending_id = None
            elif has_pending_payload:
                if not isinstance(state.pending_json, dict):
                    degraded = True
                    pending_id = None
                else:
                    try:
                        pending = AnalysisClarificationDraft.model_validate(
                            state.pending_json, strict=True
                        )
                    except (TypeError, ValueError):
                        degraded = True
                        pending = None
                        pending_id = None
        historical = []
        historical_summary_ids: list[str] = []
        try:
            historical_models = await self._historical.list_recent(
                session_id=session_id, datasource_id=datasource_id, limit=3
            )
        except Exception:
            logger.warning(
                "Historical answer summaries unavailable",
                extra={"session_id": session_id, "datasource_id": datasource_id},
            )
            historical_models = []
            degraded = True
        for item in historical_models:
            if item.provenance != "historical_answer_summary":
                degraded = True
                continue
            safe = _safe_memory_text(item.content_text, maximum=1_200)
            if safe is not None:
                historical_summary_ids.append(item.id)
                historical.append(
                    HistoricalAnswerSummary(
                        topic=item.topic,
                        content_text=safe,
                        data_freshness=(
                            "not_queried"
                            if item.data_freshness == "not_queried"
                            else "historical_not_current"
                        ),
                    )
                )
        has_context = bool(
            recent_user_turns
            or preference_values.model_dump(exclude_none=True)
            or pending
            or historical
        )
        context = ConversationContext(
            session_preferences=preference_values,
            recent_user_turns=recent_user_turns,
            pending_clarification=pending,
            historical_summaries=historical,
            load_status="degraded" if degraded else ("ready" if has_context else "empty"),
        )
        return ConversationContextSnapshot(
            context=context,
            preference_revision=preference_revision,
            datasource_context_revision=datasource_context_revision,
            pending_id=pending_id,
            pending_revision=pending_revision,
            historical_summary_ids=historical_summary_ids,
        )

    async def save_preference_from_user_message(
        self, *, session_id: str, position: int, content_text: str
    ) -> None:
        preference = _extract_preference(content_text)
        if preference is None:
            return
        existing = await self._preferences.get(session_id)
        values = dict(existing.preferences_json) if existing and existing.preferences_json else {}
        values.update(preference)
        await self._preferences.upsert(
            session_id=session_id,
            through_message_position=position,
            preferences={
                key: values[key]
                for key in ("response_language", "verbosity", "answer_format", "chart_preference")
                if key in values
            },
        )

    async def set_pending(
        self,
        *,
        session_id: str,
        datasource_id: str,
        pending: AnalysisClarificationDraft,
        pending_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        result = await self._states.set_pending(
            session_id=session_id,
            datasource_id=datasource_id,
            pending_id=pending_id,
            pending_json=pending.model_dump(mode="json"),
            expected_revision=expected_revision,
        )
        return result is not None

    async def clear_pending(
        self,
        *,
        session_id: str,
        datasource_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        return await self._states.clear(
            session_id=session_id,
            datasource_id=datasource_id,
            expected_revision=expected_revision,
        )

    async def state_revisions(
        self, *, session_id: str, datasource_id: str
    ) -> tuple[int, int, list[str]]:
        preferences = await self._preferences.get(session_id)
        state = await self._states.get(session_id, datasource_id)
        summaries = await self._historical.list_recent(
            session_id=session_id, datasource_id=datasource_id, limit=3
        )
        return (
            preferences.revision if preferences is not None else 0,
            state.revision if state is not None else 0,
            [item.id for item in summaries if _safe_memory_text(item.content_text, maximum=1_200)],
        )

    async def pending_snapshot(
        self, *, session_id: str, datasource_id: str
    ) -> tuple[str | None, int]:
        """返回当前 DataSource 的 pending 身份和版本，供 Run 冻结诊断。"""

        state = await self._states.get(session_id, datasource_id)
        if state is None or state.pending_id is None or state.pending_json is None:
            return None, state.revision if state is not None else 0
        return state.pending_id, state.revision

    async def project_historical_answer(
        self,
        *,
        session_id: str,
        datasource_id: str,
        source_run_id: str,
        topic: str,
        content_text: str,
        data_freshness: str,
    ) -> str:
        source_run = await RunRepository(self._messages.db).get(source_run_id)
        if (
            source_run is None
            or source_run.session_id != session_id
            or source_run.datasource_id != datasource_id
            or source_run.finished_at is None
        ):
            await RunRepository(self._messages.db).mark_historical_summary_projection(
                source_run_id, status="not_eligible"
            )
            return "not_eligible"
        safe = _safe_memory_text(content_text, maximum=1_200)
        if safe is None:
            await RunRepository(self._messages.db).mark_historical_summary_projection(
                source_run_id, status="not_eligible"
            )
            return "not_eligible"
        existing = await self._historical.list_by_source_run(source_run_id)
        if existing is not None:
            await RunRepository(self._messages.db).mark_historical_summary_projection(
                source_run_id, status="already_present"
            )
            return "already_present"
        await self._historical.create_if_absent(
            session_id=session_id,
            datasource_id=datasource_id,
            source_run_id=source_run_id,
            topic=topic,
            content_text=safe,
            data_freshness=data_freshness,
            source_run_finished_at=source_run.finished_at,
        )
        await RunRepository(self._messages.db).mark_historical_summary_projection(
            source_run_id, status="created"
        )
        return "created"


def _bound_recent_user_turns(messages: list[MessageModel]) -> list[RecentUserTurnProjection]:
    """只投影当前数据源的用户原话，最多 6 条且总字符不超过 4,000。"""

    accepted: list[RecentUserTurnProjection] = []
    remaining = _RECENT_MESSAGE_MAX_CHARS
    for message in reversed(messages):
        # Assistant 正文可能包含旧 SQL、数字或产物内容，不能作为新 Run 的原话上下文。
        if message.role != MessageRole.USER.value:
            continue
        if len(accepted) >= _RECENT_MESSAGE_LIMIT or remaining <= 0:
            break
        content_text = _safe_memory_text(
            message.content_text,
            maximum=min(_MAX_CONTEXT_MESSAGE_CHARS, remaining),
        )
        if content_text is None:
            continue
        accepted.append(
            RecentUserTurnProjection(
                content_text=content_text,
            )
        )
        remaining -= len(content_text)
    return list(reversed(accepted))


def _preference_projection(preferences: dict[str, object] | None) -> SessionPreferenceProjection:
    """只投影 allowlist 偏好，忽略旧的任意文本 items。"""

    if not isinstance(preferences, dict):
        return SessionPreferenceProjection()
    allowed = {
        key: value
        for key, value in preferences.items()
        if key in {"response_language", "verbosity", "answer_format", "chart_preference"}
    }
    try:
        return SessionPreferenceProjection.model_validate(allowed, strict=True)
    except ValueError:
        return SessionPreferenceProjection()


def _extract_preference(value: str) -> dict[str, str] | None:
    """从用户明确的偏好句中提取有限枚举，不保存任意原话。"""

    text = " ".join(value.split()).strip().casefold()
    if not text or not _PREFERENCE_RE.search(text):
        return None
    result: dict[str, str] = {}
    if any(token in text for token in ("中文", "chinese", "zh-cn")):
        result["response_language"] = "zh-CN"
    elif any(token in text for token in ("英文", "英语", "english", "en-us")):
        result["response_language"] = "en-US"
    if any(token in text for token in ("简短", "简洁", "精简", "concise")):
        result["verbosity"] = "concise"
    elif any(token in text for token in ("详细", "展开", "detailed")):
        result["verbosity"] = "detailed"
    if "表格" in text or "table" in text:
        result["answer_format"] = "table"
    elif "markdown" in text:
        result["answer_format"] = "markdown"
    if "不要图表" in text or "避免图表" in text or "avoid chart" in text:
        result["chart_preference"] = "avoid"
    elif "图表" in text or "chart" in text:
        result["chart_preference"] = "required" if "需要" in text or "必须" in text else "allow"
    return result or None


def _safe_memory_text(value: str, *, maximum: int) -> str | None:
    """拒绝不应进入会话上下文的内容，并在边界内截断普通文本。"""

    text = " ".join(value.split()).strip()
    if not text:
        return None
    if (
        _SENSITIVE_MEMORY_RE.search(text)
        or _ABSOLUTE_PATH_RE.search(text)
        or _RAW_SQL_RE.search(text)
        or _EMAIL_RE.search(text)
        or _PHONE_RE.search(text)
    ):
        return None
    return text[:maximum]
