from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from agent_runtime.runtime_limits import AgentRuntimeLimits
from application.runtime_limits import (
    RUNTIME_LIMIT_DEFINITIONS,
    RuntimeLimits,
    runtime_limit_field,
)
from contracts.errors import AppError, ErrorCode
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """主后端配置；从环境变量读取，并保持第一阶段需要的本地运行边界。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    app_env: str = Field(default="development", alias="APP_ENV")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    api_workers: int = Field(default=1, alias="API_WORKERS")
    metadata_database_url: str = Field(alias="METADATA_DATABASE_URL")
    datasource_root: Path = Field(alias="DATASOURCE_ROOT")
    artifact_root: Path = Field(alias="ARTIFACT_ROOT")
    script_workspace_root: Path = Field(
        default=Path("./storage/script-workspaces"),
        alias="SCRIPT_WORKSPACE_ROOT",
    )
    runtime_trace_root: Path = Field(
        default=Path("./storage/runtime-traces"),
        alias="RUNTIME_TRACE_ROOT",
    )
    session_workspace_root: Path = Field(
        default=Path("./storage/session-workspaces"),
        alias="SESSION_WORKSPACE_ROOT",
    )
    sandbox_image: str = Field(default="datapilot-analysis:0.1.0", alias="SANDBOX_IMAGE")
    secret_master_key: str = Field(alias="SECRET_MASTER_KEY", min_length=16)
    datalink_base_url: str = Field(default="http://localhost:8100", alias="DATALINK_BASE_URL")
    datalink_mcp_url: str = Field(default="http://localhost:8100/mcp", alias="DATALINK_MCP_URL")
    datalink_service_token: SecretStr | None = Field(default=None, alias="DATALINK_SERVICE_TOKEN")
    datalink_timeout_seconds: float = Field(
        default=180,
        ge=1,
        le=300,
        alias="DATALINK_TIMEOUT_SECONDS",
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="CORS_ORIGINS")
    default_query_limit: int = Field(default=100, alias="DEFAULT_QUERY_LIMIT")
    max_query_limit: int = Field(default=500, alias="MAX_QUERY_LIMIT")
    query_timeout_seconds: int = Field(default=15, alias="QUERY_TIMEOUT_SECONDS")
    max_upload_mb: int = Field(default=100, alias="MAX_UPLOAD_MB")
    langsmith_tracing: bool | None = Field(default=None, alias="LANGSMITH_TRACING")
    langsmith_endpoint: str | None = Field(default=None, alias="LANGSMITH_ENDPOINT")
    langsmith_api_key: SecretStr | None = Field(default=None, alias="LANGSMITH_API_KEY")
    langsmith_project: str | None = Field(default=None, alias="LANGSMITH_PROJECT")
    analysis_preparation_max_seconds: int = runtime_limit_field("preparation_max_seconds")
    run_opening_timeout_seconds: int = runtime_limit_field("run_opening_timeout_seconds")
    analysis_semantic_context_timeout_seconds: int = runtime_limit_field(
        "semantic_context_timeout_seconds"
    )
    analysis_plan_followup_timeout_seconds: int = runtime_limit_field(
        "analysis_plan_followup_timeout_seconds"
    )
    analysis_final_reserve_seconds: int = runtime_limit_field("final_reserve_seconds")
    analysis_commit_timeout_seconds: int = runtime_limit_field("commit_timeout_seconds")
    analysis_agent_turn_timeout_seconds: int = runtime_limit_field("agent_turn_timeout_seconds")
    final_answer_timeout_seconds: int = runtime_limit_field("final_answer_timeout_seconds")
    final_answer_idle_timeout_seconds: int = runtime_limit_field(
        "final_answer_idle_timeout_seconds"
    )
    agent_model_max_output_tokens: int = runtime_limit_field("model_max_output_tokens")
    model_context_window_tokens: int = runtime_limit_field("context_window_tokens")
    python_sandbox_timeout_seconds: int = runtime_limit_field("sandbox_timeout_seconds")
    agent_max_turns: int = runtime_limit_field("agent_max_turns")
    agent_max_data_tool_calls: int = runtime_limit_field("agent_max_data_tool_calls")
    agent_max_stage_tool_failures: int = runtime_limit_field("agent_max_stage_tool_failures")
    agent_max_commit_validation_failures: int = runtime_limit_field(
        "agent_max_commit_validation_failures"
    )
    agent_max_sql_repairs: int = runtime_limit_field("agent_max_sql_repairs")
    agent_max_discovery_attempts: int = runtime_limit_field("agent_max_discovery_attempts")
    agent_max_model_messages: int = runtime_limit_field("agent_max_model_messages")
    agent_max_model_context_chars: int = runtime_limit_field("agent_max_model_context_chars")
    agent_max_tool_message_chars: int = runtime_limit_field("agent_max_tool_message_chars")
    agent_max_compaction_steps: int = runtime_limit_field("agent_max_compaction_steps")
    agent_max_assertions_per_requirement: int = runtime_limit_field(
        "agent_max_assertions_per_requirement"
    )
    agent_max_total_assertions: int = runtime_limit_field("agent_max_total_assertions")
    agent_max_query_attempts_per_assertion: int = runtime_limit_field(
        "agent_max_query_attempts_per_assertion"
    )
    agent_max_assertion_failures: int = runtime_limit_field("agent_max_assertion_failures")

    @model_validator(mode="before")
    @classmethod
    def _normalize_runtime_limit_overrides(cls, value: object) -> object:
        """把服务级限额覆盖限制在注册表范围内，非法值回退默认值。"""

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for definition in RUNTIME_LIMIT_DEFINITIONS.values():
            raw = normalized.get(definition.env)
            if raw is None:
                raw = normalized.get(definition.env.lower())
            if raw is None:
                continue
            try:
                if isinstance(raw, bool):
                    raise ValueError
                parsed = int(raw)
                if str(raw).strip() != str(parsed):
                    raise ValueError
                if not definition.minimum <= parsed <= definition.maximum:
                    raise ValueError
            except (TypeError, ValueError):
                parsed = definition.default
            normalized[definition.env] = parsed
        return normalized

    @property
    def runtime_limits(self) -> RuntimeLimits:
        """将 Settings 的有界字段投影为一次 Run 可复用的不可变快照。"""

        return RuntimeLimits(
            preparation_max_seconds=self.analysis_preparation_max_seconds,
            run_opening_timeout_seconds=self.run_opening_timeout_seconds,
            semantic_context_timeout_seconds=self.analysis_semantic_context_timeout_seconds,
            analysis_plan_followup_timeout_seconds=self.analysis_plan_followup_timeout_seconds,
            final_reserve_seconds=self.analysis_final_reserve_seconds,
            commit_timeout_seconds=self.analysis_commit_timeout_seconds,
            agent_turn_timeout_seconds=self.analysis_agent_turn_timeout_seconds,
            final_answer_timeout_seconds=self.final_answer_timeout_seconds,
            final_answer_idle_timeout_seconds=self.final_answer_idle_timeout_seconds,
            model_max_output_tokens=self.agent_model_max_output_tokens,
            context_window_tokens=self.model_context_window_tokens,
            sandbox_timeout_seconds=self.python_sandbox_timeout_seconds,
            agent_max_turns=self.agent_max_turns,
            agent_max_data_tool_calls=self.agent_max_data_tool_calls,
            agent_max_stage_tool_failures=self.agent_max_stage_tool_failures,
            agent_max_commit_validation_failures=self.agent_max_commit_validation_failures,
            agent_max_sql_repairs=self.agent_max_sql_repairs,
            agent_max_discovery_attempts=self.agent_max_discovery_attempts,
            agent_max_model_messages=self.agent_max_model_messages,
            agent_max_model_context_chars=self.agent_max_model_context_chars,
            agent_max_tool_message_chars=self.agent_max_tool_message_chars,
            agent_max_compaction_steps=self.agent_max_compaction_steps,
            agent_max_assertions_per_requirement=self.agent_max_assertions_per_requirement,
            agent_max_total_assertions=self.agent_max_total_assertions,
            agent_max_query_attempts_per_assertion=self.agent_max_query_attempts_per_assertion,
            agent_max_assertion_failures=self.agent_max_assertion_failures,
        )

    @property
    def agent_runtime_limits(self) -> AgentRuntimeLimits:
        """将 Agent Graph 预算投影为一次 Run 可复用的不可变快照。"""

        return AgentRuntimeLimits(
            max_turns=self.agent_max_turns,
            max_data_tool_calls=self.agent_max_data_tool_calls,
            max_stage_tool_failures=self.agent_max_stage_tool_failures,
            max_commit_validation_failures=self.agent_max_commit_validation_failures,
            max_sql_repairs=self.agent_max_sql_repairs,
            max_discovery_attempts=self.agent_max_discovery_attempts,
            max_model_messages=self.agent_max_model_messages,
            max_model_context_chars=self.agent_max_model_context_chars,
            max_tool_message_chars=self.agent_max_tool_message_chars,
            max_compaction_steps=self.agent_max_compaction_steps,
            max_assertions_per_requirement=self.agent_max_assertions_per_requirement,
            max_total_assertions=self.agent_max_total_assertions,
            max_query_attempts_per_assertion=self.agent_max_query_attempts_per_assertion,
            max_assertion_failures=self.agent_max_assertion_failures,
        )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if value is None:
            return []
        if isinstance(value, list):
            return value
        raise ValueError("CORS_ORIGINS must be a comma-separated string or list")

    @field_validator("api_workers")
    @classmethod
    def _single_worker(cls, value: int) -> int:
        """固定单 worker，避免后续 Run 取消和 SSE 订阅状态跨进程丢失。"""

        if value != 1:
            raise ValueError("DataPilot Phase 1 requires API_WORKERS=1")
        return value

    def ensure_storage_roots(self) -> None:
        """创建受控存储根目录；具体文件路径校验由后续 DataSource 服务负责。"""

        for root in (
            self.datasource_root,
            self.artifact_root,
            self.script_workspace_root,
            self.runtime_trace_root,
            self.session_workspace_root,
        ):
            root.mkdir(parents=True, exist_ok=True)
            if not root.is_dir():
                raise AppError(
                    ErrorCode.CONFIGURATION_ERROR,
                    "Configured storage root is not a directory",
                    status_code=500,
                    details={"path": str(root)},
                )

    def apply_langsmith_environment(self) -> None:
        """把已解析的 LangSmith 配置注入 LangChain 使用的进程环境。"""

        values = {
            "LANGSMITH_TRACING": (
                None if self.langsmith_tracing is None else str(self.langsmith_tracing).lower()
            ),
            "LANGSMITH_ENDPOINT": self.langsmith_endpoint,
            "LANGSMITH_API_KEY": (
                self.langsmith_api_key.get_secret_value()
                if self.langsmith_api_key is not None
                else None
            ),
            "LANGSMITH_PROJECT": self.langsmith_project,
        }
        for name, value in values.items():
            if value is None:
                continue
            if not value.strip():
                os.environ.pop(name, None)
                continue
            os.environ[name] = value
