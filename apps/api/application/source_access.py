"""Resolve private source inputs without constructing Gateway or analysis resources."""

from pathlib import Path, PurePosixPath

from contracts.datasources import MySqlConnectionConfig
from data_gateway.exceptions import DataSourceCheckError
from data_gateway.types import FileSourceAccess, RelationalSourceAccess, SourceAccess
from metadata.models import DataSourceModel
from metadata.repositories import SecretRepository
from sqlalchemy.ext.asyncio import AsyncSession

from application.secret_cipher import SecretCipher

CONNECTION_CONFIG = {"mysql": MySqlConnectionConfig}


async def resolve_source_access(
    db: AsyncSession,
    cipher: SecretCipher | None,
    source: DataSourceModel,
    datasource_root: Path,
) -> SourceAccess:
    """Return process-local access only; never serialize the result into a Run."""

    if source.source_kind == "connection":
        config_type = CONNECTION_CONFIG.get(source.type)
        if config_type is None or not source.credential_ref or cipher is None:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源连接配置不可用")
        config = config_type.model_validate(source.connection_config_json)
        secret = await SecretRepository(db).get(source.credential_ref)
        if secret is None:
            raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源凭据不可用")
        return RelationalSourceAccess(
            **config.model_dump(), password=cipher.decrypt(secret.encrypted_value)
        )
    reference = source.source_ref
    if not reference:
        raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件不存在")
    relative = PurePosixPath(reference)
    root = datasource_root.resolve()
    if relative.is_absolute() or ".." in relative.parts or "\\" in reference:
        raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件引用无效")
    path = (root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise DataSourceCheckError("DATASOURCE_CHECK_FAILED", "数据源文件不存在")
    return FileSourceAccess(path=path)
