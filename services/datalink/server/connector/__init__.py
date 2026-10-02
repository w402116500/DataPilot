"""DataLink 受控 CSV/SQLite 只读 Connector。"""

from pathlib import Path

from contracts.datalink import DataLinkErrorCode
from contracts.status import DataSourceType

from server.connector.base import ConnectorError, DatasourceInfo, ReadOnlyConnector
from server.connector.csv import CsvConnector
from server.connector.grants import ConnectionGrantResolver
from server.connector.mysql import MySqlConnector
from server.connector.paths import resolve_source_path
from server.connector.sqlite import SqliteConnector


def create_connector(
    source_type: DataSourceType,
    source_path: Path,
    datasource_id: str,
    schema_revision: int,
) -> ReadOnlyConnector:
    """按已校验的数据源类型创建唯一允许的只读 Connector。"""

    if source_type == DataSourceType.CSV:
        return CsvConnector(source_path, datasource_id, schema_revision)
    if source_type == DataSourceType.SQLITE:
        return SqliteConnector(source_path, datasource_id, schema_revision)
    raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "Source type is unsupported")


__all__ = [
    "ConnectorError",
    "DatasourceInfo",
    "ReadOnlyConnector",
    "create_connector",
    "resolve_source_path",
    "MySqlConnector",
    "ConnectionGrantResolver",
]
