from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from contracts import (
    DataLinkBuildStatus,
    DataLinkExploreRequest,
    DataLinkExploreResult,
    DataLinkRebuildRequest,
    DataLinkRebuildResult,
    RunEventRead,
    RunEventType,
)
from contracts.sensitive_fields import SensitiveFieldPolicy
from pydantic import ValidationError
from server.app import create_app
from server.config import Settings
from server.responses import encode_utc_datetime
from sqlalchemy import create_engine, inspect

from tests.conftest import run_migrations, sync_sqlite_url


def unwrap(response):
    body = response.json()
    assert body["request_id"]
    assert body["error"] is None
    return body["data"]


def test_migration_creates_foundation_tables(settings: Settings) -> None:
    run_migrations(settings.metadata_database_url)
    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    tables = set(inspect(engine).get_table_names())

    assert {
        "data_sources",
        "model_profiles",
        "secrets",
        "sessions",
        "messages",
        "runs",
        "run_events",
        "tool_calls",
        "sql_audit_logs",
        "artifacts",
        "artifact_cleanup_tasks",
        "session_preferences",
        "conversation_context_states",
        "historical_answer_summaries",
    }.issubset(tables)
    assert "conversation_summaries" not in tables
    assert "long_term_memories" not in tables
    message_columns = {column["name"] for column in inspect(engine).get_columns("messages")}
    assert "answer_sections_json" in message_columns
    assert "answer_evidence_refs_json" in message_columns
    assert "artifact_refs_json" not in message_columns


def test_upgrade_permanently_drops_existing_long_term_memories(settings: Settings) -> None:
    """旧库升级后，跨会话记忆表及其历史记录都必须被物理删除。"""

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_sqlite_url(settings.metadata_database_url))
    command.upgrade(config, "0011_message_answer_evidence_refs")

    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO long_term_memories "
                "(id, scope, memory_type, content_text, confidence, created_at) "
                "VALUES ('memory_legacy', 'user', 'datasource_finding', '旧会话结论', 0.8, "
                "CURRENT_TIMESTAMP)"
            )
            assert (
                connection.exec_driver_sql("SELECT COUNT(*) FROM long_term_memories").scalar() == 1
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    try:
        assert "long_term_memories" not in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_existing_database_upgrade_adds_run_secret_snapshot_foreign_key(settings: Settings) -> None:
    """已升级到 0003 的旧库继续升级时，Run 密钥快照仍会随密钥删除置空。"""

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_sqlite_url(settings.metadata_database_url))
    command.upgrade(config, "0003_phase_four_model_profile_runtime")
    command.upgrade(config, "head")

    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    inspector = inspect(engine)
    assert "model_secret_ref" in {column["name"] for column in inspector.get_columns("runs")}
    secret_foreign_keys = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys("runs")
        if foreign_key["constrained_columns"] == ["model_secret_ref"]
    ]
    assert secret_foreign_keys == [
        {
            "name": "fk_runs_model_secret_ref_secrets",
            "constrained_columns": ["model_secret_ref"],
            "referred_schema": None,
            "referred_table": "secrets",
            "referred_columns": ["id"],
            "options": {"ondelete": "SET NULL"},
        }
    ]

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "INSERT INTO sessions (id, title, created_at, updated_at) "
            "VALUES ('session_1', '迁移测试', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO secrets (id, encrypted_value, created_at, updated_at) "
            "VALUES ('secret_1', 'encrypted', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO runs (id, session_id, question, model_secret_ref, created_at, updated_at) "
            "VALUES ('run_1', 'session_1', '迁移测试', 'secret_1', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql("DELETE FROM secrets WHERE id = 'secret_1'")
        model_secret_ref = connection.exec_driver_sql(
            "SELECT model_secret_ref FROM runs WHERE id = 'run_1'"
        ).scalar_one()

    assert model_secret_ref is None


def test_metadata_datetime_json_uses_utc_z_suffix() -> None:
    naive = datetime(2026, 9, 8, 13, 21, 9, 779821)
    aware = datetime(2026, 9, 8, 13, 21, 9, 779821, tzinfo=UTC)
    assert encode_utc_datetime(naive) == "2026-09-08T13:21:09.779821Z"
    assert encode_utc_datetime(aware) == "2026-09-08T13:21:09.779821Z"


def test_health_uses_root_path_and_reports_current_datalink_state(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["datalink"] == "ok"


def test_session_api_lifecycle(client) -> None:
    created = client.post(
        "/sessions",
        json={"title": "GMV analysis", "selected_datasource_id": None},
    )
    assert created.status_code == 201
    session = unwrap(created)
    session_id = session["id"]
    assert session["created_at"].endswith("Z")
    assert session["updated_at"].endswith("Z")
    assert session["last_message_at"] is None

    listed = unwrap(client.get("/sessions"))
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == session_id

    fetched = unwrap(client.get(f"/sessions/{session_id}"))
    assert fetched["title"] == "GMV analysis"

    updated = unwrap(client.patch(f"/sessions/{session_id}", json={"title": "GMV drop"}))
    assert updated["title"] == "GMV drop"

    messages = unwrap(client.get(f"/sessions/{session_id}/messages"))
    assert messages == {"items": [], "total": 0, "page": 1, "page_size": 20}

    deleted = unwrap(client.delete(f"/sessions/{session_id}"))
    assert deleted == {"deleted": True}

    missing = client.get(f"/sessions/{session_id}", headers={"x-request-id": "req_test"})
    body = missing.json()
    assert missing.status_code == 404
    assert body["request_id"] == "req_test"
    assert body["error"]["code"] == "SESSION_NOT_FOUND"


def test_session_list_filters_title_and_escapes_like_wildcards(client) -> None:
    unwrap(client.post("/sessions", json={"title": "GMV 分析", "selected_datasource_id": None}))
    unwrap(client.post("/sessions", json={"title": "100%完成", "selected_datasource_id": None}))

    matched = unwrap(client.get("/sessions", params={"q": "GMV"}))
    assert matched["total"] == 1
    assert matched["items"][0]["title"] == "GMV 分析"

    wildcard = unwrap(client.get("/sessions", params={"q": "%"}))
    assert wildcard["total"] == 1
    assert wildcard["items"][0]["title"] == "100%完成"


def test_session_datasource_binding_rejects_missing_source(client) -> None:
    response = client.post(
        "/sessions",
        json={"title": "GMV analysis", "selected_datasource_id": "ds_demo"},
    )
    body = response.json()

    assert response.status_code == 404
    assert body["error"]["code"] == "DATASOURCE_NOT_FOUND"


def test_session_datasource_binding_requires_mask_field_confirmation(client) -> None:
    uploaded = client.post(
        "/datasources/upload",
        data={"type": "csv", "name": "Orders"},
        files={"file": ("orders.csv", b"order_id,customer_email\n1,a@example.com\n", "text/csv")},
    )
    assert uploaded.status_code == 202
    datasource_id = unwrap(uploaded)["id"]

    blocked = client.post(
        "/sessions",
        json={"title": "GMV analysis", "selected_datasource_id": datasource_id},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "DATASOURCE_NOT_READY"

    confirmed = client.patch(
        f"/datasources/{datasource_id}/mask-fields",
        json={"mask_fields": []},
    )
    assert confirmed.status_code == 200

    created = client.post(
        "/sessions",
        json={"title": "GMV analysis", "selected_datasource_id": datasource_id},
    )
    assert created.status_code == 201


def test_model_profile_secrets_are_encrypted_and_not_returned(
    client, migrated_settings: Settings
) -> None:
    first = unwrap(
        client.post(
            "/model-profiles",
            json={
                "name": "DeepSeek",
                "provider": "openai-compatible",
                "model_name": "deepseek-chat",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-secret-one",
                "temperature": 0,
            },
        )
    )
    second = unwrap(
        client.post(
            "/model-profiles",
            json={
                "name": "Qwen",
                "provider": "openai-compatible",
                "model_name": "qwen-max",
                "base_url": "https://dashscope.example.com/v1",
                "api_key": "sk-secret-two",
                "temperature": 0.2,
            },
        )
    )

    assert first["has_api_key"] is True
    assert second["has_api_key"] is True
    assert first["run_timeout_seconds"] == 600
    assert second["run_timeout_seconds"] == 600
    assert "api_key" not in first
    assert "sk-secret" not in client.get("/model-profiles").text

    db_path = Path(migrated_settings.metadata_database_url.removeprefix("sqlite+aiosqlite:///"))
    with sqlite3.connect(db_path) as connection:
        encrypted_values = [
            row[0] for row in connection.execute("SELECT encrypted_value FROM secrets")
        ]
    assert len(encrypted_values) == 2
    assert all("sk-secret" not in value for value in encrypted_values)

    active_first = unwrap(client.post(f"/model-profiles/{first['id']}/activate"))
    assert active_first["is_active"] is True
    active_second = unwrap(client.post(f"/model-profiles/{second['id']}/activate"))
    assert active_second["is_active"] is True

    updated_second = unwrap(
        client.patch(
            f"/model-profiles/{second['id']}",
            json={"run_timeout_seconds": 120},
        )
    )
    assert updated_second["run_timeout_seconds"] == 120

    invalid_timeout = client.patch(
        f"/model-profiles/{second['id']}",
        json={"run_timeout_seconds": 601},
    )
    assert invalid_timeout.status_code == 422
    assert invalid_timeout.json()["error"]["code"] == "VALIDATION_ERROR"

    profiles = unwrap(client.get("/model-profiles"))
    active_ids = [item["id"] for item in profiles["items"] if item["is_active"]]
    assert active_ids == [second["id"]]


def test_error_envelope_for_validation(client) -> None:
    response = client.post("/sessions", json={}, headers={"x-request-id": "req_validation"})
    body = response.json()

    assert response.status_code == 422
    assert response.headers["x-request-id"] == "req_validation"
    assert body["request_id"] == "req_validation"
    assert body["data"] is None
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "errors" in body["error"]["details"]


def test_error_envelope_for_query_validation(client) -> None:
    response = client.get("/sessions?page=0", headers={"x-request-id": "req_query"})
    body = response.json()

    assert response.status_code == 422
    assert response.headers["x-request-id"] == "req_query"
    assert body["request_id"] == "req_query"
    assert body["data"] is None
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_openapi_exposes_contract_response_models(settings: Settings) -> None:
    schema = create_app(settings).openapi()
    session_create_schema = schema["paths"]["/sessions"]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]

    assert "ApiEnvelope" in session_create_schema["$ref"]
    assert "SessionRead" in json.dumps(schema["components"]["schemas"])
    assert "ModelProfileTestResult" in json.dumps(schema["components"]["schemas"])


def test_sensitive_field_policy_is_reusable() -> None:
    policy = SensitiveFieldPolicy(mask_fields=frozenset({"email"}), confirmed=True)

    assert not policy.is_sensitive_field("api_key")
    assert policy.is_sensitive_field("email")
    assert policy.mask_if_sensitive("email", "person@example.com") == "***"

    case_insensitive = SensitiveFieldPolicy(mask_fields=frozenset({"Phone"}), confirmed=True)
    assert case_insensitive.is_sensitive_field("phone")
    assert case_insensitive.mask_if_sensitive("PHONE", "13800138000") == "***"


def test_sensitive_field_policy_detects_dominant_email_or_phone_formats() -> None:
    policy = SensitiveFieldPolicy(mask_fields=frozenset({"contact"}), confirmed=True)

    assert policy.is_sensitive_values("contact", ["任意值"])
    assert not policy.is_sensitive_values("other", ["alice@example.com"])


def test_run_event_contract_keeps_payload_generic() -> None:
    event = RunEventRead(
        run_id="run_1",
        seq=1,
        type=RunEventType.TOOL_SUCCEEDED,
        timestamp=datetime.now(UTC),
        payload={
            "tool_call_id": "tool_1",
            "tool_name": "run_sql_readonly",
            "elapsed_ms": 3,
            "evidence_count": 2,
        },
    )

    assert event.type == RunEventType.TOOL_SUCCEEDED
    assert event.payload["evidence_count"] == 2


def test_datalink_contract_shells_validate_boundary_fields() -> None:
    request = DataLinkRebuildRequest(
        datasource_id="ds_demo",
        rebuild_key="rebuild_1",
        source_type="sqlite",
        source_ref="ds_demo/source.sqlite",
        schema_revision=3,
    )
    result = DataLinkRebuildResult(
        build_id="build_1",
        datasource_id=request.datasource_id,
        status=DataLinkBuildStatus.COMPLETED,
        requested_schema_revision=request.schema_revision,
        graph_version="graph_1",
    )

    explore = DataLinkExploreRequest(
        datasource_id="ds_demo",
        graph_version="graph_1",
        query="GMV orders customers",
    )
    explore_result = DataLinkExploreResult(
        datasource_id=explore.datasource_id,
        graph_version=explore.graph_version,
        query=explore.query,
        nodes=[
            {
                "id": "column:ds_demo:orders:total_amount",
                "type": "column",
                "name": "total_amount",
                "table": "orders",
            }
        ],
    )

    assert result.requested_schema_revision == 3
    assert explore.max_nodes == 12
    assert explore_result.nodes[0].type == "column"
    assert not hasattr(DataLinkBuildStatus, "QUEUED")


def test_datalink_rebuild_rejects_evaluation_answer_reference() -> None:
    with pytest.raises(ValidationError):
        DataLinkRebuildRequest(
            datasource_id="ds_demo",
            rebuild_key="rebuild_1",
            source_type="sqlite",
            source_ref="ds_demo/source.sqlite",
            schema_revision=3,
            metric_definitions_ref="demo/metric-definitions.yaml",
        )


def test_datalink_explore_rejects_unknown_focus() -> None:
    with pytest.raises(ValidationError):
        DataLinkExploreRequest(
            datasource_id="ds_demo",
            graph_version="graph_1",
            query="GMV",
            focus="all",
        )


@pytest.mark.parametrize(
    "bad_ref", ["", "C:/secret/source.sqlite", "../outside.sqlite", "demo\\x.db"]
)
def test_datalink_rebuild_rejects_unsafe_refs(bad_ref: str) -> None:
    with pytest.raises(ValidationError):
        DataLinkRebuildRequest(
            datasource_id="ds_demo",
            rebuild_key="rebuild_1",
            source_type="sqlite",
            source_ref=bad_ref,
            schema_revision=3,
        )


def test_settings_require_secret_master_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="test",
            api_host="127.0.0.1",
            api_port=8000,
            api_workers=1,
            metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
            datasource_root=tmp_path / "datasources",
            artifact_root=tmp_path / "artifacts",
            secret_master_key="",
            cors_origins=["http://testserver"],
        )


def test_settings_default_datalink_timeout_covers_initial_graph_build(tmp_path: Path) -> None:
    """默认等待时间应覆盖真实首建图谱所需的较长模型调用。"""

    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )

    assert settings.datalink_timeout_seconds == 180


def test_settings_exposes_bounded_agent_runtime_limits(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )

    assert settings.runtime_limits.analysis_plan_followup_timeout_seconds == 120
    assert settings.runtime_limits.commit_timeout_seconds == 120
    assert settings.runtime_limits.agent_turn_timeout_seconds == 120
    assert settings.runtime_limits.model_max_output_tokens == 4096
    assert settings.runtime_limits.context_window_tokens == 32768
    assert settings.runtime_limits.agent_max_turns == 20
    assert settings.runtime_limits.agent_max_data_tool_calls == 16
    assert settings.runtime_limits.agent_max_discovery_attempts == 2
    assert settings.runtime_limits.agent_max_assertion_failures == 2
    assert settings.agent_runtime_limits.max_model_context_chars == 64_000


def test_settings_accepts_bounded_runtime_env_override_and_falls_back_for_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANALYSIS_PLAN_FOLLOWUP_TIMEOUT_SECONDS", "90")
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )
    assert settings.runtime_limits.analysis_plan_followup_timeout_seconds == 90

    monkeypatch.setenv("ANALYSIS_PLAN_FOLLOWUP_TIMEOUT_SECONDS", "9999")
    invalid = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'invalid.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )
    assert invalid.runtime_limits.analysis_plan_followup_timeout_seconds == 120


def test_settings_accepts_bounded_agent_turn_timeout_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANALYSIS_AGENT_TURN_TIMEOUT_SECONDS", "90")
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'agent-turn.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )

    assert settings.runtime_limits.agent_turn_timeout_seconds == 90


def test_settings_accepts_bounded_agent_graph_budget_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_MAX_DATA_TOOL_CALLS", "24")
    monkeypatch.setenv("AGENT_MAX_TURNS", "30")
    monkeypatch.setenv("AGENT_MAX_DISCOVERY_ATTEMPTS", "1")
    monkeypatch.setenv("AGENT_MAX_ASSERTION_FAILURES", "4")
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'agent-budget.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )

    limits = settings.agent_runtime_limits
    assert limits.max_data_tool_calls == 24
    assert limits.max_turns == 30
    assert limits.max_discovery_attempts == 1
    assert limits.max_assertion_failures == 4


def test_settings_accepts_context_window_runtime_fallback_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW_TOKENS", "65536")
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'context-window.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )
    assert settings.runtime_limits.context_window_tokens == 65536


def test_settings_accepts_extended_run_opening_timeout_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_OPENING_TIMEOUT_SECONDS", "300")
    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'requirements-budget.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        cors_origins=["http://testserver"],
    )

    assert settings.runtime_limits.run_opening_timeout_seconds == 300


def test_settings_parse_comma_separated_cors_origins_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "METADATA_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
    )
    monkeypatch.setenv("DATASOURCE_ROOT", str(tmp_path / "datasources"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SECRET_MASTER_KEY", "test-secret-master-key-32-bytes")
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "http://localhost:5173, http://127.0.0.1:5173",
    )

    settings = Settings()

    assert settings.cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_create_app_applies_langsmith_settings_to_langchain_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = (
        "LANGSMITH_TRACING",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
    )
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        _env_file=None,
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        api_workers=1,
        metadata_database_url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
        datasource_root=tmp_path / "datasources",
        artifact_root=tmp_path / "artifacts",
        secret_master_key="test-secret-master-key-32-bytes",
        langsmith_tracing=True,
        langsmith_endpoint="https://smith.example.test",
        langsmith_api_key="test-langsmith-key",
        langsmith_project="DataPilot-test",
    )

    try:
        create_app(settings)

        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_ENDPOINT"] == "https://smith.example.test"
        assert os.environ["LANGSMITH_API_KEY"] == "test-langsmith-key"
        assert os.environ["LANGSMITH_PROJECT"] == "DataPilot-test"
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
