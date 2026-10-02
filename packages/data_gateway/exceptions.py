from __future__ import annotations

from data_gateway.types import SqlGuardIssue


class DataSourceCheckError(Exception):
    """表示可安全展示的数据源检查失败，不携带底层路径或异常文本。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable


class QueryTimeoutError(Exception):
    """表示 Adapter 已主动中断超过网关时限的只读查询。"""

    def __init__(self, message: str = "查询超时", audit_log_id: str | None = None) -> None:
        super().__init__(message)
        self.audit_log_id = audit_log_id


class QueryFailedError(Exception):
    """表示 Adapter 已返回可审计的执行失败，同一条 Audit 必须带上稳定码。"""

    def __init__(
        self,
        message: str = "查询执行失败",
        audit_log_id: str | None = None,
        *,
        code: str = "QUERY_FAILED",
        hint: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.audit_log_id = audit_log_id
        self.code = code
        self.hint = hint
        self.retryable = retryable


class QueryCanceledError(Exception):
    """表示查询收到内部停止请求，调用方应把同一条 Audit 标记为 canceled。"""


class SqlGuardBlockedError(Exception):
    """表示 SQL 在执行前被 Guard 阻断。"""

    def __init__(self, issue: SqlGuardIssue, audit_log_id: str) -> None:
        super().__init__(issue.message)
        self.code = issue.reason_code
        self.message = issue.message
        self.audit_log_id = audit_log_id
        self.issue = issue
