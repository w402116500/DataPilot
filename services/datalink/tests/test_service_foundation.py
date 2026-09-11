from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.routing import Mount

from server import config
from server.config import Settings, load_settings
from server.main import create_app


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """创建隔离配置，避免服务基础测试使用工作目录下的真实文件。"""

    source_root = tmp_path / "datasources"
    source_root.mkdir()
    values: dict[str, object] = {
        "source_root": source_root,
        "database_path": tmp_path / "datalink" / "datalink.db",
    }
    values.update(overrides)
    return Settings(**values)


def test_health_reports_safe_service_state(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        llm_model="test-model",
        llm_base_url="https://models.example.test/v1",
        llm_api_key="test-secret-key",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "graph_database": "ok",
        "source_root": "ok",
        "model_configured": True,
        "mcp_mounted": True,
    }
    assert "test-secret-key" not in response.text
    assert str(settings.database_path) not in response.text


def test_health_degrades_when_source_root_is_missing(tmp_path: Path) -> None:
    settings = Settings(
        source_root=tmp_path / "missing",
        database_path=tmp_path / "datalink" / "datalink.db",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["source_root"] == "missing"


def test_settings_only_read_datalink_prefixed_values(monkeypatch) -> None:
    monkeypatch.setenv("API_PORT", "9999")
    monkeypatch.setenv("DATALINK_PORT", "8123")

    settings = Settings()

    assert settings.port == 8123


def test_load_settings_anchors_the_env_file_at_project_root(tmp_path: Path, monkeypatch) -> None:
    """从服务子目录启动时也必须读取项目根目录指定的配置文件。"""

    env_file = tmp_path / ".env"
    env_file.write_text("DATALINK_PORT=8124\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ENV_FILE", env_file)

    assert load_settings().port == 8124


def test_mcp_is_mounted_at_the_confirmed_path(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    mcp_mounts = [
        route for route in app.routes if isinstance(route, Mount) and route.path == "/mcp"
    ]

    assert len(mcp_mounts) == 1
    assert app.state.mcp_mounted is True


def test_mcp_initialize_starts_streamable_http_session_manager(tmp_path: Path) -> None:
    """父服务生命周期必须带起 MCP 会话管理器，避免初始化请求返回 500。"""

    initialize_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "datalink-service-test", "version": "0"},
        },
    }

    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.post(
            "/mcp/",
            json=initialize_request,
            headers={"accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 200
    message = next(
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )
    assert json.loads(message)["result"]["serverInfo"]["name"] == "DataPilot DataLink"
