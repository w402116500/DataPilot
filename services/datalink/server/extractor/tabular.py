"""从 CSV 或 SQLite 的受控结构事实生成表、字段和显式外键。"""

from __future__ import annotations

from hashlib import sha256

from contracts.datalink import DataLinkEdgeEvidenceRead, DataLinkEdgeType, DataLinkNodeType

from server.connector.base import DatasourceInfo
from server.models.graph import GraphEdge, GraphNode, GraphPendingEdge, GraphStructure

_MAX_GRAPH_ID_LENGTH = 300


class TabularExtractor:
    """只将 Connector 已确认的表、字段和外键转成可版本化的图谱结构。"""

    def extract(self, datasource: DatasourceInfo) -> GraphStructure:
        """生成 Table、Column、contains 和 foreign_key，不推测任何业务语义。"""

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        pending_edges: list[GraphPendingEdge] = []
        column_ids: dict[tuple[str, str], str] = {}

        for table in datasource.tables:
            table_id = make_graph_id("table", datasource.datasource_id, table.name)
            nodes.append(
                GraphNode(
                    id=table_id,
                    type=DataLinkNodeType.TABLE,
                    name=table.name,
                    properties={"row_count": table.row_count},
                )
            )
            for column in table.columns:
                column_id = make_graph_id(
                    "column", datasource.datasource_id, table.name, column.name
                )
                column_ids[(table.name, column.name)] = column_id
                nodes.append(
                    GraphNode(
                        id=column_id,
                        type=DataLinkNodeType.COLUMN,
                        name=column.name,
                        table_name=table.name,
                        properties={
                            "dtype": column.dtype,
                            "nullable": column.nullable,
                            "is_primary_key": column.is_primary_key,
                        },
                    )
                )
                edges.append(
                    GraphEdge(
                        id=make_graph_id("edge", "contains", table_id, column_id),
                        source_id=table_id,
                        target_id=column_id,
                        type=DataLinkEdgeType.CONTAINS,
                        confidence=1.0,
                    )
                )

        for table in datasource.tables:
            for foreign_key in table.foreign_keys:
                if (
                    foreign_key.column_count > 1
                    or sum(
                        key.constraint_name == foreign_key.constraint_name
                        for key in table.foreign_keys
                    )
                    > 1
                ):
                    # A composite constraint does not prove any individual column join.
                    continue
                source_id = column_ids.get((foreign_key.source_table, foreign_key.source_column))
                target_id = column_ids.get((foreign_key.target_table, foreign_key.target_column))
                if source_id is None:
                    continue
                if target_id is None:
                    target_ref = make_graph_id(
                        "column",
                        datasource.datasource_id,
                        foreign_key.target_table,
                        foreign_key.target_column,
                    )
                    pending_edges.append(
                        GraphPendingEdge(
                            id=make_graph_id("pending_edge", "foreign_key", source_id, target_ref),
                            source_id=source_id,
                            target_ref=target_ref,
                            type=DataLinkEdgeType.FOREIGN_KEY,
                            confidence=1.0,
                            missing_endpoints=("target",),
                            properties={"constraint_name": foreign_key.constraint_name},
                        )
                    )
                    continue
                edges.append(
                    GraphEdge(
                        id=make_graph_id("edge", "foreign_key", source_id, target_id),
                        source_id=source_id,
                        target_id=target_id,
                        type=DataLinkEdgeType.FOREIGN_KEY,
                        confidence=1.0,
                        evidence=DataLinkEdgeEvidenceRead(
                            kind="explicit_foreign_key",
                            summary=(
                                f"{foreign_key.source_table}.{foreign_key.source_column} "
                                f"references {foreign_key.target_table}.{foreign_key.target_column}"
                            ),
                        ),
                        properties={"constraint_name": foreign_key.constraint_name},
                    )
                )

        return GraphStructure(
            nodes=tuple(nodes), edges=tuple(edges), pending_edges=tuple(pending_edges)
        )


def make_graph_id(kind: str, *parts: str) -> str:
    """生成可读且有长度上限的逻辑 ID，避免源字段名撑破对外契约。"""

    raw_id = ":".join((kind, *parts))
    if len(raw_id) <= _MAX_GRAPH_ID_LENGTH:
        return raw_id
    digest = sha256(raw_id.encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{parts[0]}:{digest}"
