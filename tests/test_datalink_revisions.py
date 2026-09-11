"""Management boundary and published Head reconciliation regressions."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from application.datalink_client import DataLinkClient, DataLinkClientError
from application.datalink_preview import DataLinkPreviewService
from application.datasources import DataSourceService
from contracts.datalink import (
    DataLinkBuildRead,
    DataLinkDraftExploreRead,
    DataLinkDraftPreviewRequest,
    DataLinkDraftSaveRequest,
    DataLinkPublishRequest,
    DataLinkRebuildResult,
    DataLinkStatusRead,
)
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import DataSourceModel
from metadata.repositories import DataSourceRepository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        {"wrong": "shape"},
        {
            "datasource_id": "other",
            "base_graph_version": "g",
            "schema_revision": 1,
            "draft_revision": 1,
            "status": "active",
            "changes": [],
        },
    ],
)
async def test_draft_absence_and_invalid_upstream_identity(body):
    client = DataLinkClient(
        "http://test",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(body))),
    )
    if body is None:
        assert await client.draft("ds") is None
    else:
        with pytest.raises(DataLinkClientError) as exc:
            await client.draft("ds")
        assert exc.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "publish"])
async def test_revision_write_timeout_does_not_automatically_repeat(operation):
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("internal address", request=request)

    client = DataLinkClient(
        "http://test", timeout_seconds=1, transport=httpx.MockTransport(timeout)
    )
    with pytest.raises(DataLinkClientError) as exc:
        if operation == "save":
            await client.save_draft(
                "ds",
                DataLinkDraftSaveRequest(base_graph_version="g", schema_revision=1, changes=[]),
            )
        else:
            await client.publish(
                "ds",
                DataLinkPublishRequest(
                    expected_head="g", expected_draft_revision=1, idempotency_key="key"
                ),
            )
    assert len(requests) == 1
    assert exc.value.status_code == 503
    assert "internal address" not in exc.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_status", ["completed", "running"])
@pytest.mark.parametrize("source_status", ["ready", "schema_ready"])
async def test_completed_candidate_never_becomes_metadata_head(latest_status, source_status):
    published = DataLinkBuildRead(
        build_id="old",
        datasource_id="ds",
        schema_revision=1,
        attempt_no=1,
        status="completed",
        graph_version="published",
    )
    candidate = published.model_copy(
        update={
            "status": latest_status,
            "build_id": "new",
            "graph_version": "candidate",
            "publication_state": "candidate",
            "attempt_no": 2,
        }
    )
    repository = SimpleNamespace(
        complete_datalink_build=AsyncMock(), fail_datalink_build=AsyncMock()
    )
    service = object.__new__(DataSourceService)
    service.repository = repository
    model = SimpleNamespace(id="ds", schema_revision=1, status=source_status)
    await service._reconcile_datalink_status(
        model,
        DataLinkStatusRead(
            datasource_id="ds",
            current_graph_version="published",
            head_build=published,
            current_build=candidate,
        ),
    )
    repository.complete_datalink_build.assert_awaited_once_with(
        model, build_id="old", graph_version="published", expected_schema_revision=1
    )
    repository.fail_datalink_build.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_rebuild_candidate_reconciles_head_instead_of_publishing_candidate():
    model = SimpleNamespace(
        id="ds", schema_revision=1, status="ready", type="sqlite", source_ref="source.sqlite"
    )
    result = DataLinkRebuildResult(
        build_id="candidate-build",
        datasource_id="ds",
        status="completed",
        requested_schema_revision=1,
        graph_version="candidate",
        publication_state="candidate",
    )
    service = object.__new__(DataSourceService)
    service.repository = SimpleNamespace(
        start_datalink_build=AsyncMock(return_value=model), complete_datalink_build=AsyncMock()
    )
    status = DataLinkStatusRead(datasource_id="ds", current_graph_version="published")
    service.datalink_client = SimpleNamespace(
        rebuild=AsyncMock(return_value=result), status=AsyncMock(return_value=status)
    )
    service._reconcile_datalink_status = AsyncMock()
    assert await service._rebuild_started_model(model, raise_on_failure=True) == result
    service.repository.complete_datalink_build.assert_not_awaited()
    service._reconcile_datalink_status.assert_awaited_once_with(model, status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,revision,expected",
    [
        ("schema_ready", 1, "new-head"),
        ("schema_ready", 2, "old-head"),
        ("deleting", 1, "old-head"),
    ],
)
async def test_head_projection_recovers_schema_ready_without_reviving_stale_source(
    migrated_settings, state, revision, expected
):
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    try:
        async with create_session_factory(engine)() as db:
            model = DataSourceModel(
                id="revision-source",
                name="Source",
                type="csv",
                status=state,
                schema_revision=revision,
                datalink_graph_version="old-head",
            )
            db.add(model)
            await db.commit()
            await DataSourceRepository(db).complete_datalink_build(
                model, build_id="new-build", graph_version="new-head", expected_schema_revision=1
            )
            await db.commit()
            await db.refresh(model)
            assert model.datalink_graph_version == expected
            assert model.status == ("ready" if expected == "new-head" else state)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_draft_preview_uses_safe_projection_and_distinct_revision_identity():
    response = DataLinkDraftExploreRead(
        datasource_id="ds",
        base_graph_version="base",
        schema_revision=1,
        draft_revision=3,
        result={
            "datasource_id": "ds",
            "graph_version": "base",
            "query": "amount",
            "retrieval_mode": "keyword",
            "nodes": [
                {
                    "id": "internal-column",
                    "type": "column",
                    "table": "orders",
                    "name": "amount",
                    "description": "Before discount",
                    "profile": {
                        "column_id": "internal-column",
                        "dtype": "number",
                        "null_rate": 0,
                        "distinct_count": 1,
                        "unique_rate": 1,
                        "sample_values": [12345],
                    },
                }
            ],
        },
    )
    datasources = SimpleNamespace(datalink_draft_preview=AsyncMock(return_value=response))
    service = DataLinkPreviewService(datasources, SimpleNamespace())
    result = await service.preview_draft(
        "ds", DataLinkDraftPreviewRequest(expected_draft_revision=3, query="amount")
    )
    assert result.draft_revision == 3 and result.base_graph_version == "base"
    assert result.semantic_context.fields[0].description == "Before discount"
    assert "sample_values" not in result.model_dump_json()
    assert "internal-column" not in result.model_dump_json()
