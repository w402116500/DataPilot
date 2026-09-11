"""DataLink 图谱版本、失败保留和重启收尾的存储边界测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from contracts.datalink import DataLinkBuildStatus, DataLinkErrorCode
from fastapi.testclient import TestClient

from server.config import Settings
from server.graph.repository import GraphRepository
from server.graph.storage import GraphStorage
from server.main import create_app


def make_repository(tmp_path: Path) -> GraphRepository:
    """创建独立临时图谱库，避免版本测试使用开发机上的状态。"""

    repository = GraphRepository(GraphStorage(tmp_path / "datalink.db"))
    repository.initialize()
    return repository


def test_graph_schema_has_all_versioned_tables(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    with repository.storage.connection() as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()

    assert {str(row["name"]) for row in rows} >= {
        "graph_builds",
        "datasource_graph_heads",
        "nodes",
        "edges",
        "column_profiles",
        "node_embeddings",
        "pending_edges",
    }


def test_initialize_adds_rebuild_key_to_existing_graph_database(tmp_path: Path) -> None:
    database_path = tmp_path / "datalink.db"
    legacy_build_id = "build_legacy"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE graph_builds (
                id TEXT PRIMARY KEY,
                datasource_id TEXT NOT NULL,
                schema_revision INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                graph_version TEXT NOT NULL UNIQUE,
                error_code TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (datasource_id, schema_revision, attempt_no)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO graph_builds (
                id, datasource_id, schema_revision, attempt_no, status, graph_version,
                error_code, error_message, started_at, finished_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                legacy_build_id,
                "ds_demo",
                1,
                1,
                DataLinkBuildStatus.COMPLETED.value,
                "graph_legacy",
                "2026-08-20T00:00:00+00:00",
                "2026-08-20T00:01:00+00:00",
                "2026-08-20T00:00:00+00:00",
            ),
        )

    repository = GraphRepository(GraphStorage(database_path))
    repository.initialize()

    migrated = repository.get_build(legacy_build_id)
    assert migrated is not None
    assert migrated.rebuild_key == legacy_build_id


def test_claim_creates_a_new_attempt_after_completion_and_blocks_active_build(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)

    created = repository.claim_build("ds_demo", 1, "rebuild_1")
    replay_while_running = repository.claim_build("ds_demo", 1, "rebuild_1")
    different_rebuild = repository.claim_build("ds_demo", 1, "rebuild_2")
    completed = repository.complete_build(created.build.id)
    replay_after_completion = repository.claim_build("ds_demo", 1, "rebuild_1")
    rebuilt = repository.claim_build("ds_demo", 1, "rebuild_2")
    head = repository.get_head("ds_demo")

    assert created.should_execute is True
    assert replay_while_running.disposition == "reused"
    assert replay_while_running.build.id == created.build.id
    assert different_rebuild.disposition == "blocked"
    assert different_rebuild.build.id == created.build.id
    assert replay_after_completion.disposition == "reused"
    assert replay_after_completion.build.id == completed.id
    assert rebuilt.should_execute is True
    assert rebuilt.build.id != completed.id
    assert rebuilt.build.graph_version != completed.graph_version
    assert rebuilt.build.attempt_no == 2
    assert head is not None
    assert head.current_build_id == completed.id


def test_failed_rebuild_keeps_old_head_and_next_attempt_is_new(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    first = repository.claim_build("ds_demo", 1, "rebuild_1").build
    completed_first = repository.complete_build(first.id)

    second = repository.claim_build("ds_demo", 2, "rebuild_2").build
    failed_second = repository.fail_build(second.id, DataLinkErrorCode.BUILD_FAILED, "Build failed")
    retry = repository.claim_build("ds_demo", 2, "rebuild_3").build
    head = repository.get_head("ds_demo")

    assert failed_second.status == DataLinkBuildStatus.FAILED
    assert head is not None
    assert head.current_build_id == completed_first.id
    assert head.current_graph_version == completed_first.graph_version
    assert retry.attempt_no == 2
    assert retry.id != second.id


def test_completed_versions_remain_readable_until_datasource_is_removed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    first = repository.complete_build(repository.claim_build("ds_demo", 1).build.id)
    second = repository.complete_build(repository.claim_build("ds_demo", 2).build.id)

    assert repository.get_completed_build("ds_demo", first.graph_version) == first
    assert repository.get_completed_build("ds_demo", second.graph_version) == second
    assert repository.remove_datasource("ds_demo") is True
    assert repository.remove_datasource("ds_demo") is False
    assert repository.get_head("ds_demo") is None
    assert repository.get_build(first.id) is None


def test_service_start_marks_interrupted_build_failed(tmp_path: Path) -> None:
    database_path = tmp_path / "datalink" / "datalink.db"
    repository = GraphRepository(GraphStorage(database_path))
    repository.initialize()
    interrupted = repository.claim_build("ds_demo", 1).build
    source_root = tmp_path / "datasources"
    source_root.mkdir()

    with TestClient(create_app(Settings(source_root=source_root, database_path=database_path))):
        pass

    recovered = GraphRepository(GraphStorage(database_path)).get_build(interrupted.id)

    assert recovered is not None
    assert recovered.status == DataLinkBuildStatus.FAILED
    assert recovered.error_code == DataLinkErrorCode.BUILD_INTERRUPTED
    assert recovered.graph_version == interrupted.graph_version
    assert GraphRepository(GraphStorage(database_path)).get_head("ds_demo") is None


def test_edge_endpoint_constraint_requires_nodes_from_same_build(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    build = repository.claim_build("ds_demo", 1).build

    with pytest.raises(sqlite3.IntegrityError):
        with repository.storage.transaction() as connection:
            connection.execute(
                """
                INSERT INTO edges (
                    id, datasource_id, build_id, graph_version, source_id, target_id, type,
                    confidence, evidence_json, properties_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "edge:missing",
                    build.datasource_id,
                    build.id,
                    build.graph_version,
                    "column:missing:source",
                    "column:missing:target",
                    "joinable",
                    0.5,
                    None,
                    "{}",
                    build.created_at.isoformat(),
                ),
            )
