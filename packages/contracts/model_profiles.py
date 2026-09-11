from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from contracts.status import ModelProfileStatus

FinalOutputMode = Literal["markdown", "json_schema", "json_object", "submit_answer"]
CAPABILITY_CONTRACT_VERSION = "3"


class ModelProfileCapabilities(BaseModel):
    """一次真实模型探测的可持久化结果，不包含任何明文密钥。"""

    model_config = ConfigDict(extra="forbid")

    tool_calling_supported: bool
    final_output_mode: FinalOutputMode | None = None
    capability_contract_version: str = Field(
        default=CAPABILITY_CONTRACT_VERSION, min_length=1, max_length=40
    )
    capability_fingerprint: str = Field(min_length=64, max_length=128)

    @model_validator(mode="after")
    def _validate_final_output_mode(self) -> ModelProfileCapabilities:
        if self.tool_calling_supported and self.final_output_mode is None:
            raise ValueError("Tool Calling 通过时必须保存最终答案输出模式")
        if not self.tool_calling_supported and self.final_output_mode is not None:
            raise ValueError("Tool Calling 未通过时不能保存最终答案输出模式")
        return self


def build_model_capability_fingerprint(
    *,
    provider: str,
    model_name: str,
    base_url: str,
    api_key: str,
    temperature: float,
    run_timeout_seconds: int,
) -> str:
    """生成请求配置版本标记，最终结果和中间内容都不保存明文密钥。"""

    secret_version_marker = sha256(api_key.encode("utf-8")).hexdigest()
    payload = {
        # Changing the output negotiation order invalidates previously tested profiles.
        "version": 4,
        "provider": provider,
        "model_name": model_name,
        "base_url": base_url,
        "temperature": temperature,
        "run_timeout_seconds": run_timeout_seconds,
        "secret_version_marker": secret_version_marker,
    }
    canonical_payload = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return sha256(canonical_payload.encode("utf-8")).hexdigest()


class ModelProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="openai-compatible", min_length=1, max_length=80)
    model_name: str = Field(min_length=1, max_length=120)
    base_url: AnyHttpUrl
    api_key: str = Field(min_length=1, max_length=4096)
    temperature: float = Field(default=0, ge=0, le=2)
    run_timeout_seconds: int = Field(default=600, ge=1, le=600)
    context_window_tokens: int | None = Field(default=None, ge=1_024, le=1_000_000)


class ModelProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: AnyHttpUrl | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    temperature: float | None = Field(default=None, ge=0, le=2)
    run_timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    context_window_tokens: int | None = Field(default=None, ge=1_024, le=1_000_000)


class ModelProfileRead(BaseModel):
    id: str
    name: str
    provider: str
    model_name: str
    base_url: str
    temperature: float
    run_timeout_seconds: int
    context_window_tokens: int | None
    status: ModelProfileStatus
    is_active: bool
    has_api_key: bool
    tool_calling_supported: bool | None
    final_output_mode: FinalOutputMode | None
    capability_contract_version: str | None
    created_at: datetime
    updated_at: datetime


class ModelProfileTestResult(BaseModel):
    profile_id: str
    status: ModelProfileStatus
    tool_calling_supported: bool
    final_output_mode: FinalOutputMode | None = None
    capability_contract_version: str | None = None
    message: str
