"""单进程 Run 取消句柄注册表。"""

from __future__ import annotations

import asyncio


class RunCancellation:
    """供模型、Data Gateway、DataLink 和 Sandbox 统一查询的取消信号。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class RunCancelRegistry:
    """进程内取消信号表；重启后不恢复，改由 RunRecovery 失败收尾。"""

    def __init__(self) -> None:
        self._signals: dict[str, RunCancellation] = {}

    def register(self, run_id: str) -> RunCancellation:
        """注册或复用当前任务的取消信号，避免重复调度产生两套句柄。"""

        return self._signals.setdefault(run_id, RunCancellation())

    def cancel(self, run_id: str) -> bool:
        """尽力通知当前进程的执行任务；数据库取消事实仍由服务另行保存。"""

        signal = self._signals.get(run_id)
        if signal is None:
            return False
        signal.cancel()
        return True

    def unregister(self, run_id: str) -> None:
        """Run 收尾后释放内存句柄，终态历史不依赖此注册表。"""

        self._signals.pop(run_id, None)
