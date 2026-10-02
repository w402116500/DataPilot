"""真实 DataLink 模型的显式验收，不进入日常离线测试门禁。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from contracts.status import DataSourceType
from fastapi.testclient import TestClient

from server.config import load_settings
from server.main import create_app


@pytest.mark.live_model
@pytest.mark.parametrize(
    ("filename", "source_type"),
    [
        ("ecommerce.sqlite", DataSourceType.SQLITE),
        ("ecommerce_flat.csv", DataSourceType.CSV),
    ],
)
def test_live_model_builds_fixed_demo(
    tmp_path: Path, filename: str, source_type: DataSourceType
) -> None:
    """用固定 CSV/SQLite Demo 验证真实模型能完成最小语义建图。"""

    source_root = tmp_path / "datasources"
    source_directory = source_root / "demo"
    source_directory.mkdir(parents=True)
    demo_source = Path(__file__).parents[3] / "data" / "demo" / filename
    shutil.copy2(demo_source, source_directory / filename)
    settings = load_settings(
        source_root=source_root,
        database_path=tmp_path / "datalink" / "datalink.db",
    )
    if not settings.model_configured:
        pytest.skip("未配置 DATALINK_LLM_MODEL、DATALINK_LLM_BASE_URL 和 DATALINK_LLM_API_KEY")

    with TestClient(create_app(settings)) as client:
        rebuild = client.post(
            "/v1/graphs/rebuild",
            json={
                "datasource_id": "live_demo",
                "rebuild_key": f"live_{source_type.value}",
                "source_type": source_type.value,
                "source_ref": f"demo/{filename}",
                "schema_revision": 1,
            },
        )
        assert rebuild.status_code == 202, rebuild.text
        result = rebuild.json()
        assert result["status"] == "running"
        assert result["graph_version"] is None
        status = client.get("/v1/graphs/live_demo/status")
        assert status.status_code == 200, status.text
        build = status.json()["current_build"]
        assert build is not None
        assert build["status"] == "completed", status.text
        graph_version = status.json()["current_graph_version"]
        assert graph_version is not None
        graph = client.get(f"/v1/graphs/live_demo?graph_version={graph_version}")

    assert graph.status_code == 200
    graph_data = graph.json()
    node_types = {node["type"] for node in graph_data["nodes"]}
    assert {"concept", "entity"}.issubset(node_types)
    if source_type is DataSourceType.SQLITE:
        edge_types = {edge["type"] for edge in graph_data["edges"]}
        assert "foreign_key" in edge_types or "joinable" in edge_types
