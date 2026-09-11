from __future__ import annotations

from contracts.api import Page, PageResult
from contracts.errors import AppError, ErrorCode
from contracts.model_profiles import (
    CAPABILITY_CONTRACT_VERSION,
    ModelProfileCapabilities,
    ModelProfileCreate,
    ModelProfileRead,
    ModelProfileTestResult,
    ModelProfileUpdate,
    build_model_capability_fingerprint,
)
from contracts.status import ModelProfileStatus
from metadata.models import ModelProfileModel
from metadata.repositories import ModelProfileRepository, SecretRepository

from application.model_capability_probe import (
    ModelCapabilityProbe,
    ModelCapabilityProbeRequest,
    ModelCapabilityProbeResult,
)
from application.secret_cipher import SecretCipher


def to_model_profile_read(model: ModelProfileModel) -> ModelProfileRead:
    """把模型配置转换成 API 契约；对外只暴露 has_api_key，不暴露密钥值。"""

    return ModelProfileRead(
        id=model.id,
        name=model.name,
        provider=model.provider,
        model_name=model.model_name,
        base_url=model.base_url,
        temperature=model.temperature,
        run_timeout_seconds=model.run_timeout_seconds,
        context_window_tokens=model.context_window_tokens,
        status=ModelProfileStatus(model.status),
        is_active=model.is_active,
        has_api_key=model.secret_ref is not None,
        tool_calling_supported=model.tool_calling_supported,
        final_output_mode=model.final_output_mode,
        capability_contract_version=model.capability_contract_version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class ModelProfileService:
    """管理模型配置和加密密钥，禁止将 API Key 放入 API 响应或普通 Metadata。"""

    def __init__(
        self,
        profiles: ModelProfileRepository,
        secrets: SecretRepository,
        cipher: SecretCipher,
        capability_probe: ModelCapabilityProbe,
    ) -> None:
        self.profiles = profiles
        self.secrets = secrets
        self.cipher = cipher
        self.capability_probe = capability_probe

    async def create(self, payload: ModelProfileCreate) -> ModelProfileRead:
        """创建模型配置，并把 API Key 加密后单独保存到 Secret 表。"""

        secret = await self.secrets.create(self.cipher.encrypt(payload.api_key))
        model = await self.profiles.create(payload, secret.id)
        return to_model_profile_read(model)

    async def list(self, page: Page) -> PageResult[ModelProfileRead]:
        """分页列出模型配置，仅返回是否配置了密钥这一状态。"""

        items, total = await self.profiles.list(offset=page.offset, limit=page.page_size)
        return PageResult(
            items=[to_model_profile_read(item) for item in items],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )

    async def get(self, profile_id: str) -> ModelProfileRead:
        """读取单个模型配置的安全投影。"""

        model = await self._get_model(profile_id)
        return to_model_profile_read(model)

    async def update(self, profile_id: str, payload: ModelProfileUpdate) -> ModelProfileRead:
        """更新模型配置；如果传入新 API Key，会替换对应的 Secret。"""

        model = await self._get_model(profile_id)
        if payload.api_key is not None:
            encrypted = self.cipher.encrypt(payload.api_key)
            if model.secret_ref is None:
                secret = await self.secrets.create(encrypted)
                model.secret_ref = secret.id
            else:
                secret = await self.secrets.get(model.secret_ref)
                if secret is None:
                    secret = await self.secrets.create(encrypted)
                    model.secret_ref = secret.id
                else:
                    await self.secrets.update(secret, encrypted)
        model = await self.profiles.update(model, payload)
        return to_model_profile_read(model)

    async def test(self, profile_id: str) -> ModelProfileTestResult:
        """真实探测 Profile 的原生 Tool Calling 与最终答案输出能力。"""

        model = await self._get_model(profile_id)
        if model.secret_ref is None:
            raise AppError(
                ErrorCode.MODEL_PROFILE_NOT_READY,
                "Model profile has no API key",
                status_code=409,
                details={"profile_id": profile_id},
            )
        secret = await self.secrets.get(model.secret_ref)
        if secret is None:
            return await self._record_unavailable_secret(model)
        try:
            api_key = self.cipher.decrypt(secret.encrypted_value)
        except Exception:
            return await self._record_unavailable_secret(model)

        fingerprint = build_model_capability_fingerprint(
            provider=model.provider,
            model_name=model.model_name,
            base_url=model.base_url,
            api_key=api_key,
            temperature=model.temperature,
            run_timeout_seconds=model.run_timeout_seconds,
        )
        try:
            probe_result = await self.capability_probe.probe(
                ModelCapabilityProbeRequest(
                    model_name=model.model_name,
                    base_url=model.base_url,
                    api_key=api_key,
                    temperature=model.temperature,
                    timeout_seconds=model.run_timeout_seconds,
                )
            )
        except Exception:
            probe_result = ModelCapabilityProbeResult(
                tool_calling_supported=False,
                final_output_mode=None,
                message="模型能力探测请求失败，请检查模型或兼容接口配置。",
            )

        model = await self.profiles.record_capabilities(
            model,
            ModelProfileCapabilities(
                tool_calling_supported=probe_result.tool_calling_supported,
                final_output_mode=probe_result.final_output_mode,
                capability_contract_version=CAPABILITY_CONTRACT_VERSION,
                capability_fingerprint=fingerprint,
            ),
        )
        return ModelProfileTestResult(
            profile_id=model.id,
            status=ModelProfileStatus(model.status),
            tool_calling_supported=probe_result.tool_calling_supported,
            final_output_mode=probe_result.final_output_mode,
            capability_contract_version=CAPABILITY_CONTRACT_VERSION,
            message=probe_result.message,
        )

    async def _record_unavailable_secret(self, model: ModelProfileModel) -> ModelProfileTestResult:
        """密钥缺失或无法解密时只写安全的失败状态，不保留可运行的 tested 状态。"""

        model = await self.profiles.set_status(model, ModelProfileStatus.FAILED)
        return ModelProfileTestResult(
            profile_id=model.id,
            status=ModelProfileStatus.FAILED,
            tool_calling_supported=False,
            final_output_mode=None,
            capability_contract_version=None,
            message="模型密钥不可用，无法完成能力探测。",
        )

    async def activate(self, profile_id: str) -> ModelProfileRead:
        """将指定配置设为当前唯一激活的模型。"""

        model = await self._get_model(profile_id)
        model = await self.profiles.activate(model)
        return to_model_profile_read(model)

    async def delete(self, profile_id: str) -> None:
        """删除模型配置，并同时回收其独立存储的加密密钥。"""

        model = await self._get_model(profile_id)
        secret_ref = model.secret_ref
        await self.profiles.delete(model)
        await self.secrets.delete_by_id(secret_ref)

    async def _get_model(self, profile_id: str) -> ModelProfileModel:
        """查找模型配置 ORM 模型，统一生成未找到错误。"""

        model = await self.profiles.get(profile_id)
        if model is None:
            raise AppError(
                ErrorCode.MODEL_PROFILE_NOT_FOUND,
                "Model profile does not exist",
                status_code=404,
                details={"profile_id": profile_id},
            )
        return model
