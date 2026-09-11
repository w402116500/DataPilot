"""DataLink Port 的同版本内存缓存，不持有 MCP Client 或网络配置。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.contracts import (
    AgentErrorCode,
    AgentFailure,
    DataLinkExploreCommand,
    DataLinkExploreResponse,
)
from agent_runtime.ports import CancellationSignal, DataLinkPort


@dataclass(frozen=True)
class DataLinkCacheKey:
    """唯一标识一个固定 DataSource、Schema 和图版本下的检索请求。"""

    datasource_id: str
    schema_revision: int
    graph_version: str
    query: str
    focus: str | None
    max_nodes: int

    @classmethod
    def from_command(cls, command: DataLinkExploreCommand) -> DataLinkCacheKey:
        """只从已验证的业务命令生成缓存键，不接受客户端或地址信息。"""

        return cls(
            datasource_id=command.datasource_id,
            schema_revision=command.schema_revision,
            graph_version=command.graph_version,
            query=command.query,
            focus=command.focus,
            max_nodes=command.max_nodes,
        )


class CachingDataLinkPort:
    """仅在 DataLink 暂不可用时复用完全同键的成功检索结果。"""

    def __init__(self, inner: DataLinkPort) -> None:
        self._inner = inner
        self._cache: dict[DataLinkCacheKey, DataLinkExploreResponse] = {}

    async def explore(
        self,
        request: DataLinkExploreCommand,
        cancellation: CancellationSignal,
    ) -> DataLinkExploreResponse | AgentFailure:
        """成功即覆盖同键缓存；非法或安全错误永远不读取缓存。"""

        if cancellation.is_cancelled():
            return AgentFailure(code=AgentErrorCode.RUN_CANCELED, message="分析已取消")
        result = await self._inner.explore(request, cancellation)
        if cancellation.is_cancelled():
            return AgentFailure(code=AgentErrorCode.RUN_CANCELED, message="分析已取消")
        if isinstance(result, DataLinkExploreResponse):
            if not _matches_fixed_version(request, result):
                return AgentFailure(
                    code=AgentErrorCode.DATALINK_REQUEST_INVALID,
                    message="DataLink 返回版本与当前请求不一致",
                )
            fresh = DataLinkExploreResponse(result=result.result, cache_hit=False)
            self._cache[DataLinkCacheKey.from_command(request)] = fresh
            return fresh
        if result.code is not AgentErrorCode.DATALINK_UNAVAILABLE:
            return result
        cached = self._cache.get(DataLinkCacheKey.from_command(request))
        if cached is None:
            return result
        return DataLinkExploreResponse(result=cached.result, cache_hit=True)


def _matches_fixed_version(
    request: DataLinkExploreCommand,
    response: DataLinkExploreResponse,
) -> bool:
    """缓存前核对关键身份，避免异常上游结果污染同版本缓存。"""

    return (
        response.result.datasource_id == request.datasource_id
        and response.result.graph_version == request.graph_version
        and response.result.query == request.query
    )
