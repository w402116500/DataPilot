from __future__ import annotations

from contracts.datalink import DataLinkExploreRequest
from fastmcp import FastMCP
from pydantic import ValidationError

from server.retrieval.explore import GraphAccessError, GraphExplorer


def create_mcp_server(explorer: GraphExplorer) -> FastMCP:
    """创建只读 MCP 容器，并只注册一个版本化语义探索工具。"""

    mcp = FastMCP(
        name="DataPilot DataLink",
        instructions="Provides versioned semantic data-map exploration for DataPilot agents.",
    )

    @mcp.tool(name="datalink_explore")
    def datalink_explore(
        datasource_id: str,
        graph_version: str,
        query: str,
        focus: str | None = None,
        max_nodes: int = 12,
    ) -> dict[str, object]:
        """按固定版本寻找相关字段、脱敏画像和外键或 Joinable 证据。"""

        try:
            request = DataLinkExploreRequest(
                datasource_id=datasource_id,
                graph_version=graph_version,
                query=query,
                focus=focus,
                max_nodes=max_nodes,
            )
            return explorer.explore(request).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError("INVALID_QUERY: DataLink explore parameters are invalid") from exc
        except GraphAccessError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

    return mcp
