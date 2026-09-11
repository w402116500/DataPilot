from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitiveFieldPolicy:
    """只按数据源所有者确认的列名遮蔽数据。

    过去这里会猜测列名和样例格式，导致同一列在不同结果中出现不一致的
    判断。现在策略是显式配置：空集合也必须是用户确认后的结果。
    """

    mask_value: str = "***"
    mask_fields: frozenset[str] = frozenset()
    confirmed: bool = False

    def is_sensitive_field(self, field_name: str) -> bool:
        """判断列名是否在用户明确保存的遮蔽清单中。"""

        normalized_name = field_name.casefold()
        return any(normalized_name == configured.casefold() for configured in self.mask_fields)

    def is_sensitive_values(self, field_name: str, values: Iterable[object]) -> bool:
        """兼容 Data Gateway 的列扫描入口，但不再读取值样例做猜测。"""

        del values
        return self.is_sensitive_field(field_name)

    def sensitive_fields_for_rows(
        self, field_names: Sequence[str], rows: Sequence[Sequence[object]]
    ) -> frozenset[str]:
        """返回用户配置的遮蔽列，参数保留以兼容序列化器的统一调用。"""

        del rows
        return frozenset(
            field_name for field_name in field_names if self.is_sensitive_field(field_name)
        )

    def mask_if_sensitive(self, field_name: str, value: object) -> object:
        """保留空值，其余命中敏感规则的字段替换成统一掩码。"""

        if value is None or not self.is_sensitive_field(field_name):
            return value
        return self.mask_value
