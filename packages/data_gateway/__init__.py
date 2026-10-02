"""Data Gateway package: Schema、预览和安全查询的唯一数据访问边界。"""

from data_gateway.registry import AdapterRegistry, supported_data_source_types
from data_gateway.service import DataGateway
from data_gateway.types import QueryCancelToken

__all__ = ["AdapterRegistry", "DataGateway", "QueryCancelToken", "supported_data_source_types"]
