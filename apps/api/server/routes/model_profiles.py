from __future__ import annotations

from typing import Annotated

from application.model_profiles import ModelProfileService
from contracts.api import ApiEnvelope, DeleteResult, Page, PageResult
from contracts.model_profiles import (
    ModelProfileCreate,
    ModelProfileRead,
    ModelProfileTestResult,
    ModelProfileUpdate,
)
from fastapi import APIRouter, Depends, Query, Request

from server.dependencies import get_model_profile_service
from server.responses import success_response

router = APIRouter(prefix="/model-profiles", tags=["model-profiles"])
ModelProfileServiceDep = Annotated[ModelProfileService, Depends(get_model_profile_service)]


@router.get("", response_model=ApiEnvelope[PageResult[ModelProfileRead]])
async def list_model_profiles(
    request: Request,
    service: ModelProfileServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """分页返回模型配置，响应不会包含 API Key。"""

    return success_response(request, await service.list(Page(page=page, page_size=page_size)))


@router.post("", status_code=201, response_model=ApiEnvelope[ModelProfileRead])
async def create_model_profile(
    request: Request,
    payload: ModelProfileCreate,
    service: ModelProfileServiceDep,
):
    """创建模型配置，并将 API Key 交给服务层加密保存。"""

    return success_response(request, await service.create(payload), status_code=201)


@router.get("/{profile_id}", response_model=ApiEnvelope[ModelProfileRead])
async def get_model_profile(
    request: Request,
    profile_id: str,
    service: ModelProfileServiceDep,
):
    """读取指定模型配置的安全投影。"""

    return success_response(request, await service.get(profile_id))


@router.patch("/{profile_id}", response_model=ApiEnvelope[ModelProfileRead])
async def update_model_profile(
    request: Request,
    profile_id: str,
    payload: ModelProfileUpdate,
    service: ModelProfileServiceDep,
):
    """更新模型配置；传入新的 API Key 时会替换密文。"""

    return success_response(request, await service.update(profile_id, payload))


@router.post("/{profile_id}/test", response_model=ApiEnvelope[ModelProfileTestResult])
async def test_model_profile(
    request: Request,
    profile_id: str,
    service: ModelProfileServiceDep,
):
    """检查配置是否具备后续连接模型所需的基础条件。"""

    return success_response(request, await service.test(profile_id))


@router.post("/{profile_id}/activate", response_model=ApiEnvelope[ModelProfileRead])
async def activate_model_profile(
    request: Request,
    profile_id: str,
    service: ModelProfileServiceDep,
):
    """将指定模型配置设为当前唯一激活项。"""

    return success_response(request, await service.activate(profile_id))


@router.delete("/{profile_id}", response_model=ApiEnvelope[DeleteResult])
async def delete_model_profile(
    request: Request,
    profile_id: str,
    service: ModelProfileServiceDep,
):
    """删除模型配置及其关联的加密 API Key。"""

    await service.delete(profile_id)
    return success_response(request, {"deleted": True})
