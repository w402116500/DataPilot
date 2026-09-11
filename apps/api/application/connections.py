"""Own connection configuration, secret lifecycle and DataLink authorization."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from contracts.datalink import DataLinkConnectionGrantConsumeRequest, DataLinkConnectionGrantRead
from contracts.datasources import DataSourceConnectionCreate, DataSourceConnectionUpdate
from contracts.errors import AppError, ErrorCode
from contracts.ids import make_id
from metadata.connection_repository import ConnectionRepository
from metadata.models import DataSourceModel
from metadata.repositories import DataSourceRepository, SecretRepository

from application.secret_cipher import SecretCipher


def _fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


class ConnectionService:
    def __init__(self, repository: DataSourceRepository, cipher: SecretCipher) -> None:
        self.sources = repository
        self.repository = ConnectionRepository(repository.db)
        self.secrets = SecretRepository(repository.db)
        self.cipher = cipher

    async def create(self, payload: DataSourceConnectionCreate) -> DataSourceModel:
        secret = await self.secrets.create(
            self.cipher.encrypt(payload.credentials.password.get_secret_value())
        )
        config = payload.config.model_dump(mode="json")
        return await self.repository.create(
            datasource_id=make_id("datasource"),
            name=payload.name,
            description=payload.description,
            config=config,
            credential_ref=secret.id,
            fingerprint=_fingerprint(config),
        )

    async def update(
        self, model: DataSourceModel, payload: DataSourceConnectionUpdate
    ) -> DataSourceModel:
        if not await self.repository.lock_current(model.id, payload.expected_connection_revision):
            raise AppError(ErrorCode.HEAD_STALE, "连接已变化，请刷新后重试", status_code=409)
        await self.sources.db.refresh(model)
        config = payload.config.model_dump(mode="json")
        changed = config != model.connection_config_json or payload.credentials is not None
        if not changed:
            return model
        old_secret = model.credential_ref
        if payload.credentials is not None:
            secret = await self.secrets.create(
                self.cipher.encrypt(payload.credentials.password.get_secret_value())
            )
            model.credential_ref = secret.id
        model.connection_config_json = config
        model.connection_fingerprint = _fingerprint(config)
        model.connection_revision += 1
        model.datalink_build_id = None
        model.datalink_graph_version = None
        model.mask_fields_confirmed = False
        await self.sources.db.flush()
        await self.repository.revoke(model.id)
        if payload.credentials is not None:
            await self.secrets.delete_by_id(old_secret)
        return await self.sources.start_inspection(model)

    async def issue_grant(self, model: DataSourceModel, rebuild_key: str) -> str:
        if not await self.repository.lock_current(model.id, model.connection_revision):
            raise AppError(ErrorCode.HEAD_STALE, "连接已变化，请重新建图", status_code=409)
        await self.sources.db.refresh(model)
        if (
            model.status not in {"schema_ready", "ready", "building_datalink"}
            or not model.schema_cache_json
        ):
            raise AppError(ErrorCode.DATASOURCE_NOT_READY, "数据源尚未完成检查", status_code=409)
        token = secrets.token_urlsafe(32)
        await self.repository.add_grant(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            datasource_id=model.id,
            rebuild_key=rebuild_key,
            source_type=model.type,
            schema_revision=model.schema_revision,
            connection_revision=model.connection_revision,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return token

    async def consume(
        self, payload: DataLinkConnectionGrantConsumeRequest
    ) -> DataLinkConnectionGrantRead:
        denied = AppError(ErrorCode.BUILD_FAILED, "连接授权无效或已过期", status_code=403)
        # Reserve the source write lock before binding: rotation/revocation cannot
        # interleave the identity check and the private secret read.
        if not await self.repository.lock_current(
            payload.datasource_id, payload.connection_revision
        ):
            raise denied
        model = await self.sources.get(payload.datasource_id)
        await self.sources.db.refresh(model)
        if (
            model.status not in {"schema_ready", "ready", "building_datalink"}
            or model.schema_revision != payload.schema_revision
        ):
            raise denied
        grant = await self.repository.bind_grant(
            token_hash=hashlib.sha256(payload.token.get_secret_value().encode()).hexdigest(),
            datasource_id=payload.datasource_id,
            rebuild_key=payload.rebuild_key,
            build_id=payload.build_id,
            schema_revision=payload.schema_revision,
            connection_revision=payload.connection_revision,
            now=datetime.now(UTC),
        )
        if grant is None or not model.credential_ref:
            raise denied
        secret = await self.secrets.get(model.credential_ref)
        if secret is None:
            raise denied
        return DataLinkConnectionGrantRead(
            datasource_id=model.id,
            source_type=model.type,
            schema_revision=model.schema_revision,
            connection_revision=model.connection_revision,
            config=model.connection_config_json,
            password=self.cipher.decrypt(secret.encrypted_value),
            schema=model.schema_cache_json,
        )
