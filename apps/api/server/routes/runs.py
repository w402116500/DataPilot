"""阶段五 Run 命令、SSE 与只读历史路由。"""

from __future__ import annotations

from typing import Annotated

from contracts.api import ApiEnvelope
from contracts.run_events import RunEventRead
from contracts.runs import (
    RunArtifactRead,
    RunCancelRead,
    RunCancelRequest,
    RunRead,
    SqlAuditRead,
    ToolCallRead,
)
from contracts.trace_dag import TraceDagRead
from contracts.validation import DataLinkConsumptionListRead, DataLinkConsumptionRead
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from runtime.run_event_stream import RunEventStream
from runtime.run_history_service import RunHistoryService
from runtime.run_service import RunService

from server.dependencies import get_run_event_stream, get_run_history_service, get_run_service
from server.responses import success_response

router = APIRouter(tags=["runs"])
RunServiceDep = Annotated[RunService, Depends(get_run_service)]
RunHistoryServiceDep = Annotated[RunHistoryService, Depends(get_run_history_service)]
RunEventStreamDep = Annotated[RunEventStream, Depends(get_run_event_stream)]


@router.get("/runs/{run_id}", response_model=ApiEnvelope[RunRead])
async def get_run(request: Request, run_id: str, service: RunServiceDep):
    """读取 Run 的固定身份和最终状态，不会启动或恢复分析。"""

    return success_response(request, await service.get(run_id))


@router.post("/runs/{run_id}/cancel", response_model=ApiEnvelope[RunCancelRead])
async def cancel_run(
    request: Request,
    run_id: str,
    payload: RunCancelRequest,
    service: RunServiceDep,
):
    """登记取消命令；实际停止和终态由后台 Finalizer 完成。"""

    return success_response(request, await service.cancel(run_id, reason=payload.reason))


@router.get("/runs/{run_id}/events", response_model=None)
async def stream_run_events(
    run_id: str,
    stream: RunEventStreamDep,
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """按 seq 回放和订阅 Run 事件；心跳不写进数据库。"""

    await stream.ensure_run_exists(run_id)
    return StreamingResponse(
        stream.events(run_id, after_seq=after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/events/history", response_model=ApiEnvelope[list[RunEventRead]])
async def list_run_events(
    request: Request,
    run_id: str,
    stream: RunEventStreamDep,
    after_seq: int = Query(default=0, ge=0),
):
    """给非 SSE 客户端提供同一事件账本的只读分页窗口。"""

    await stream.ensure_run_exists(run_id)
    return success_response(request, await stream.list_after(run_id, after_seq=after_seq))


@router.get("/runs/{run_id}/tool-calls", response_model=ApiEnvelope[list[ToolCallRead]])
async def list_run_tool_calls(request: Request, run_id: str, service: RunHistoryServiceDep):
    """读取工具调用安全摘要，不返回原始输入或输出。"""

    return success_response(request, await service.list_tool_calls(run_id))


@router.get("/runs/{run_id}/trace-dag", response_model=ApiEnvelope[TraceDagRead])
async def get_run_trace_dag(request: Request, run_id: str, service: RunHistoryServiceDep):
    """返回当前 Run 的只读 Trace DAG，不启动或恢复任何分析。"""

    return success_response(request, await service.get_trace_dag(run_id))


@router.get("/runs/{run_id}/sql-audits", response_model=ApiEnvelope[list[SqlAuditRead]])
async def list_run_sql_audits(request: Request, run_id: str, service: RunHistoryServiceDep):
    """读取 SQL 审计元数据，不回显 SQL 正文。"""

    return success_response(request, await service.list_sql_audits(run_id))


@router.get(
    "/runs/{run_id}/datalink-consumptions",
    response_model=ApiEnvelope[DataLinkConsumptionListRead],
)
async def list_run_datalink_consumptions(
    request: Request, run_id: str, service: RunHistoryServiceDep
):
    """读取本次 Run 当时消费的安全语义投影，不重新访问 DataLink。"""

    return success_response(request, await service.list_datalink_consumptions(run_id))


@router.get(
    "/runs/{run_id}/datalink-consumptions/{consumption_id}",
    response_model=ApiEnvelope[DataLinkConsumptionRead],
)
async def get_run_datalink_consumption(
    request: Request,
    run_id: str,
    consumption_id: str,
    service: RunHistoryServiceDep,
):
    return success_response(request, await service.get_datalink_consumption(run_id, consumption_id))


@router.get("/runs/{run_id}/artifacts", response_model=ApiEnvelope[list[RunArtifactRead]])
async def list_run_artifacts(request: Request, run_id: str, service: RunHistoryServiceDep):
    """读取 Run 的 Artifact 元数据，不泄露受控文件路径。"""

    return success_response(request, await service.list_artifacts(run_id))


@router.get("/artifacts/{artifact_id}", response_model=ApiEnvelope[RunArtifactRead])
async def get_artifact(request: Request, artifact_id: str, service: RunHistoryServiceDep):
    """读取单个属于 Run 的 Artifact 安全元数据。"""

    return success_response(request, await service.get_artifact(artifact_id))


@router.get("/artifacts/{artifact_id}/content", response_model=None)
async def get_artifact_content(artifact_id: str, service: RunHistoryServiceDep) -> Response:
    """返回受控 Artifact 正文，绝不让请求指定存储路径或文件名。"""

    content, mime_type = await service.read_artifact_content(artifact_id)
    return Response(
        content=content,
        media_type=mime_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/artifacts/{artifact_id}/download", response_model=None)
async def download_artifact(artifact_id: str, service: RunHistoryServiceDep) -> Response:
    """以受控文件名下载已经登记的 Artifact，不暴露宿主机路径。"""

    content, mime_type, filename = await service.artifact_download(artifact_id)
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
