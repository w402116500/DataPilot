"""在创建 Run 请求内解析一次可执行的私有上下文。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path, PurePosixPath

from agent_runtime.contracts import CONTEXT_PROJECTION_VERSION, ConversationContext, RunContext
from application.run_execution import RunExecutionContext
from application.script_workspace import SessionWorkspaceManager
from application.secret_cipher import SecretCipher
from application.source_access import resolve_source_access
from contracts.datasources import SchemaSummaryRead
from contracts.errors import AppError, ErrorCode
from contracts.model_profiles import CAPABILITY_CONTRACT_VERSION
from contracts.runs import ModelRuntimeSnapshot
from contracts.status import DataSourceStatus, ModelProfileStatus
from data_gateway.types import FileSourceAccess, GatewaySourceSnapshot
from metadata.models import DataSourceModel, ModelProfileModel, SessionModel
from metadata.repositories import (
    DataSourceRepository,
    ModelProfileRepository,
    SecretRepository,
)
from pydantic import ValidationError
from server.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.conversation_memory import ConversationContextSnapshot, ConversationMemoryService

logger = logging.getLogger(__name__)


def _schema_dialect(datasource: DataSourceModel) -> str:
    """读取已验证的冻结 Schema 方言；不为损坏数据补默认值。"""

    try:
        schema = SchemaSummaryRead.model_validate(datasource.schema_cache_json)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.DATASOURCE_NOT_READY,
            "当前数据源的 Schema 快照无效，请重新准备数据源",
            status_code=409,
            details={"datasource_id": datasource.id},
        ) from exc
    if schema.datasource_id != datasource.id:
        raise AppError(
            ErrorCode.DATASOURCE_NOT_READY,
            "当前数据源的 Schema 快照身份不匹配，请重新准备数据源",
            status_code=409,
            details={"datasource_id": datasource.id},
        )
    return schema.dialect


def _context_digest(value: str) -> str:
    """上下文快照只保留不可逆摘要，不把用户原话写进诊断 hash 输入。"""

    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


class RunContextResolver:
    """创建请求的依赖解析器，密钥只在返回的内存对象中短暂存在。"""

    def __init__(self, db: AsyncSession, settings: Settings, cipher: SecretCipher) -> None:
        self._db = db
        self._settings = settings
        self._cipher = cipher
        self._session_workspaces = SessionWorkspaceManager(settings.session_workspace_root)

    async def resolve(
        self,
        *,
        run_id: str,
        session: SessionModel,
        question: str,
        high_water_before_run: int,
    ) -> RunExecutionContext:
        """读取当前可用对象并冻结其身份；失败时不创建任何 Run 或用户消息。"""

        datasource = await self._resolve_datasource(session)
        profile = await self._resolve_profile()
        api_key = await self._resolve_api_key(profile)
        access = await resolve_source_access(
            self._db, self._cipher, datasource, self._settings.datasource_root
        )
        snapshot = None
        if isinstance(access, FileSourceAccess):
            snapshot = await self._session_workspaces.create_input_snapshot(
                session_id=session.id,
                run_id=run_id,
                source_path=access.path,
            )
            access = FileSourceAccess(snapshot.path)
        source_snapshot = GatewaySourceSnapshot.from_source(datasource)
        context_snapshot = await self._resolve_conversation_context(
            session_id=session.id,
            datasource_id=datasource.id,
            high_water_before_run=high_water_before_run,
        )
        conversation_context = context_snapshot.context
        preference_revision = context_snapshot.preference_revision
        datasource_context_revision = context_snapshot.datasource_context_revision
        pending_id = context_snapshot.pending_id
        pending_revision = context_snapshot.pending_revision
        historical_ids = context_snapshot.historical_summary_ids
        through_position = high_water_before_run
        context_hash = hashlib.sha256(
            json.dumps(
                {
                    "projection_version": CONTEXT_PROJECTION_VERSION,
                    "question_digest": _context_digest(question),
                    "schema_revision": datasource.schema_revision,
                    "preference_revision": preference_revision,
                    "datasource_context_revision": datasource_context_revision,
                    "pending_id": pending_id,
                    "pending_revision": pending_revision,
                    "through_message_position": through_position,
                    "recent_user_turn_digests": [
                        _context_digest(item.content_text)
                        for item in conversation_context.recent_user_turns
                    ],
                    "historical_summaries": [
                        {
                            "id": item_id,
                            "content_digest": _context_digest(item.content_text),
                        }
                        for item_id, item in zip(
                            historical_ids,
                            conversation_context.historical_summaries,
                            strict=False,
                        )
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        runtime_limits = self._settings.runtime_limits
        context_window_tokens = (
            profile.context_window_tokens or runtime_limits.context_window_tokens
        )
        output_budget_tokens = runtime_limits.model_max_output_tokens
        system_budget_tokens = 4_096
        tool_schema_cost = 8_192
        safety_margin_tokens = 1_024
        input_budget_tokens = max(
            1,
            context_window_tokens
            - output_budget_tokens
            - system_budget_tokens
            - tool_schema_cost
            - safety_margin_tokens,
        )
        model = ModelRuntimeSnapshot(
            profile_id=profile.id,
            provider="openai-compatible",
            model_name=profile.model_name,
            base_url=profile.base_url,
            temperature=profile.temperature,
            run_timeout_seconds=profile.run_timeout_seconds,
            final_output_mode=profile.final_output_mode,
            model_capability_fingerprint=profile.capability_fingerprint,
            context_window_tokens=context_window_tokens,
            context_window_source=(
                "profile" if profile.context_window_tokens else "runtime_fallback"
            ),
            input_budget_tokens=input_budget_tokens,
            output_budget_tokens=output_budget_tokens,
            system_budget_tokens=system_budget_tokens,
            tool_schema_cost=tool_schema_cost,
            safety_margin_tokens=safety_margin_tokens,
        )
        return RunExecutionContext(
            run_context=RunContext(
                run_id=run_id,
                session_id=session.id,
                datasource_id=datasource.id,
                model_profile_id=profile.id,
                model_name=profile.model_name,
                schema_revision=datasource.schema_revision,
                connection_revision=datasource.connection_revision,
                datalink_graph_version=datasource.datalink_graph_version,
                input_snapshot_ref=snapshot.relative_ref if snapshot else None,
                input_filename=snapshot.filename if snapshot else None,
                mask_fields=list(datasource.mask_fields_json or []),
                question=question,
                context_projection_version=CONTEXT_PROJECTION_VERSION,
                context_through_message_position=through_position,
                session_preferences_revision=preference_revision,
                datasource_context_revision=datasource_context_revision,
                pending_id=pending_id,
                pending_revision=pending_revision,
                historical_summary_ids=historical_ids,
                context_snapshot_hash=context_hash,
                context_load_status=conversation_context.load_status,
                datasource_display_name=datasource.name,
                datasource_description=(
                    datasource.description.strip()[:2_000]
                    if datasource.description and datasource.description.strip()
                    else None
                ),
                datasource_type=datasource.type,
                query_dialect=_schema_dialect(datasource),
                conversation_context=conversation_context,
            ),
            model=model,
            api_key=api_key,
            sandbox_image=self._settings.sandbox_image,
            input_snapshot_path=str(snapshot.path) if snapshot else None,
            source_access=access,
            source_snapshot=source_snapshot,
        )

    async def _resolve_datasource(self, session: SessionModel) -> DataSourceModel:
        datasource_id = session.selected_datasource_id
        if datasource_id is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "当前会话尚未选择可分析的数据源",
                status_code=409,
            )
        datasource = await DataSourceRepository(self._db).get(datasource_id)
        if datasource is None:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_FOUND,
                "会话选择的数据源不存在",
                status_code=404,
            )
        if (
            datasource.status
            not in {
                DataSourceStatus.SCHEMA_READY.value,
                DataSourceStatus.READY.value,
            }
            or not datasource.mask_fields_confirmed
            or datasource.schema_cache_json is None
            or datasource.schema_revision < 1
        ):
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "当前会话的数据源尚未准备完成",
                status_code=409,
                details={"datasource_id": datasource.id},
            )
        return datasource

    def _source_path(self, datasource: DataSourceModel) -> Path:
        """把数据源相对引用解析到受控根目录，供创建快照使用。"""

        source_ref = datasource.source_ref
        if not source_ref:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY,
                "数据源文件不可用",
                status_code=409,
            )
        relative = PurePosixPath(source_ref)
        if (
            relative.is_absolute()
            or "\\" in source_ref
            or ".." in relative.parts
            or any(part in {"", "."} for part in relative.parts)
        ):
            raise AppError(ErrorCode.DATASOURCE_NOT_READY, "数据源文件不可用", status_code=409)
        target = (self._settings.datasource_root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(self._settings.datasource_root.resolve())
        except ValueError as exc:
            raise AppError(
                ErrorCode.DATASOURCE_NOT_READY, "数据源文件不可用", status_code=409
            ) from exc
        if not target.is_file() or target.is_symlink():
            raise AppError(ErrorCode.DATASOURCE_NOT_READY, "数据源文件不可用", status_code=409)
        return target

    async def _resolve_conversation_context(
        self,
        *,
        session_id: str,
        datasource_id: str,
        high_water_before_run: int,
    ) -> ConversationContextSnapshot:
        """会话背景可降级，但版本和安全摘要 ID 必须与同一次读取绑定。"""

        try:
            return await ConversationMemoryService(self._db).build_context_snapshot(
                session_id=session_id,
                datasource_id=datasource_id,
                high_water_before_run=high_water_before_run,
            )
        except Exception:
            logger.warning(
                "Conversation memory context unavailable; continuing without it",
                extra={"session_id": session_id},
            )
            return ConversationContextSnapshot(
                context=ConversationContext(load_status="degraded"),
                preference_revision=0,
                datasource_context_revision=0,
                pending_id=None,
                pending_revision=0,
                historical_summary_ids=[],
            )

    async def _resolve_profile(self) -> ModelProfileModel:
        profile = await ModelProfileRepository(self._db).get_active()
        if profile is None:
            raise AppError(
                ErrorCode.MODEL_PROFILE_NOT_READY,
                "尚未设置可用的激活模型",
                status_code=409,
            )
        if (
            profile.status != ModelProfileStatus.TESTED.value
            or profile.secret_ref is None
            or profile.tool_calling_supported is not True
            or profile.final_output_mode is None
            or profile.capability_contract_version != CAPABILITY_CONTRACT_VERSION
            or profile.capability_fingerprint is None
        ):
            raise AppError(
                ErrorCode.MODEL_PROFILE_NOT_READY,
                "激活模型尚未通过原生工具调用能力检查",
                status_code=409,
                details={"profile_id": profile.id},
            )
        return profile

    async def _resolve_api_key(self, profile: ModelProfileModel) -> str:
        secret = await SecretRepository(self._db).get(profile.secret_ref)
        if secret is None:
            raise AppError(
                ErrorCode.MODEL_PROFILE_NOT_READY,
                "激活模型的密钥不可用",
                status_code=409,
                details={"profile_id": profile.id},
            )
        try:
            return self._cipher.decrypt(secret.encrypted_value)
        except Exception as exc:
            raise AppError(
                ErrorCode.MODEL_PROFILE_NOT_READY,
                "激活模型的密钥不可用",
                status_code=409,
                details={"profile_id": profile.id},
            ) from exc
