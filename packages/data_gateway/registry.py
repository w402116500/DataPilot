from __future__ import annotations

from contracts.datasources import DataSourceTypeDescriptor
from contracts.errors import AppError, ErrorCode
from contracts.status import DataSourceType

from data_gateway.adapters import CsvAdapter, SqliteAdapter
from data_gateway.mysql_adapter import MySqlAdapter
from data_gateway.types import DataSourceAdapter, SourceHandle

_TYPE_DESCRIPTORS = (
    DataSourceTypeDescriptor(
        type=DataSourceType.CSV,
        label="CSV",
        description="单表文件数据源",
        enabled=True,
        accepted_extensions=[".csv"],
        dialect="duckdb",
        upload_mode="file",
        capabilities=dict(
            schema=True, preview=True, readonly_sql=True, datalink=True, python_snapshot=True
        ),
    ),
    DataSourceTypeDescriptor(
        type=DataSourceType.SQLITE,
        label="SQLite",
        description="本地 SQLite 数据库文件",
        enabled=True,
        accepted_extensions=[".sqlite", ".db"],
        dialect="sqlite",
        upload_mode="file",
        capabilities=dict(
            schema=True, preview=True, readonly_sql=True, datalink=True, python_snapshot=True
        ),
    ),
    DataSourceTypeDescriptor(
        type=DataSourceType.MYSQL,
        label="MySQL",
        description="MySQL 关系型数据库",
        enabled=True,
        accepted_extensions=[],
        dialect="mysql",
        upload_mode="connection",
        parameters=[
            dict(name="host", label="主机"),
            dict(name="port", label="端口", type="integer", default=3306),
            dict(name="database", label="数据库"),
            dict(name="username", label="用户名"),
            dict(name="password", label="密码", secret=True),
            dict(name="tls", label="验证 TLS", type="boolean", default=True),
            dict(
                name="connect_timeout_seconds", label="连接超时（秒）", type="integer", default=10
            ),
        ],
        capabilities=dict(
            schema=True, preview=True, readonly_sql=True, datalink=True, python_snapshot=False
        ),
    ),
)


def supported_data_source_types() -> list[DataSourceTypeDescriptor]:
    """返回产品层允许展示和创建的类型，不泄露 Registry 的类名。"""

    return list(_TYPE_DESCRIPTORS)


def get_type_descriptor(type_name: str) -> DataSourceTypeDescriptor:
    """按用户选择的类型查受控声明，未知或停用类型一律拒绝。"""

    for descriptor in _TYPE_DESCRIPTORS:
        if descriptor.type.value == type_name and descriptor.enabled:
            return descriptor
    raise AppError(
        ErrorCode.UNSUPPORTED_FILE_TYPE,
        "不支持的文件类型",
        status_code=422,
    )


class AdapterRegistry:
    """只根据 Metadata 已验证的类型创建 Adapter，不接收模块名或用户路径。"""

    def create(self, source: SourceHandle) -> DataSourceAdapter:
        if source.datasource_type is DataSourceType.CSV:
            return CsvAdapter(source)
        if source.datasource_type is DataSourceType.SQLITE:
            return SqliteAdapter(source)
        if source.datasource_type is DataSourceType.MYSQL:
            return MySqlAdapter(source)
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            "不支持的文件类型",
            status_code=422,
        )
