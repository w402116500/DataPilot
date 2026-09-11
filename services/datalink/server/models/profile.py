"""字段画像的内部模型与安全的共享契约转换。"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.datalink import DataLinkColumnProfileRead, DataLinkScalar


@dataclass(frozen=True)
class ColumnProfile:
    """单个字段的有限统计画像，敏感字段不保存任何实际取值。"""

    id: str
    column_id: str
    column_name: str
    dtype: str
    null_rate: float
    distinct_count: int
    unique_rate: float
    top_values: tuple[DataLinkScalar, ...]
    sample_values: tuple[DataLinkScalar, ...]
    min_value: DataLinkScalar | None = None
    max_value: DataLinkScalar | None = None
    semantic_type: str | None = None
    is_sensitive: bool = False

    def to_read(self) -> DataLinkColumnProfileRead:
        """转换为对 REST、MCP 和模型都可见的脱敏画像形状。"""

        return DataLinkColumnProfileRead(
            column_id=self.column_id,
            dtype=self.dtype,
            semantic_type=self.semantic_type,
            null_rate=self.null_rate,
            distinct_count=self.distinct_count,
            unique_rate=self.unique_rate,
            min_value=self.min_value,
            max_value=self.max_value,
            top_values=list(self.top_values),
            sample_values=list(self.sample_values),
        )
