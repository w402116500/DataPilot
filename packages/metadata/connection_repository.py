"""Transactional connection updates and durable, revocable Build grants."""

from datetime import UTC, datetime

from contracts.ids import make_id
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from metadata.models import ConnectionGrantModel, DataSourceModel


class ConnectionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        datasource_id: str,
        name: str,
        description: str | None,
        config: dict,
        credential_ref: str,
        fingerprint: str,
    ) -> DataSourceModel:
        model = DataSourceModel(
            id=datasource_id,
            name=name,
            description=description,
            type="mysql",
            source_kind="connection",
            connection_config_json=config,
            credential_ref=credential_ref,
            connection_fingerprint=fingerprint,
            connection_revision=1,
            status="inspecting",
            inspection_id=make_id("inspection"),
        )
        self.db.add(model)
        await self.db.flush()
        return model

    async def lock_current(self, datasource_id: str, revision: int) -> bool:
        result = await self.db.execute(
            update(DataSourceModel)
            .where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.source_kind == "connection",
                DataSourceModel.connection_revision == revision,
                DataSourceModel.status.not_in(("deleting", "deleted")),
            )
            .values(connection_revision=DataSourceModel.connection_revision)
        )
        return result.rowcount == 1

    async def revoke(self, datasource_id: str) -> None:
        await self.db.execute(
            update(ConnectionGrantModel)
            .where(
                ConnectionGrantModel.datasource_id == datasource_id,
                ConnectionGrantModel.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )

    async def add_grant(self, **values) -> None:
        self.db.add(ConnectionGrantModel(id=make_id("grant"), **values))
        await self.db.flush()

    async def bind_grant(
        self,
        *,
        token_hash: str,
        datasource_id: str,
        rebuild_key: str,
        build_id: str,
        schema_revision: int,
        connection_revision: int,
        now: datetime,
    ) -> ConnectionGrantModel | None:
        result = await self.db.execute(
            update(ConnectionGrantModel)
            .where(
                ConnectionGrantModel.token_hash == token_hash,
                ConnectionGrantModel.datasource_id == datasource_id,
                ConnectionGrantModel.rebuild_key == rebuild_key,
                ConnectionGrantModel.schema_revision == schema_revision,
                ConnectionGrantModel.connection_revision == connection_revision,
                ConnectionGrantModel.expires_at > now,
                ConnectionGrantModel.revoked_at.is_(None),
                or_(
                    ConnectionGrantModel.build_id.is_(None),
                    ConnectionGrantModel.build_id == build_id,
                ),
            )
            .values(build_id=build_id, consumed_at=now)
        )
        if result.rowcount != 1:
            return None
        return await self.db.scalar(
            select(ConnectionGrantModel).where(ConnectionGrantModel.token_hash == token_hash)
        )
