from __future__ import annotations

from typing import Annotated

from application.sessions import SessionService
from contracts.api import ApiEnvelope, DeleteResult, Page, PageResult
from contracts.runs import RunCreate, RunCreateAccepted, RunRead
from contracts.sessions import MessageRead, SessionCreate, SessionRead, SessionUpdate
from fastapi import APIRouter, Depends, Query, Request
from runtime.run_service import RunService

from server.dependencies import get_run_service, get_session_service
from server.responses import success_response

router = APIRouter(prefix="/sessions", tags=["sessions"])
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
RunServiceDep = Annotated[RunService, Depends(get_run_service)]


@router.get("", response_model=ApiEnvelope[PageResult[SessionRead]])
async def list_sessions(
    request: Request,
    service: SessionServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=200),
):
    """分页返回会话列表，可选按标题子串过滤。"""

    return success_response(
        request,
        await service.list(Page(page=page, page_size=page_size), q=q),
    )


@router.post("", status_code=201, response_model=ApiEnvelope[SessionRead])
async def create_session(
    request: Request,
    payload: SessionCreate,
    service: SessionServiceDep,
):
    """创建一个新的会话。"""

    return success_response(request, await service.create(payload), status_code=201)


@router.get("/{session_id}", response_model=ApiEnvelope[SessionRead])
async def get_session(
    request: Request,
    session_id: str,
    service: SessionServiceDep,
):
    """读取指定会话。"""

    return success_response(request, await service.get(session_id))


@router.post("/{session_id}/runs", status_code=202, response_model=ApiEnvelope[RunCreateAccepted])
async def create_run(
    request: Request,
    session_id: str,
    payload: RunCreate,
    service: RunServiceDep,
):
    """按 Session 当前数据源创建一个后台分析 Run。"""

    return success_response(request, await service.create(session_id, payload), status_code=202)


@router.get("/{session_id}/runs", response_model=ApiEnvelope[PageResult[RunRead]])
async def list_session_runs(
    request: Request,
    session_id: str,
    service: RunServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=2000),
):
    """只读返回会话内已保存的 Run 历史，不触发重放。"""

    return success_response(
        request,
        await service.list_for_session(
            session_id,
            Page(page=page, page_size=page_size),
            q=q,
        ),
    )


@router.patch("/{session_id}", response_model=ApiEnvelope[SessionRead])
async def update_session(
    request: Request,
    session_id: str,
    payload: SessionUpdate,
    service: SessionServiceDep,
):
    """更新指定会话的基础信息。"""

    return success_response(request, await service.update(session_id, payload))


@router.delete("/{session_id}", response_model=ApiEnvelope[DeleteResult])
async def delete_session(
    request: Request,
    session_id: str,
    service: SessionServiceDep,
):
    """删除指定会话。"""

    await service.delete(session_id)
    return success_response(request, {"deleted": True})


@router.get("/{session_id}/messages", response_model=ApiEnvelope[PageResult[MessageRead]])
async def list_session_messages(
    request: Request,
    session_id: str,
    service: SessionServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    before_position: int | None = Query(default=None, ge=1),
):
    """返回最近一窗已保存消息；before_position 用于向上加载更早消息。"""

    del page
    return success_response(
        request,
        await service.list_messages(
            session_id,
            Page(page=1, page_size=page_size),
            before_position=before_position,
        ),
    )
