"""阶段五后台执行专用的私有 Run 上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.contracts import RunContext
from contracts.runs import ModelRuntimeSnapshot
from data_gateway.types import GatewaySourceSnapshot, SourceAccess


@dataclass(frozen=True)
class RunExecutionContext:
    """请求阶段解析的执行材料，只能在当前进程后台任务中短暂持有。

    ``api_key`` 绝不进入 Run 表、事件、消息、Artifact、GraphState 或 Agent Runtime DTO。
    进程重启后此对象自然消失，遗留 Run 只能失败收尾，不能被恢复执行。
    """

    run_context: RunContext
    model: ModelRuntimeSnapshot
    api_key: str = field(repr=False)
    sandbox_image: str
    input_snapshot_path: str | None
    source_access: SourceAccess | None = field(default=None, repr=False)
    source_snapshot: GatewaySourceSnapshot | None = field(default=None, repr=False)
