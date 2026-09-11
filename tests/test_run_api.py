from __future__ import annotations

import asyncio
import hashlib

from contracts.run_events import RunEventCreate, RunEventType
from contracts.status import DataSourceStatus, MessageRole
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import (
    ArtifactModel,
    DataSourceModel,
    SessionModel,
    SqlAuditLogModel,
    ToolCallModel,
)
from metadata.repositories import MessageRepository, RunRepository
from runtime.run_event_pipeline import RunEventPipeline
from runtime.run_history_service import MAX_INLINE_FILE_BYTES


async def _seed_history(settings) -> None:
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(
                DataSourceModel(
                    id="datasource_api",
                    name="API",
                    description=None,
                    type="csv",
                    source_ref="datasource_api/source.csv",
                    file_size=1,
                    content_hash="hash",
                    schema_cache_json={"datasource_id": "datasource_api", "tables": []},
                    schema_revision=1,
                    status=DataSourceStatus.READY.value,
                )
            )
            db.add(
                SessionModel(
                    id="session_api",
                    title="API",
                    selected_datasource_id="datasource_api",
                )
            )
            await db.flush()
            message = await MessageRepository(db).create(
                session_id="session_api",
                role=MessageRole.USER,
                content_text="问题",
            )
            run = await RunRepository(db).create(
                run_id="run_api",
                session_id="session_api",
                datasource_id="datasource_api",
                user_message_id=message.id,
                question="问题",
                idempotency_key="api_key",
                model_profile_id=None,
                model_provider=None,
                model_name=None,
                schema_revision=1,
                datalink_graph_version=None,
                run_timeout_seconds=60,
                input_snapshot_ref="snapshots/run_api/source.csv",
                final_output_mode="json_schema",
                model_capability_fingerprint="a" * 64,
            )
            db.add(
                SqlAuditLogModel(
                    id="audit_api",
                    run_id=run.id,
                    tool_call_id="tool_api",
                    datasource_id="datasource_api",
                    schema_revision=1,
                    attempt_no=0,
                    original_sql="SELECT value FROM data",
                    normalized_sql="SELECT value FROM data LIMIT 500",
                    status="succeeded",
                    statement_type="SELECT",
                    referenced_tables_json=["data"],
                    row_count=1,
                    elapsed_ms=2,
                )
            )
            db.add(
                SqlAuditLogModel(
                    id="audit_blocked_api",
                    run_id=run.id,
                    tool_call_id="tool_api",
                    datasource_id="datasource_api",
                    schema_revision=1,
                    attempt_no=1,
                    repaired_from_id="audit_api",
                    original_sql="DROP TABLE data",
                    status="blocked",
                    blocked_reason_code="SQL_STATEMENT_NOT_ALLOWED",
                    blocked_reason="只允许只读 SELECT 查询",
                )
            )
            content = b'{"columns":["value"],"rows":[[1]]}'
            db.add(
                ArtifactModel(
                    id="artifact_api",
                    run_id=run.id,
                    session_id="session_api",
                    tool_call_id="tool_api",
                    type="table",
                    title="查询结果",
                    storage_ref=None,
                    mime_type="application/json",
                    size_bytes=len(content),
                    preview_json={"columns": ["value"], "rows": [[1]]},
                    metadata_json={"full_result": False},
                    content_hash=hashlib.sha256(content).hexdigest(),
                )
            )
            pipeline = RunEventPipeline(db)
            async with pipeline.transaction(run.id):
                await pipeline.append(RunEventCreate(run_id=run.id, type=RunEventType.RUN_QUEUED))
                await pipeline.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.TOOL_CALLED,
                        payload={
                            "tool_call_id": "tool_api",
                            "tool_name": "run_sql_readonly",
                            "turn_no": 1,
                        },
                        tool_input={
                            "sql": "SELECT value FROM data",
                            "runtime": {"secret": "must-not-persist"},
                        },
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.TOOL_SUCCEEDED,
                        payload={
                            "tool_call_id": "tool_api",
                            "tool_name": "run_sql_readonly",
                            "turn_no": 1,
                            "elapsed_ms": 2,
                            "evidence_count": 2,
                            "output_summary_json": "{}",
                        },
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.TOOL_CALLED,
                        payload={
                            "tool_call_id": "tool_commit_api",
                            "tool_name": "commit_analysis_claims",
                            "turn_no": 1,
                        },
                        tool_input={
                            "claims": [
                                {
                                    "requirement_id": "R1",
                                    "claim": "敏感正文不得进入事件账本",
                                    "evidence_binding_ids": ["E1"],
                                }
                            ]
                        },
                    )
                )
                await pipeline.append(
                    RunEventCreate(
                        run_id=run.id,
                        type=RunEventType.TOOL_SUCCEEDED,
                        payload={
                            "tool_call_id": "tool_commit_api",
                            "tool_name": "commit_analysis_claims",
                            "turn_no": 1,
                            "elapsed_ms": 3,
                            "evidence_count": 1,
                            "output_summary_json": '{"claim_count":1,"evidence_binding_count":1}',
                        },
                    )
                )
                db.add_all(
                    [
                        ToolCallModel(
                            id="tool_python_api",
                            run_id=run.id,
                            tool_call_id="tool_python_api",
                            tool_name="run_python",
                            input_json={
                                "script": "print('ok')",
                                "output_paths": ["outputs/result.csv"],
                                "purpose": "生成汇总",
                                "container": {"network": "host"},
                            },
                        ),
                        ToolCallModel(
                            id="tool_datalink_api",
                            run_id=run.id,
                            tool_call_id="tool_datalink_api",
                            tool_name="explore_datalink",
                            input_json={
                                "query": "sales 与 customers 的关系",
                                "focus": None,
                                "max_nodes": 8,
                                "credentials": ["must-not-persist"],
                            },
                        ),
                        ToolCallModel(
                            id="tool_unknown_api",
                            run_id=run.id,
                            tool_call_id="tool_unknown_api",
                            tool_name="unknown_tool",
                            input_json={"token": "must-not-persist"},
                        ),
                        ToolCallModel(
                            id="tool_invalid_python_api",
                            run_id=run.id,
                            tool_call_id="tool_invalid_python_api",
                            tool_name="run_python",
                            input_json={
                                "script": "print('bad')",
                                "output_paths": ["outputs/result.csv", 7],
                                "purpose": "错误类型",
                            },
                        ),
                        ToolCallModel(
                            id="tool_invalid_sql_api",
                            run_id=run.id,
                            tool_call_id="tool_invalid_sql_api",
                            tool_name="run_sql_readonly",
                            input_json={"sql": {"raw": "SELECT secret FROM data"}},
                        ),
                    ]
                )
                await pipeline.append(
                    RunEventCreate(run_id=run.id, type=RunEventType.RUN_SUCCEEDED)
                )
                await db.commit()
            run.status = "succeeded"
            await db.commit()
    finally:
        await engine.dispose()


def _data(response):
    body = response.json()
    assert body["error"] is None
    return body["data"]


def test_history_endpoints_replay_tools_and_artifacts_only(client, migrated_settings) -> None:
    asyncio.run(_seed_history(migrated_settings))

    run = _data(client.get("/runs/run_api"))
    assert run["status"] == "succeeded"
    assert isinstance(run["user_message_id"], str)

    tools = _data(client.get("/runs/run_api/tool-calls"))
    tools_by_id = {tool["id"]: tool for tool in tools}
    assert tools_by_id["tool_api"]["input_params"] == {"sql": "SELECT value FROM data"}
    assert tools_by_id["tool_python_api"]["input_params"] == {
        "script": "print('ok')",
        "output_paths": ["outputs/result.csv"],
        "purpose": "生成汇总",
    }
    assert tools_by_id["tool_datalink_api"]["input_params"] == {
        "query": "sales 与 customers 的关系",
        "focus": None,
        "max_nodes": 8,
    }
    assert tools_by_id["tool_commit_api"]["input_params"] == {
        "claim_count": 1,
        "requirement_ids": ["R1"],
        "evidence_binding_ids": ["E1"],
    }
    assert tools_by_id["tool_unknown_api"]["input_params"] is None
    assert tools_by_id["tool_invalid_python_api"]["input_params"] is None
    assert tools_by_id["tool_invalid_sql_api"]["input_params"] is None
    assert all("input_json" not in tool for tool in tools)

    audits = _data(client.get("/runs/run_api/sql-audits"))
    audits_by_id = {audit["id"]: audit for audit in audits}
    assert audits_by_id["audit_api"]["referenced_tables"] == ["data"]
    assert audits_by_id["audit_api"]["original_sql"] == "SELECT value FROM data"
    assert audits_by_id["audit_api"]["normalized_sql"] == "SELECT value FROM data LIMIT 500"
    blocked_audit = audits_by_id["audit_blocked_api"]
    assert blocked_audit["blocked_reason_code"] == "SQL_STATEMENT_NOT_ALLOWED"
    assert blocked_audit["blocked_reason"] == "只允许只读 SELECT 查询"
    assert blocked_audit["repaired_from_id"] == "audit_api"

    artifacts = _data(client.get("/runs/run_api/artifacts"))
    assert artifacts[0]["id"] == "artifact_api"
    assert "storage_ref" not in artifacts[0]

    replay = _data(client.get("/runs/run_api/events/history?after_seq=1"))
    assert [event["seq"] for event in replay] == [2, 3, 4, 5, 6]
    assert replay[-1]["type"] == "run.succeeded"
    assert "SELECT value FROM data" not in str(replay)
    assert "敏感正文不得进入事件账本" not in str(replay)
    assert any(
        event["type"] == "tool.succeeded"
        and event["payload"].get("tool_call_id") == "tool_commit_api"
        for event in replay
    )
    assert all("tool_input" not in event["payload"] for event in replay)

    dag = _data(client.get("/runs/run_api/trace-dag"))
    assert dag["run_id"] == "run_api"
    assert {node["kind"] for node in dag["nodes"]} >= {
        "run-start",
        "tool",
        "artifact",
        "run-terminal",
    }
    assert all("storage_ref" not in node.get("detail", {}) for node in dag["nodes"])
    assert "敏感正文不得进入事件账本" not in str(dag)


async def _seed_file_artifacts(settings) -> dict[str, bytes]:
    payloads = {
        "artifact_json_file": b'{"items":[1,2]}',
        "artifact_txt_file": "分析完成\n第二行".encode(),
        "artifact_csv_file": b'name,note\r\n"Ada, A.","line 1\nline 2"\r\n',
        "artifact_tsv_file": b"name\tamount\nAda\t10\n",
        "artifact_xlsx_file": b"PK\x03\x04xlsx",
        "artifact_binary_file": b"\x00\x01\x02",
        "artifact_oversized_file": b"x" * (MAX_INLINE_FILE_BYTES + 1),
        "artifact_actual_oversized": b"y" * (MAX_INLINE_FILE_BYTES + 1),
        "artifact_hash_mismatch": b"hash mismatch",
        "artifact_size_mismatch": b"size mismatch",
        "artifact_invalid_utf8": b"\xff\xfe",
    }
    definitions = {
        "artifact_json_file": ("application/json", ".json"),
        "artifact_txt_file": ("text/plain", ".txt"),
        "artifact_csv_file": ("text/csv", ".csv"),
        "artifact_tsv_file": ("text/tab-separated-values", ".tsv"),
        "artifact_xlsx_file": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
        "artifact_binary_file": ("application/octet-stream", ".bin"),
        "artifact_oversized_file": ("text/plain", ".txt"),
        "artifact_actual_oversized": ("text/plain", ".txt"),
        "artifact_hash_mismatch": ("text/plain", ".txt"),
        "artifact_size_mismatch": ("text/plain", ".txt"),
        "artifact_invalid_utf8": ("text/plain", ".txt"),
    }
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            for artifact_id, content in payloads.items():
                mime_type, suffix = definitions[artifact_id]
                storage_ref = f"runs/run_api/{artifact_id}{suffix}"
                target = settings.artifact_root / storage_ref
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                content_hash = hashlib.sha256(content).hexdigest()
                size_bytes = len(content)
                if artifact_id == "artifact_hash_mismatch":
                    content_hash = hashlib.sha256(b"different").hexdigest()
                if artifact_id == "artifact_size_mismatch":
                    size_bytes += 1
                if artifact_id == "artifact_actual_oversized":
                    size_bytes = 16
                db.add(
                    ArtifactModel(
                        id=artifact_id,
                        run_id="run_api",
                        session_id="session_api",
                        tool_call_id="tool_api",
                        type="file",
                        title=artifact_id,
                        storage_ref=storage_ref,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        preview_json=None,
                        metadata_json={"source": "python_output"},
                        content_hash=content_hash,
                    )
                )
            db.add(
                ArtifactModel(
                    id="artifact_unstored_file",
                    run_id="run_api",
                    session_id="session_api",
                    tool_call_id="tool_api",
                    type="file",
                    title="unstored",
                    storage_ref=None,
                    mime_type="text/plain",
                    size_bytes=0,
                    preview_json={"text": "not a registered file"},
                    metadata_json={"source": "python_output"},
                    content_hash=hashlib.sha256(b"not a registered file").hexdigest(),
                )
            )
            await db.commit()
    finally:
        await engine.dispose()
    return payloads


def test_file_artifact_content_is_allowlisted_sized_and_integrity_checked(
    client,
    migrated_settings,
) -> None:
    asyncio.run(_seed_history(migrated_settings))
    payloads = asyncio.run(_seed_file_artifacts(migrated_settings))

    artifacts = {
        artifact["id"]: artifact for artifact in _data(client.get("/runs/run_api/artifacts"))
    }
    for artifact_id in (
        "artifact_json_file",
        "artifact_txt_file",
        "artifact_csv_file",
        "artifact_tsv_file",
    ):
        assert artifacts[artifact_id]["inline_previewable"] is True
        response = client.get(f"/artifacts/{artifact_id}/content")
        assert response.status_code == 200
        assert response.content == payloads[artifact_id]
        assert response.headers["x-content-type-options"] == "nosniff"

    for artifact_id in ("artifact_xlsx_file", "artifact_binary_file"):
        assert artifacts[artifact_id]["inline_previewable"] is False
        assert client.get(f"/artifacts/{artifact_id}/content").status_code == 404

    assert artifacts["artifact_unstored_file"]["inline_previewable"] is False
    assert client.get("/artifacts/artifact_unstored_file/content").status_code == 404

    assert artifacts["artifact_oversized_file"]["inline_previewable"] is False
    oversized = client.get("/artifacts/artifact_oversized_file/content")
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert artifacts["artifact_actual_oversized"]["inline_previewable"] is True
    for artifact_id in (
        "artifact_actual_oversized",
        "artifact_hash_mismatch",
        "artifact_size_mismatch",
        "artifact_invalid_utf8",
    ):
        assert client.get(f"/artifacts/{artifact_id}/content").status_code == 404

    download = client.get("/artifacts/artifact_xlsx_file/download")
    assert download.status_code == 200
    assert download.content == payloads["artifact_xlsx_file"]
    assert download.headers["content-disposition"].endswith('artifact_xlsx_file.xlsx"')


def test_sse_replays_after_seq_and_closes_on_terminal_event(client, migrated_settings) -> None:
    asyncio.run(_seed_history(migrated_settings))

    with client.stream("GET", "/runs/run_api/events?after_seq=1") as response:
        assert response.status_code == 200
        payload = "".join(response.iter_text())

    assert "id: 2" in payload
    assert "id: 4" in payload
    assert "event: run.succeeded" in payload
    assert "id: 1" not in payload
    assert "SELECT value FROM data" not in payload
    assert "tool_input" not in payload


async def _seed_message_history(settings) -> None:
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(SessionModel(id="session_messages", title="Messages"))
            await db.flush()
            await MessageRepository(db).create(
                session_id="session_messages",
                role=MessageRole.ASSISTANT,
                content_text="正式回答",
                answer_evidence_refs=["audit_1", "artifact_1"],
            )
            await db.commit()
    finally:
        await engine.dispose()


def test_message_history_returns_markdown_and_answer_evidence(client, migrated_settings) -> None:
    asyncio.run(_seed_message_history(migrated_settings))

    response = _data(client.get("/sessions/session_messages/messages"))
    assert response["items"][0]["answer_evidence_refs"] == ["audit_1", "artifact_1"]
    assert response["items"][0]["content_text"] == "正式回答"
    assert "answer_sections" not in response["items"][0]
    assert "artifact_refs" not in response["items"][0]


async def _seed_message_window(settings, *, count: int) -> None:
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(SessionModel(id="session_window", title="Window"))
            await db.flush()
            messages = MessageRepository(db)
            for index in range(count):
                await messages.create(
                    session_id="session_window",
                    role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                    content_text=f"消息{index + 1}",
                )
            await db.commit()
    finally:
        await engine.dispose()


def test_message_history_returns_latest_window_without_repeating_first(
    client, migrated_settings
) -> None:
    asyncio.run(_seed_message_window(migrated_settings, count=25))

    latest = _data(client.get("/sessions/session_window/messages"))
    assert latest["total"] == 25
    assert latest["page"] == 1
    assert latest["page_size"] == 20
    assert [item["position"] for item in latest["items"]] == list(range(6, 26))

    earlier = _data(
        client.get("/sessions/session_window/messages", params={"before_position": 6})
    )
    assert [item["position"] for item in earlier["items"]] == list(range(1, 6))
    assert earlier["total"] == 25


async def _seed_run_questions(settings) -> None:
    engine = create_sqlite_engine(settings.metadata_database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            db.add(
                DataSourceModel(
                    id="datasource_search",
                    name="Search",
                    description=None,
                    type="csv",
                    source_ref="datasource_search/source.csv",
                    file_size=1,
                    content_hash="hash",
                    schema_cache_json={"tables": []},
                    schema_revision=1,
                    status=DataSourceStatus.READY.value,
                )
            )
            db.add(SessionModel(id="session_search", title="Search"))
            await db.flush()
            messages = MessageRepository(db)
            runs = RunRepository(db)
            first = await messages.create(
                session_id="session_search",
                role=MessageRole.USER,
                content_text="同比是什么",
            )
            second = await messages.create(
                session_id="session_search",
                role=MessageRole.USER,
                content_text="100%完成率",
            )
            first_run = await runs.create(
                run_id="run_search_yoy",
                session_id="session_search",
                datasource_id="datasource_search",
                user_message_id=first.id,
                question="同比是什么",
                idempotency_key="search_1",
                model_profile_id=None,
                model_provider=None,
                model_name=None,
                schema_revision=1,
                datalink_graph_version=None,
                run_timeout_seconds=60,
                input_snapshot_ref="snapshots/run_search_yoy/source.csv",
                final_output_mode="json_schema",
                model_capability_fingerprint="a" * 64,
            )
            first_run.status = "succeeded"
            await db.flush()
            await runs.create(
                run_id="run_search_pct",
                session_id="session_search",
                datasource_id="datasource_search",
                user_message_id=second.id,
                question="100%完成率",
                idempotency_key="search_2",
                model_profile_id=None,
                model_provider=None,
                model_name=None,
                schema_revision=1,
                datalink_graph_version=None,
                run_timeout_seconds=60,
                input_snapshot_ref="snapshots/run_search_pct/source.csv",
                final_output_mode="json_schema",
                model_capability_fingerprint="a" * 64,
            )
            await db.commit()
    finally:
        await engine.dispose()


def test_session_run_list_filters_question_and_escapes_like(client, migrated_settings) -> None:
    asyncio.run(_seed_run_questions(migrated_settings))

    matched = _data(client.get("/sessions/session_search/runs", params={"q": "同比"}))
    assert matched["total"] == 1
    assert matched["items"][0]["question"] == "同比是什么"

    wildcard = _data(client.get("/sessions/session_search/runs", params={"q": "%"}))
    assert wildcard["total"] == 1
    assert wildcard["items"][0]["question"] == "100%完成率"
