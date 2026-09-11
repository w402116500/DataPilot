from __future__ import annotations

import pytest
from application.model_capability_probe import (
    ModelCapabilityProbeRequest,
    ModelCapabilityProbeResult,
)
from application.model_profiles import ModelProfileService
from application.secret_cipher import SecretCipher
from contracts.api import Page
from contracts.model_profiles import (
    ModelProfileCapabilities,
    ModelProfileCreate,
    ModelProfileUpdate,
    build_model_capability_fingerprint,
)
from contracts.status import ModelProfileStatus
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.models import SecretModel
from metadata.repositories import ModelProfileRepository, SecretRepository
from server.config import Settings


def _capability_fingerprint(*, temperature: float, api_key: str = "test-api-key") -> str:
    return build_model_capability_fingerprint(
        provider="openai-compatible",
        model_name="demo-model",
        base_url="https://model.example/v1",
        api_key=api_key,
        temperature=temperature,
        run_timeout_seconds=60,
    )


class _FakeCapabilityProbe:
    def __init__(self, result: ModelCapabilityProbeResult) -> None:
        self.result = result
        self.requests: list[ModelCapabilityProbeRequest] = []

    async def probe(self, request: ModelCapabilityProbeRequest) -> ModelCapabilityProbeResult:
        self.requests.append(request)
        return self.result


@pytest.mark.asyncio
async def test_model_profile_service_records_real_probe_result(migrated_settings: Settings) -> None:
    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            cipher = SecretCipher(migrated_settings.secret_master_key)
            secrets = SecretRepository(db)
            secret = await secrets.create(cipher.encrypt("test-api-key"))
            profiles = ModelProfileRepository(db)
            profile = await profiles.create(
                ModelProfileCreate(
                    name="Demo model",
                    provider="openai-compatible",
                    model_name="demo-model",
                    base_url="https://model.example/v1",
                    api_key="test-api-key",
                    temperature=0,
                    run_timeout_seconds=60,
                ),
                secret.id,
            )
            probe = _FakeCapabilityProbe(
                ModelCapabilityProbeResult(
                    tool_calling_supported=True,
                    final_output_mode="json_object",
                    message="probe succeeded",
                )
            )
            service = ModelProfileService(profiles, secrets, cipher, probe)

            result = await service.test(profile.id)

            assert result.status is ModelProfileStatus.TESTED
            assert result.tool_calling_supported is True
            assert result.final_output_mode == "json_object"
            stored = await profiles.get(profile.id)
            assert stored is not None
            assert stored.capability_fingerprint is not None
            assert stored.final_output_mode == "json_object"
            listed = await service.list(Page(page=1, page_size=20))
            assert listed.items[0].tool_calling_supported is True
            assert listed.items[0].final_output_mode == "json_object"
            assert len(probe.requests) == 1
            assert probe.requests[0].api_key == "test-api-key"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_profile_configuration_change_invalidates_capabilities(
    migrated_settings: Settings,
) -> None:
    """会影响模型请求的配置或密钥更新后，旧探测结果不能再用于创建 Run。"""

    engine = create_sqlite_engine(migrated_settings.metadata_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            db.add(SecretModel(id="secret_1", encrypted_value="encrypted"))
            await db.flush()
            profiles = ModelProfileRepository(db)
            profile = await profiles.create(
                ModelProfileCreate(
                    name="Demo model",
                    provider="openai-compatible",
                    model_name="demo-model",
                    base_url="https://model.example/v1",
                    api_key="test-api-key",
                    temperature=0,
                    run_timeout_seconds=60,
                ),
                "secret_1",
            )
            fingerprint = _capability_fingerprint(temperature=0)
            profile = await profiles.record_capabilities(
                profile,
                ModelProfileCapabilities(
                    tool_calling_supported=True,
                    final_output_mode="json_schema",
                    capability_fingerprint=fingerprint,
                ),
            )

            assert profile.status == ModelProfileStatus.TESTED.value
            assert profile.capability_checked_at is not None
            assert profile.capability_fingerprint == fingerprint
            assert "test-api-key" not in fingerprint

            profile = await profiles.update(profile, ModelProfileUpdate(name="Renamed model"))
            assert profile.status == ModelProfileStatus.TESTED.value
            assert profile.capability_fingerprint == fingerprint

            profile = await profiles.update(profile, ModelProfileUpdate(temperature=0.2))
            assert profile.status == ModelProfileStatus.CREATED.value
            assert profile.tool_calling_supported is None
            assert profile.final_output_mode is None
            assert profile.capability_fingerprint is None
            assert profile.capability_checked_at is None

            profile = await profiles.record_capabilities(
                profile,
                ModelProfileCapabilities(
                    tool_calling_supported=True,
                    final_output_mode="json_object",
                    capability_fingerprint=_capability_fingerprint(temperature=0.2),
                ),
            )
            profile = await profiles.update(
                profile,
                ModelProfileUpdate(api_key="replacement-api-key"),
            )
            assert profile.status == ModelProfileStatus.CREATED.value
            assert profile.tool_calling_supported is None
            assert profile.final_output_mode is None
            assert profile.capability_fingerprint is None
            assert profile.capability_checked_at is None
    finally:
        await engine.dispose()
