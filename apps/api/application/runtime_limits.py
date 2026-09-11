"""主后端运行时限额的单一默认值、边界和环境变量注册表。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.runtime_limits import AGENT_RUNTIME_LIMIT_DEFINITIONS


@dataclass(frozen=True)
class RuntimeLimitDefinition:
    """一个可被服务级环境配置覆盖的有界运行时限额。"""

    default: int
    minimum: int
    maximum: int
    env: str
    description: str


RUNTIME_LIMIT_DEFINITIONS = {
    "preparation_max_seconds": RuntimeLimitDefinition(
        default=240,
        minimum=30,
        maximum=300,
        env="ANALYSIS_PREPARATION_MAX_SECONDS",
        description="准备阶段共享外层窗口",
    ),
    "run_opening_timeout_seconds": RuntimeLimitDefinition(
        default=60,
        minimum=10,
        maximum=300,
        env="RUN_OPENING_TIMEOUT_SECONDS",
        description="Run Opening 模型调用上限",
    ),
    "semantic_context_timeout_seconds": RuntimeLimitDefinition(
        default=30,
        minimum=5,
        maximum=120,
        env="ANALYSIS_SEMANTIC_CONTEXT_TIMEOUT_SECONDS",
        description="DataLink 语义上下文阶段上限",
    ),
    "analysis_plan_followup_timeout_seconds": RuntimeLimitDefinition(
        default=120,
        minimum=30,
        maximum=240,
        env="ANALYSIS_PLAN_FOLLOWUP_TIMEOUT_SECONDS",
        description="分析计划语义定稿模型调用上限",
    ),
    "final_reserve_seconds": RuntimeLimitDefinition(
        default=45,
        minimum=15,
        maximum=120,
        env="ANALYSIS_FINAL_RESERVE_SECONDS",
        description="为 Claim 和最终答案保留的最低时间",
    ),
    "commit_timeout_seconds": RuntimeLimitDefinition(
        default=120,
        minimum=15,
        maximum=120,
        env="ANALYSIS_COMMIT_TIMEOUT_SECONDS",
        description="结论提交模型回合上限",
    ),
    "agent_turn_timeout_seconds": RuntimeLimitDefinition(
        default=120,
        minimum=15,
        maximum=300,
        env="ANALYSIS_AGENT_TURN_TIMEOUT_SECONDS",
        description="普通 Agent 数据分析模型回合上限",
    ),
    "final_answer_timeout_seconds": RuntimeLimitDefinition(
        default=45,
        minimum=15,
        maximum=120,
        env="FINAL_ANSWER_TIMEOUT_SECONDS",
        description="最终答案单次模型调用上限",
    ),
    "final_answer_idle_timeout_seconds": RuntimeLimitDefinition(
        default=30,
        minimum=5,
        maximum=120,
        env="FINAL_ANSWER_IDLE_TIMEOUT_SECONDS",
        description="流式最终答案分片之间的最大空闲时间",
    ),
    "model_max_output_tokens": RuntimeLimitDefinition(
        default=4096,
        minimum=512,
        maximum=32768,
        env="AGENT_MODEL_MAX_OUTPUT_TOKENS",
        description="主 Agent 模型单次输出 token 上限",
    ),
    "context_window_tokens": RuntimeLimitDefinition(
        default=32768,
        minimum=1024,
        maximum=1000000,
        env="MODEL_CONTEXT_WINDOW_TOKENS",
        description="未在 Model Profile 配置时使用的模型上下文窗口",
    ),
    "sandbox_timeout_seconds": RuntimeLimitDefinition(
        default=30,
        minimum=5,
        maximum=300,
        env="PYTHON_SANDBOX_TIMEOUT_SECONDS",
        description="Python 沙盒单次脚本执行上限",
    ),
}

RUNTIME_LIMIT_DEFINITIONS.update(
    {
        name: RuntimeLimitDefinition(**definition)
        for name, definition in AGENT_RUNTIME_LIMIT_DEFINITIONS.items()
    }
)


@dataclass(frozen=True)
class RuntimeLimits:
    """一次服务启动解析出的运行时限额快照。"""

    preparation_max_seconds: int = RUNTIME_LIMIT_DEFINITIONS["preparation_max_seconds"].default
    run_opening_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS[
        "run_opening_timeout_seconds"
    ].default
    semantic_context_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS[
        "semantic_context_timeout_seconds"
    ].default
    analysis_plan_followup_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS[
        "analysis_plan_followup_timeout_seconds"
    ].default
    final_reserve_seconds: int = RUNTIME_LIMIT_DEFINITIONS["final_reserve_seconds"].default
    commit_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS["commit_timeout_seconds"].default
    agent_turn_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_turn_timeout_seconds"
    ].default
    final_answer_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS[
        "final_answer_timeout_seconds"
    ].default
    final_answer_idle_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS[
        "final_answer_idle_timeout_seconds"
    ].default
    model_max_output_tokens: int = RUNTIME_LIMIT_DEFINITIONS["model_max_output_tokens"].default
    context_window_tokens: int = RUNTIME_LIMIT_DEFINITIONS["context_window_tokens"].default
    sandbox_timeout_seconds: int = RUNTIME_LIMIT_DEFINITIONS["sandbox_timeout_seconds"].default
    agent_max_turns: int = RUNTIME_LIMIT_DEFINITIONS["agent_max_turns"].default
    agent_max_data_tool_calls: int = RUNTIME_LIMIT_DEFINITIONS["agent_max_data_tool_calls"].default
    agent_max_stage_tool_failures: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_stage_tool_failures"
    ].default
    agent_max_commit_validation_failures: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_commit_validation_failures"
    ].default
    agent_max_sql_repairs: int = RUNTIME_LIMIT_DEFINITIONS["agent_max_sql_repairs"].default
    agent_max_discovery_attempts: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_discovery_attempts"
    ].default
    agent_max_model_messages: int = RUNTIME_LIMIT_DEFINITIONS["agent_max_model_messages"].default
    agent_max_model_context_chars: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_model_context_chars"
    ].default
    agent_max_tool_message_chars: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_tool_message_chars"
    ].default
    agent_max_compaction_steps: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_compaction_steps"
    ].default
    agent_max_assertions_per_requirement: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_assertions_per_requirement"
    ].default
    agent_max_total_assertions: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_total_assertions"
    ].default
    agent_max_query_attempts_per_assertion: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_query_attempts_per_assertion"
    ].default
    agent_max_assertion_failures: int = RUNTIME_LIMIT_DEFINITIONS[
        "agent_max_assertion_failures"
    ].default


def runtime_limit_field(name: str) -> object:
    """返回供 Pydantic Settings 使用的默认值、范围和环境变量别名。"""

    definition = RUNTIME_LIMIT_DEFINITIONS[name]
    # Field is imported lazily so this registry remains usable by non-settings code and tests.
    from pydantic import Field

    return Field(
        default=definition.default,
        ge=definition.minimum,
        le=definition.maximum,
        alias=definition.env,
        description=definition.description,
    )
