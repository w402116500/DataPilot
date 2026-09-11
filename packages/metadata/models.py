from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """生成带 UTC 时区的当前时间，供所有 Metadata 时间列使用。"""

    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有 SQLAlchemy Metadata 模型的声明基类。"""

    pass


class TimestampMixin:
    """为需要审计创建与更新时间的模型提供统一时间列。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class DataSourceModel(TimestampMixin, Base):
    """数据源 Metadata：保存受控文件引用、检查快照和删除 tombstone。"""

    __tablename__ = "data_sources"
    __table_args__ = (
        CheckConstraint("schema_revision >= 0", name="ck_data_sources_schema_revision"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(500), unique=True)
    source_kind: Mapped[str] = mapped_column(
        String(20), default="file", server_default="file", nullable=False
    )
    connection_config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    credential_ref: Mapped[str | None] = mapped_column(
        ForeignKey("secrets.id", ondelete="RESTRICT")
    )
    connection_fingerprint: Mapped[str | None] = mapped_column(String(64))
    connection_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    inspection_id: Mapped[str | None] = mapped_column(String(120))
    file_size: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    schema_cache_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    schema_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mask_fields_json: Mapped[list[str] | None] = mapped_column(JSON)
    mask_fields_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="uploaded", nullable=False)
    datalink_build_id: Mapped[str | None] = mapped_column(String(120))
    datalink_graph_version: Mapped[str | None] = mapped_column(String(120))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecretModel(TimestampMixin, Base):
    __tablename__ = "secrets"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)


class ConnectionGrantModel(Base):
    """Hashed short-lived authorization bound to one rebuild and one consumer Build."""

    __tablename__ = "connection_grants"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    datasource_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True
    )
    rebuild_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    connection_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    build_id: Mapped[str | None] = mapped_column(String(120))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelProfileModel(TimestampMixin, Base):
    __tablename__ = "model_profiles"
    __table_args__ = (
        Index(
            "uq_model_profiles_single_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        CheckConstraint(
            "final_output_mode IS NULL OR "
            "final_output_mode IN ('markdown', 'json_schema', 'json_object', 'submit_answer')",
            name="ck_model_profiles_final_output_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    run_timeout_seconds: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(ForeignKey("secrets.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(40), default="created", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tool_calling_supported: Mapped[bool | None] = mapped_column(Boolean)
    final_output_mode: Mapped[str | None] = mapped_column(String(30))
    capability_contract_version: Mapped[str | None] = mapped_column(String(40))
    capability_fingerprint: Mapped[str | None] = mapped_column(String(128))
    capability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_window_tokens: Mapped[int | None] = mapped_column(Integer)


class SessionModel(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    selected_datasource_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL")
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionPreferencesModel(TimestampMixin, Base):
    """Session 级安全偏好；偏好不绑定 DataSource，也不依赖 Run 成功。"""

    __tablename__ = "session_preferences"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    through_message_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    preferences_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class DatasourceConversationStateModel(TimestampMixin, Base):
    """当前 Session + DataSource 的唯一 pending clarification。"""

    __tablename__ = "conversation_context_states"
    __table_args__ = (
        Index("ix_conversation_context_states_active", "session_id", "datasource_id", "revision"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    datasource_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    pending_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class HistoricalAnswerSummaryModel(TimestampMixin, Base):
    """按 Run 不可变追加的安全历史答案摘要。"""

    __tablename__ = "historical_answer_summaries"
    __table_args__ = (
        UniqueConstraint("source_run_id", name="uq_historical_answer_summaries_source_run"),
        Index(
            "ix_historical_answer_summaries_session_datasource_finished",
            "session_id",
            "datasource_id",
            "source_run_finished_at",
            "source_run_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    datasource_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False
    )
    source_run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    source_run_finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(
        String(40), nullable=False, default="historical_answer_summary"
    )
    data_freshness: Mapped[str] = mapped_column(String(40), nullable=False)


class MessageModel(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "position", name="uq_messages_position"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    # 旧消息允许为空；空来源不能进入新的数据分析 Prompt。
    datasource_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="SET NULL")
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_sections_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    answer_evidence_refs_json: Mapped[list[str] | None] = mapped_column(JSON)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RunModel(TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_runs_session_idempotency_key"),
        Index(
            "uq_runs_single_active_session",
            "session_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
        CheckConstraint(
            "run_timeout_seconds IS NULL OR "
            "(run_timeout_seconds >= 1 AND run_timeout_seconds <= 600)",
            name="ck_runs_run_timeout_seconds",
        ),
        CheckConstraint(
            "final_output_mode IS NULL OR "
            "final_output_mode IN ('markdown', 'json_schema', 'json_object', 'submit_answer')",
            name="ck_runs_final_output_mode",
        ),
        CheckConstraint(
            "protocol_id IS NULL OR protocol_id IN ('general-task', 'data-analysis')",
            name="ck_runs_protocol_id",
        ),
        CheckConstraint(
            "completion_kind IS NULL OR completion_kind IN "
            "('completed', 'partial', 'clarification')",
            name="ck_runs_completion_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    datasource_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT")
    )
    user_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # 旧 Run 没有幂等键和固定超时，故迁移后两个字段仍允许为空。
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    model_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="SET NULL")
    )
    model_secret_ref: Mapped[str | None] = mapped_column(
        ForeignKey("secrets.id", ondelete="SET NULL")
    )
    model_provider: Mapped[str | None] = mapped_column(String(80))
    model_name: Mapped[str | None] = mapped_column(String(120))
    schema_revision: Mapped[int | None] = mapped_column(Integer)
    connection_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    datalink_graph_version: Mapped[str | None] = mapped_column(String(120))
    input_snapshot_ref: Mapped[str | None] = mapped_column(String(500))
    mask_fields_json: Mapped[list[str] | None] = mapped_column(JSON)
    run_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    final_output_mode: Mapped[str | None] = mapped_column(String(30))
    protocol_id: Mapped[str | None] = mapped_column(String(40))
    model_capability_fingerprint: Mapped[str | None] = mapped_column(String(128))
    session_preferences_revision: Mapped[int | None] = mapped_column(Integer)
    datasource_context_revision: Mapped[int | None] = mapped_column(Integer)
    context_through_message_position: Mapped[int | None] = mapped_column(Integer)
    pending_id_snapshot: Mapped[str | None] = mapped_column(String(120))
    pending_revision_snapshot: Mapped[int | None] = mapped_column(Integer)
    context_projection_version: Mapped[str | None] = mapped_column(String(40))
    context_snapshot_hash: Mapped[str | None] = mapped_column(String(128))
    context_load_status: Mapped[str | None] = mapped_column(String(20))
    historical_summary_ids_json: Mapped[list[str] | None] = mapped_column(JSON)
    historical_summary_count: Mapped[int | None] = mapped_column(Integer)
    answer_data_freshness: Mapped[str | None] = mapped_column(String(40))
    historical_context_injected: Mapped[bool | None] = mapped_column(Boolean)
    historical_summary_projection_status: Mapped[str | None] = mapped_column(String(24))
    historical_summary_projection_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    historical_summary_projection_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    context_window_tokens: Mapped[int | None] = mapped_column(Integer)
    context_window_source: Mapped[str | None] = mapped_column(String(24))
    input_budget_tokens: Mapped[int | None] = mapped_column(Integer)
    output_budget_tokens: Mapped[int | None] = mapped_column(Integer)
    system_budget_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_schema_cost: Mapped[int | None] = mapped_column(Integer)
    safety_margin_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_kind: Mapped[str | None] = mapped_column(String(20))
    incomplete_reason: Mapped[str | None] = mapped_column(String(120))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEventModel(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_seq"),
        Index("ix_run_events_run_seq", "run_id", "seq"),
        Index("ix_run_events_run_type", "run_id", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ToolCallModel(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_run_tool_call_id", "run_id", "tool_call_id"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    # 模型返回的编号只在同一个 Run 内关联事件；id 是数据库内部主键。
    tool_call_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SqlAuditLogModel(TimestampMixin, Base):
    """每次 SQL 尝试的一条审计记录，状态随实际执行过程更新。"""

    __tablename__ = "sql_audit_logs"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", "attempt_no", name="uq_sql_audit_attempt"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    tool_call_id: Mapped[str | None] = mapped_column(String(120))
    datasource_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT")
    )
    schema_revision: Mapped[int | None] = mapped_column(Integer)
    attempt_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    connection_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    repaired_from_id: Mapped[str | None] = mapped_column(
        ForeignKey("sql_audit_logs.id", ondelete="SET NULL")
    )
    original_sql: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_sql: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="proposed", nullable=False)
    statement_type: Mapped[str | None] = mapped_column(String(40))
    referenced_tables_json: Mapped[list[str] | None] = mapped_column(JSON)
    blocked_reason_code: Mapped[str | None] = mapped_column(String(80))
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id", ondelete="SET NULL"))
    row_count: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)


class ArtifactModel(Base):
    """已脱敏结果的 Metadata；文件位置可为空，以支持预览降级。"""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    tool_call_id: Mapped[str | None] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_ref: Mapped[str | None] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 完整结果已落盘时没有预览；使用 SQL NULL，避免 SQLite 把 None 变成 JSON 文本 "null"。
    preview_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ArtifactCleanupTaskModel(TimestampMixin, Base):
    __tablename__ = "artifact_cleanup_tasks"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id", ondelete="SET NULL"))
    storage_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class DataLinkValidationModel(Base):
    """Durable relation validation task and its safe aggregate report."""

    __tablename__ = "datalink_validations"
    __table_args__ = (
        UniqueConstraint(
            "datasource_id", "idempotency_key", name="uq_datalink_validation_idempotency"
        ),
        Index("ix_datalink_validations_datasource_created", "datasource_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    datasource_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False
    )
    relation_id: Mapped[str] = mapped_column(String(300), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    connection_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False)
    endpoint_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    audit_log_ids_json: Mapped[list[str] | None] = mapped_column(JSON)
    artifact_ids_json: Mapped[list[str] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunDatalinkConsumptionModel(Base):
    """Safe DataLink consumption history for a single Run; never stores raw MCP JSON."""

    __tablename__ = "run_datalink_consumptions"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_datalink_consumption_seq"),
        Index("ix_run_datalink_consumptions_run_seq", "run_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    focus: Mapped[str | None] = mapped_column(String(40))
    max_nodes: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_version: Mapped[str | None] = mapped_column(String(120))
    mode: Mapped[str | None] = mapped_column(String(20))
    payload_status: Mapped[str] = mapped_column(String(40), nullable=False)
    returned_status: Mapped[str] = mapped_column(String(40), nullable=False)
    consumer_receipt_status: Mapped[str] = mapped_column(String(40), nullable=False)
    is_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(120))
    payload_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
