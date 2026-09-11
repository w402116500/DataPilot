"""Agent Graph 的可调运行预算及其安全范围。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRuntimeLimits:
    """一次 Run 使用的 Agent Graph 预算快照。

    这些值可以由主后端的 Settings 覆盖，但 Graph 本身只接收快照，
    不直接读取环境变量，避免同一个 Run 中途改变行为。
    """

    max_turns: int = 20
    max_data_tool_calls: int = 16
    max_stage_tool_failures: int = 2
    max_commit_validation_failures: int = 2
    max_sql_repairs: int = 2
    max_discovery_attempts: int = 2
    max_model_messages: int = 32
    max_model_context_chars: int = 64_000
    max_tool_message_chars: int = 32_000
    max_compaction_steps: int = 256
    max_assertions_per_requirement: int = 4
    max_total_assertions: int = 8
    max_query_attempts_per_assertion: int = 3
    max_assertion_failures: int = 2


AGENT_RUNTIME_LIMIT_DEFINITIONS = {
    "agent_max_turns": {
        "default": 20,
        "minimum": 4,
        "maximum": 40,
        "env": "AGENT_MAX_TURNS",
        "description": "单个数据分析 Run 允许的 Agent 最大回合数",
    },
    "agent_max_data_tool_calls": {
        "default": 16,
        "minimum": 1,
        "maximum": 32,
        "env": "AGENT_MAX_DATA_TOOL_CALLS",
        "description": "单个 Run 允许执行的 SQL/Python/DataLink 数据工具调用总数",
    },
    "agent_max_stage_tool_failures": {
        "default": 2,
        "minimum": 1,
        "maximum": 5,
        "env": "AGENT_MAX_STAGE_TOOL_FAILURES",
        "description": "同一阶段连续工具失败后收尾的次数上限",
    },
    "agent_max_commit_validation_failures": {
        "default": 2,
        "minimum": 1,
        "maximum": 3,
        "env": "AGENT_MAX_COMMIT_VALIDATION_FAILURES",
        "description": "结论提交校验失败后的重试次数上限",
    },
    "agent_max_sql_repairs": {
        "default": 2,
        "minimum": 0,
        "maximum": 3,
        "env": "AGENT_MAX_SQL_REPAIRS",
        "description": "普通 SQL 被安全拒绝后允许的不同 SQL 改写次数",
    },
    "agent_max_discovery_attempts": {
        "default": 2,
        "minimum": 1,
        "maximum": 2,
        "env": "AGENT_MAX_DISCOVERY_ATTEMPTS",
        "description": "Discovery 阶段允许的探索尝试次数（初次查询加一次修复）",
    },
    "agent_max_model_messages": {
        "default": 32,
        "minimum": 8,
        "maximum": 64,
        "env": "AGENT_MAX_MODEL_MESSAGES",
        "description": "发送给 Agent 模型的最大工作集消息数",
    },
    "agent_max_model_context_chars": {
        "default": 64_000,
        "minimum": 16_000,
        "maximum": 128_000,
        "env": "AGENT_MAX_MODEL_CONTEXT_CHARS",
        "description": "Agent 工作集的字符预算上限",
    },
    "agent_max_tool_message_chars": {
        "default": 32_000,
        "minimum": 8_000,
        "maximum": 64_000,
        "env": "AGENT_MAX_TOOL_MESSAGE_CHARS",
        "description": "单条工具观察进入模型上下文前的字符上限",
    },
    "agent_max_compaction_steps": {
        "default": 256,
        "minimum": 32,
        "maximum": 512,
        "env": "AGENT_MAX_COMPACTION_STEPS",
        "description": "上下文压缩循环的最大步骤数",
    },
    "agent_max_assertions_per_requirement": {
        "default": 4,
        "minimum": 1,
        "maximum": 16,
        "env": "AGENT_MAX_ASSERTIONS_PER_REQUIREMENT",
        "description": "单个用户目标允许的结构化检查项数量",
    },
    "agent_max_total_assertions": {
        "default": 8,
        "minimum": 1,
        "maximum": 32,
        "env": "AGENT_MAX_TOTAL_ASSERTIONS",
        "description": "单个分析 Run 允许的结构化检查项总数",
    },
    "agent_max_query_attempts_per_assertion": {
        "default": 3,
        "minimum": 1,
        "maximum": 8,
        "env": "AGENT_MAX_QUERY_ATTEMPTS_PER_ASSERTION",
        "description": "单个检查项允许的查询尝试次数",
    },
    "agent_max_assertion_failures": {
        "default": 2,
        "minimum": 1,
        "maximum": 5,
        "env": "AGENT_MAX_ASSERTION_FAILURES",
        "description": "同一检查项重复同类合同错误后的熔断阈值",
    },
}


def default_agent_runtime_limits() -> AgentRuntimeLimits:
    """返回不依赖主后端 Settings 的包级安全默认值。"""

    return AgentRuntimeLimits()
