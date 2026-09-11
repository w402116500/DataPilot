"""DataLink Build、Graph Head 和版本读取的唯一持久化所有者。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from contracts.datalink import (
    DataLinkBuildStatus,
    DataLinkEdgeEvidenceRead,
    DataLinkEdgeType,
    DataLinkErrorCode,
    DataLinkNodeType,
)
from contracts.ids import make_id
from contracts.sensitive_fields import SensitiveFieldPolicy

from server.graph.storage import GraphStorage
from server.models.graph import GraphEdge, GraphEmbedding, GraphNode, GraphPendingEdge
from server.models.profile import ColumnProfile

_SENSITIVE_FIELD_POLICY = SensitiveFieldPolicy()


@dataclass(frozen=True)
class GraphBuildRecord:
    """一条已持久化的 Build 尝试，不包含数据源路径或原始错误。"""

    id: str
    datasource_id: str
    rebuild_key: str
    schema_revision: int
    attempt_no: int
    status: DataLinkBuildStatus
    graph_version: str
    error_code: DataLinkErrorCode | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime
    origin_kind: str = "automated"
    publication_state: str = "published"
    base_graph_version: str | None = None
    connection_revision: int = 0


@dataclass(frozen=True)
class GraphHeadRecord:
    """某个 DataSource 当前可读图谱版本的唯一指针。"""

    datasource_id: str
    current_build_id: str
    current_graph_version: str
    updated_at: datetime


@dataclass(frozen=True)
class BuildClaim:
    """一次 Build 请求对已有版本的持久化判断结果。"""

    build: GraphBuildRecord
    disposition: Literal["created", "reused", "blocked"]

    @property
    def should_execute(self) -> bool:
        """只有新建的 running Build 才能进入本次后台建图流水线。"""

        return self.disposition == "created"


@dataclass(frozen=True)
class GraphSnapshot:
    """一个已完成版本的内部读取快照，始终限定在单个 Build 内。"""

    build: GraphBuildRecord
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    profiles: tuple[ColumnProfile, ...]


class GraphRepository:
    """集中维护 Build 生命周期与 Graph Head，避免路由或流水线各自改状态。"""

    def __init__(self, storage: GraphStorage) -> None:
        self.storage = storage

    def initialize(self) -> None:
        """初始化独立图谱数据库，供服务启动生命周期调用。"""

        self.storage.initialize()

    def claim_build(
        self,
        datasource_id: str,
        schema_revision: int,
        rebuild_key: str | None = None,
        *,
        connection_revision: int = 0,
        connection: sqlite3.Connection | None = None,
    ) -> BuildClaim:
        """按单次 rebuild key 去重；只有活动 Build 才阻塞新的用户动作。"""

        resolved_rebuild_key = rebuild_key or make_id("rebuild")

        with (
            nullcontext(connection) if connection is not None else self.storage.transaction()
        ) as connection:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(graph_builds)").fetchall()
            }
            if "connection_revision" not in columns:
                connection.execute(
                    "ALTER TABLE graph_builds ADD COLUMN connection_revision INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            existing = self._find_build_by_rebuild_key(
                connection,
                datasource_id=datasource_id,
                rebuild_key=resolved_rebuild_key,
            )
            if existing is not None:
                if (
                    existing.schema_revision != schema_revision
                    or existing.connection_revision != connection_revision
                ):
                    raise ValueError("Rebuild key belongs to another source revision")
                return BuildClaim(build=existing, disposition="reused")

            running = self._find_running_build(connection, datasource_id)
            if running is not None:
                return BuildClaim(build=running, disposition="blocked")

            attempt_no = self._next_attempt_no(connection, datasource_id, schema_revision)
            now = _utc_now()
            build_id = make_id("build")
            graph_version = make_id("graph")
            head = connection.execute(
                "SELECT current_graph_version FROM datasource_graph_heads WHERE datasource_id=?",
                (datasource_id,),
            ).fetchone()
            base_graph_version = head[0] if head else None
            connection.execute(
                """
                INSERT INTO graph_builds (
                    id, datasource_id, rebuild_key, schema_revision, attempt_no, status,
                    graph_version,
                    error_code, error_message, started_at, finished_at, created_at,
                    base_graph_version, connection_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    build_id,
                    datasource_id,
                    resolved_rebuild_key,
                    schema_revision,
                    attempt_no,
                    DataLinkBuildStatus.RUNNING.value,
                    graph_version,
                    now,
                    now,
                    base_graph_version,
                    connection_revision,
                ),
            )
            return BuildClaim(
                build=GraphBuildRecord(
                    id=build_id,
                    datasource_id=datasource_id,
                    rebuild_key=resolved_rebuild_key,
                    schema_revision=schema_revision,
                    connection_revision=connection_revision,
                    attempt_no=attempt_no,
                    status=DataLinkBuildStatus.RUNNING,
                    graph_version=graph_version,
                    error_code=None,
                    error_message=None,
                    started_at=_parse_datetime(now),
                    finished_at=None,
                    created_at=_parse_datetime(now),
                    base_graph_version=base_graph_version,
                ),
                disposition="created",
            )

    def complete_build(self, build_id: str) -> GraphBuildRecord:
        """完成 Build 并在同一事务中切换当前 Graph Head。"""

        with self.storage.transaction() as connection:
            build = self._require_build(connection, build_id)
            self._complete_in_transaction(connection, build)
            return self._require_build(connection, build_id)

    def set_build_metadata(
        self,
        build_id: str,
        *,
        origin_kind: str,
        publication_state: str,
        base_graph_version: str | None,
    ) -> None:
        """在写入快照前固定 Build 来源和基线版本。"""

        with self.storage.transaction() as connection:
            build = self._require_build(connection, build_id)
            if build.status != DataLinkBuildStatus.RUNNING:
                raise ValueError("Completed build metadata is immutable")
            connection.execute(
                "UPDATE graph_builds SET origin_kind = ?, publication_state = ?, "
                "base_graph_version = ? WHERE id = ?",
                (origin_kind, publication_state, base_graph_version, build_id),
            )

    def store_completed_graph(
        self,
        build_id: str,
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        profiles: Sequence[ColumnProfile],
        embeddings: Sequence[GraphEmbedding] = (),
        pending_edges: Sequence[GraphPendingEdge] = (),
        *,
        connection: sqlite3.Connection | None = None,
    ) -> GraphBuildRecord:
        """原子写入一个完整版本并切换 Head，任一校验失败均不留下半张图。"""

        with (
            nullcontext(connection) if connection is not None else self.storage.transaction()
        ) as connection:
            build = self._require_build(connection, build_id)
            if build.status != DataLinkBuildStatus.RUNNING:
                raise ValueError("Only a running Build can store graph artifacts")
            self._validate_artifacts(nodes, edges, profiles, embeddings, pending_edges)
            created_at = _utc_now()
            self._insert_nodes(connection, build, nodes, created_at)
            self._insert_edges(connection, build, edges, created_at)
            self._insert_profiles(connection, build, profiles, created_at)
            self._insert_embeddings(connection, build, embeddings, created_at)
            self._insert_pending_edges(connection, build, pending_edges, created_at)
            self._complete_in_transaction(connection, build)
            return self._require_build(connection, build_id)

    def fail_build(
        self,
        build_id: str,
        error_code: DataLinkErrorCode = DataLinkErrorCode.BUILD_FAILED,
        error_message: str = "Graph build failed",
    ) -> GraphBuildRecord:
        """标记当前 Build 失败，绝不改动此前成功的 Graph Head。"""

        with self.storage.transaction() as connection:
            build = self._require_build(connection, build_id)
            if build.status != DataLinkBuildStatus.RUNNING:
                return build
            connection.execute(
                """
                UPDATE graph_builds
                SET status = ?, error_code = ?, error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    DataLinkBuildStatus.FAILED.value,
                    error_code.value,
                    error_message,
                    _utc_now(),
                    build_id,
                ),
            )
            return self._require_build(connection, build_id)

    def recover_interrupted_builds(self) -> int:
        """在服务启动时收尾遗留运行中 Build，不自动重跑已中断工作。"""

        with self.storage.transaction() as connection:
            result = connection.execute(
                """
                UPDATE graph_builds
                SET status = ?, error_code = ?, error_message = ?, finished_at = ?
                WHERE status = ?
                """,
                (
                    DataLinkBuildStatus.FAILED.value,
                    DataLinkErrorCode.BUILD_INTERRUPTED.value,
                    "Graph build interrupted by service restart",
                    _utc_now(),
                    DataLinkBuildStatus.RUNNING.value,
                ),
            )
            return result.rowcount

    def get_build(self, build_id: str) -> GraphBuildRecord | None:
        """按稳定 Build ID 查询一次构建尝试。"""

        with self.storage.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_builds WHERE id = ?", (build_id,)
            ).fetchone()
        return _build_from_row(row) if row is not None else None

    def get_completed_build(
        self, datasource_id: str, graph_version: str
    ) -> GraphBuildRecord | None:
        """只返回指定 DataSource 的完成版本，拒绝 running 和 failed 图谱。"""

        with self.storage.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM graph_builds
                WHERE datasource_id = ? AND graph_version = ? AND status = ?
                    AND publication_state = 'published'
                """,
                (datasource_id, graph_version, DataLinkBuildStatus.COMPLETED.value),
            ).fetchone()
        return _build_from_row(row) if row is not None else None

    def get_build_by_graph_version(self, graph_version: str) -> GraphBuildRecord | None:
        """按版本定位 Build，供调用方区分版本不存在和数据源不匹配。"""

        with self.storage.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_builds WHERE graph_version = ?", (graph_version,)
            ).fetchone()
        return _build_from_row(row) if row is not None else None

    def get_latest_build(self, datasource_id: str) -> GraphBuildRecord | None:
        """读取最近一次尝试，供状态接口说明失败或完成后的当前状态。"""

        with self.storage.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM graph_builds
                WHERE datasource_id = ?
                ORDER BY created_at DESC, attempt_no DESC
                LIMIT 1
                """,
                (datasource_id,),
            ).fetchone()
        return _build_from_row(row) if row is not None else None

    def get_snapshot(self, build: GraphBuildRecord) -> GraphSnapshot:
        """读取一个已完成 Build 的所有正式图谱内容，不跨版本拼接数据。"""

        if build.status != DataLinkBuildStatus.COMPLETED:
            raise ValueError("Only completed Builds can be read as graph snapshots")
        with self.storage.connection() as connection:
            node_rows = connection.execute(
                "SELECT * FROM nodes WHERE build_id = ? ORDER BY type, name, id", (build.id,)
            ).fetchall()
            edge_rows = connection.execute(
                """
                SELECT * FROM edges WHERE build_id = ?
                ORDER BY type, confidence DESC, source_id, target_id
                """,
                (build.id,),
            ).fetchall()
            profile_rows = connection.execute(
                "SELECT * FROM column_profiles WHERE build_id = ? ORDER BY column_id", (build.id,)
            ).fetchall()
        return GraphSnapshot(
            build=build,
            nodes=tuple(_node_from_row(row) for row in node_rows),
            edges=tuple(_edge_from_row(row) for row in edge_rows),
            profiles=tuple(_profile_from_row(row) for row in profile_rows),
        )

    def get_embeddings(self, build_id: str) -> tuple[GraphEmbedding, ...]:
        """只供服务内检索读取向量；对外 DTO 永远不包含向量内容。"""

        with self.storage.connection() as connection:
            rows = connection.execute(
                """
                SELECT node_id, embedding_model, embedding_vector, searchable_text
                FROM node_embeddings WHERE build_id = ? ORDER BY node_id
                """,
                (build_id,),
            ).fetchall()
        return tuple(
            GraphEmbedding(
                node_id=str(row["node_id"]),
                embedding_model=str(row["embedding_model"]),
                vector=tuple(float(value) for value in json.loads(str(row["embedding_vector"]))),
                searchable_text=str(row["searchable_text"]),
            )
            for row in rows
        )

    def get_head(self, datasource_id: str) -> GraphHeadRecord | None:
        """读取当前完成图谱的唯一 Head，不从 Build 历史猜测最新版本。"""

        with self.storage.connection() as connection:
            row = connection.execute(
                "SELECT * FROM datasource_graph_heads WHERE datasource_id = ?",
                (datasource_id,),
            ).fetchone()
        return _head_from_row(row) if row is not None else None

    def get_pending_edges(self, build_id: str) -> tuple[GraphPendingEdge, ...]:
        with self.storage.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_edges WHERE build_id=? ORDER BY id", (build_id,)
            ).fetchall()
        return tuple(
            GraphPendingEdge(
                id=row["id"],
                source_id=row["source_id"],
                target_ref=row["target_ref"],
                type=DataLinkEdgeType(row["type"]),
                confidence=row["confidence"],
                missing_endpoints=tuple(json.loads(row["missing_endpoints_json"])),
                properties=json.loads(row["properties_json"]),
            )
            for row in rows
        )

    def remove_datasource(self, datasource_id: str) -> bool:
        """幂等删除 DataLink 自己的图谱记录，不触碰主后端数据源文件。"""

        with self.storage.transaction() as connection:
            connection.execute(
                "DELETE FROM datasource_graph_heads WHERE datasource_id = ?", (datasource_id,)
            )
            for table in (
                "semantic_drafts",
                "semantic_change_log",
                "semantic_publish_log",
                "semantic_version_operations",
            ):
                connection.execute(f"DELETE FROM {table} WHERE datasource_id=?", (datasource_id,))
            result = connection.execute(
                "DELETE FROM graph_builds WHERE datasource_id = ?", (datasource_id,)
            )
            return result.rowcount > 0

    @staticmethod
    def _validate_artifacts(
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        profiles: Sequence[ColumnProfile],
        embeddings: Sequence[GraphEmbedding],
        pending_edges: Sequence[GraphPendingEdge],
    ) -> None:
        """在写库前检查端点与画像归属，避免 FK 错误留下难以解释的版本。"""

        node_ids = {node.id for node in nodes}
        if not node_ids or len(node_ids) != len(nodes):
            raise ValueError("Graph nodes must be non-empty and have unique IDs")
        if any(edge.source_id not in node_ids or edge.target_id not in node_ids for edge in edges):
            raise ValueError("Graph edge endpoint is unavailable in this Build")
        if len({(edge.source_id, edge.target_id, edge.type) for edge in edges}) != len(edges):
            raise ValueError("Graph edges must be unique by endpoint and type")
        if any(profile.column_id not in node_ids for profile in profiles):
            raise ValueError("Graph profile column is unavailable in this Build")
        if len({profile.column_id for profile in profiles}) != len(profiles):
            raise ValueError("Graph profiles must have unique columns")
        if any(embedding.node_id not in node_ids for embedding in embeddings):
            raise ValueError("Graph embedding node is unavailable in this Build")
        if any(edge.source_id not in node_ids for edge in pending_edges):
            raise ValueError("Pending edge source is unavailable in this Build")
        if len({edge.id for edge in pending_edges}) != len(pending_edges):
            raise ValueError("Pending edges must have unique IDs")

    @staticmethod
    def _insert_nodes(
        connection: sqlite3.Connection,
        build: GraphBuildRecord,
        nodes: Sequence[GraphNode],
        created_at: str,
    ) -> None:
        """写入节点的有限展示属性，不保存 Connector 路径或原始数据行。"""

        connection.executemany(
            """
            INSERT INTO nodes (
                id, datasource_id, build_id, graph_version, type, name, source_id,
                properties_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    node.id,
                    build.datasource_id,
                    build.id,
                    build.graph_version,
                    node.type.value,
                    node.name,
                    node.table_name,
                    _to_json(_node_properties(node)),
                    created_at,
                )
                for node in nodes
            ],
        )

    @staticmethod
    def _insert_edges(
        connection: sqlite3.Connection,
        build: GraphBuildRecord,
        edges: Sequence[GraphEdge],
        created_at: str,
    ) -> None:
        """写入已经通过端点校验的正式边及其有限证据摘要。"""

        connection.executemany(
            """
            INSERT INTO edges (
                id, datasource_id, build_id, graph_version, source_id, target_id, type,
                confidence, evidence_json, properties_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    edge.id,
                    build.datasource_id,
                    build.id,
                    build.graph_version,
                    edge.source_id,
                    edge.target_id,
                    edge.type.value,
                    edge.confidence,
                    _to_json(edge.evidence.model_dump(mode="json")) if edge.evidence else None,
                    _to_json(dict(edge.properties)),
                    created_at,
                )
                for edge in edges
            ],
        )

    @staticmethod
    def _insert_profiles(
        connection: sqlite3.Connection,
        build: GraphBuildRecord,
        profiles: Sequence[ColumnProfile],
        created_at: str,
    ) -> None:
        """只写已经脱敏的画像读取形状，避免内部字段带入存储。"""

        connection.executemany(
            """
            INSERT INTO column_profiles (
                id, datasource_id, build_id, graph_version, column_id, properties_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    profile.id,
                    build.datasource_id,
                    build.id,
                    build.graph_version,
                    profile.column_id,
                    _to_json(_safe_profile_for_storage(profile)),
                    created_at,
                )
                for profile in profiles
            ],
        )

    @staticmethod
    def _insert_embeddings(
        connection: sqlite3.Connection,
        build: GraphBuildRecord,
        embeddings: Sequence[GraphEmbedding],
        created_at: str,
    ) -> None:
        """写入可选向量供本服务内部召回，不向外部接口传递。"""

        connection.executemany(
            """
            INSERT INTO node_embeddings (
                datasource_id, build_id, graph_version, node_id, embedding_model,
                embedding_vector, searchable_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    build.datasource_id,
                    build.id,
                    build.graph_version,
                    embedding.node_id,
                    embedding.embedding_model,
                    _to_json(list(embedding.vector)),
                    embedding.searchable_text,
                    created_at,
                )
                for embedding in embeddings
            ],
        )

    @staticmethod
    def _insert_pending_edges(
        connection: sqlite3.Connection,
        build: GraphBuildRecord,
        pending_edges: Sequence[GraphPendingEdge],
        created_at: str,
    ) -> None:
        """保存缺端点候选供后续重建复核，不让它进入正式 edges 表。"""

        connection.executemany(
            """
            INSERT INTO pending_edges (
                id, datasource_id, build_id, graph_version, source_id, target_ref, type,
                confidence, missing_endpoints_json, properties_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    edge.id,
                    build.datasource_id,
                    build.id,
                    build.graph_version,
                    edge.source_id,
                    edge.target_ref,
                    edge.type.value,
                    edge.confidence,
                    _to_json(list(edge.missing_endpoints)),
                    _to_json(dict(edge.properties)),
                    created_at,
                )
                for edge in pending_edges
            ],
        )

    @staticmethod
    def _complete_in_transaction(connection: sqlite3.Connection, build: GraphBuildRecord) -> None:
        """在已有写事务中完成 Build 并一次性切换可读版本指针。"""

        if build.status != DataLinkBuildStatus.RUNNING:
            raise ValueError("Only a running Build can be completed")
        finished_at = _utc_now()
        connection.execute(
            """
            UPDATE graph_builds
            SET status = ?, error_code = NULL, error_message = NULL, finished_at = ?
            WHERE id = ?
            """,
            (DataLinkBuildStatus.COMPLETED.value, finished_at, build.id),
        )
        if build.publication_state != "published":
            return
        head = connection.execute(
            "SELECT current_graph_version FROM datasource_graph_heads WHERE datasource_id=?",
            (build.datasource_id,),
        ).fetchone()
        if (head[0] if head else None) != build.base_graph_version:
            raise ValueError("Graph Head changed while this build was running")
        connection.execute(
            """
            INSERT INTO datasource_graph_heads (
                datasource_id, current_graph_version, current_build_id, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(datasource_id) DO UPDATE SET
                current_graph_version = excluded.current_graph_version,
                current_build_id = excluded.current_build_id,
                updated_at = excluded.updated_at
            """,
            (build.datasource_id, build.graph_version, build.id, finished_at),
        )

    @staticmethod
    def _find_build(
        connection: sqlite3.Connection,
        *,
        datasource_id: str,
        schema_revision: int,
        status: DataLinkBuildStatus,
    ) -> GraphBuildRecord | None:
        row = connection.execute(
            """
            SELECT * FROM graph_builds
            WHERE datasource_id = ? AND schema_revision = ? AND status = ?
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (datasource_id, schema_revision, status.value),
        ).fetchone()
        return _build_from_row(row) if row is not None else None

    @staticmethod
    def _find_running_build(
        connection: sqlite3.Connection, datasource_id: str
    ) -> GraphBuildRecord | None:
        row = connection.execute(
            """
            SELECT * FROM graph_builds
            WHERE datasource_id = ? AND status = ?
            ORDER BY started_at ASC
            LIMIT 1
            """,
            (datasource_id, DataLinkBuildStatus.RUNNING.value),
        ).fetchone()
        return _build_from_row(row) if row is not None else None

    @staticmethod
    def _find_build_by_rebuild_key(
        connection: sqlite3.Connection,
        *,
        datasource_id: str,
        rebuild_key: str,
    ) -> GraphBuildRecord | None:
        row = connection.execute(
            """
            SELECT * FROM graph_builds
            WHERE datasource_id = ? AND rebuild_key = ?
            LIMIT 1
            """,
            (datasource_id, rebuild_key),
        ).fetchone()
        return _build_from_row(row) if row is not None else None

    @staticmethod
    def _next_attempt_no(
        connection: sqlite3.Connection, datasource_id: str, schema_revision: int
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_attempt_no
            FROM graph_builds
            WHERE datasource_id = ? AND schema_revision = ?
            """,
            (datasource_id, schema_revision),
        ).fetchone()
        return int(row["next_attempt_no"])

    @staticmethod
    def _require_build(connection: sqlite3.Connection, build_id: str) -> GraphBuildRecord:
        row = connection.execute("SELECT * FROM graph_builds WHERE id = ?", (build_id,)).fetchone()
        if row is None:
            raise ValueError("Graph Build does not exist")
        return _build_from_row(row)


def _utc_now() -> str:
    """统一保存 UTC ISO 时间，避免图谱版本时间依赖本机时区。"""

    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    """把本服务写入的 ISO 时间恢复为带时区的 datetime。"""

    return datetime.fromisoformat(value)


def _build_from_row(row: sqlite3.Row) -> GraphBuildRecord:
    """将 SQLite 的 Build 记录转换为受限内部模型。"""

    error_code = row["error_code"]
    return GraphBuildRecord(
        id=str(row["id"]),
        datasource_id=str(row["datasource_id"]),
        rebuild_key=str(row["rebuild_key"]),
        schema_revision=int(row["schema_revision"]),
        connection_revision=int(row["connection_revision"]),
        attempt_no=int(row["attempt_no"]),
        status=DataLinkBuildStatus(str(row["status"])),
        graph_version=str(row["graph_version"]),
        error_code=DataLinkErrorCode(str(error_code)) if error_code is not None else None,
        error_message=str(row["error_message"]) if row["error_message"] is not None else None,
        started_at=_parse_datetime(str(row["started_at"])),
        finished_at=_parse_datetime(str(row["finished_at"])) if row["finished_at"] else None,
        created_at=_parse_datetime(str(row["created_at"])),
        origin_kind=str(row["origin_kind"]),
        publication_state=str(row["publication_state"]),
        base_graph_version=row["base_graph_version"],
    )


def _head_from_row(row: sqlite3.Row) -> GraphHeadRecord:
    """将当前版本指针转换为内部模型。"""

    return GraphHeadRecord(
        datasource_id=str(row["datasource_id"]),
        current_build_id=str(row["current_build_id"]),
        current_graph_version=str(row["current_graph_version"]),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _to_json(value: object) -> str:
    """集中使用紧凑 JSON 写入图谱 SQLite，保证日期等契约值稳定可读。"""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _node_properties(node: GraphNode) -> dict[str, object]:
    """合并节点的展示元数据与规则属性，不新增源文件位置等敏感属性。"""

    return {
        **node.properties,
        "table_name": node.table_name,
        "semantic_type": node.semantic_type,
        "description": node.description,
        "aliases": list(node.aliases),
    }


def _node_from_row(row: sqlite3.Row) -> GraphNode:
    """从版本化 SQLite 行恢复内部节点模型。"""

    properties = json.loads(str(row["properties_json"]))
    return GraphNode(
        id=str(row["id"]),
        type=DataLinkNodeType(str(row["type"])),
        name=str(row["name"]),
        table_name=_optional_string(properties.get("table_name")),
        semantic_type=_optional_string(properties.get("semantic_type")),
        description=_optional_string(properties.get("description")),
        aliases=tuple(str(alias) for alias in properties.get("aliases", [])),
        properties={
            key: value
            for key, value in properties.items()
            if key not in {"table_name", "semantic_type", "description", "aliases"}
        },
    )


def _edge_from_row(row: sqlite3.Row) -> GraphEdge:
    """从版本化 SQLite 行恢复内部边及其公开证据摘要。"""

    evidence_json = row["evidence_json"]
    return GraphEdge(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        target_id=str(row["target_id"]),
        type=DataLinkEdgeType(str(row["type"])),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        evidence=(
            DataLinkEdgeEvidenceRead.model_validate(json.loads(str(evidence_json)))
            if evidence_json is not None
            else None
        ),
        properties=json.loads(str(row["properties_json"])),
    )


def _profile_from_row(row: sqlite3.Row) -> ColumnProfile:
    """从已脱敏的画像 JSON 恢复内部画像，默认不信任它含有真实样例。"""

    properties = json.loads(str(row["properties_json"]))
    return ColumnProfile(
        id=str(row["id"]),
        column_id=str(row["column_id"]),
        column_name="",
        dtype=str(properties["dtype"]),
        null_rate=float(properties["null_rate"]),
        distinct_count=int(properties["distinct_count"]),
        unique_rate=float(properties["unique_rate"]),
        top_values=tuple(properties.get("top_values", [])),
        sample_values=tuple(properties.get("sample_values", [])),
        min_value=properties.get("min_value"),
        max_value=properties.get("max_value"),
        semantic_type=_optional_string(properties.get("semantic_type")),
    )


def _optional_string(value: object) -> str | None:
    """把 JSON 中缺失或空展示字段统一还原为 None。"""

    return str(value) if value is not None else None


def _safe_profile_for_storage(profile: ColumnProfile) -> dict[str, object]:
    """写库前以共享规则复核敏感字段，避免上游漏标将真实值带入历史版本。"""

    value = profile.to_read().model_dump(mode="json")
    if profile.is_sensitive or _SENSITIVE_FIELD_POLICY.is_sensitive_field(profile.column_name):
        value.update({"top_values": [], "sample_values": [], "min_value": None, "max_value": None})
    return value
