"""Run SSE 回放：数据库事件是唯一事实，内存对象只负责唤醒。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Final

from contracts.errors import AppError, ErrorCode
from contracts.run_events import RunEventRead, RunEventType
from contracts.status import RunStatus
from metadata.repositories import RunRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.run_event_pipeline import RunEventNotifier, RunEventPipeline

_EVENT_BATCH_SIZE: Final = 200
_HEARTBEAT_SECONDS: Final = 15
_TERMINAL_EVENTS: Final = frozenset(
    {
        RunEventType.RUN_SUCCEEDED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELED,
    }
)
_TERMINAL_STATUSES: Final = frozenset(
    {
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELED.value,
    }
)


class RunEventStream:
    """只读生成单个 Run 的 SSE；不会调用模型、网关、DataLink 或 Sandbox。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: RunEventNotifier,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier

    async def ensure_run_exists(self, run_id: str) -> None:
        """在建立长连接前给出稳定的 404，而不是挂起不存在的 Run。"""

        async with self._session_factory() as db:
            if await RunRepository(db).get(run_id) is None:
                raise AppError(ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404)

    async def list_after(self, run_id: str, *, after_seq: int) -> list[RunEventRead]:
        """读取同一账本的有限窗口，供非 SSE 客户端按序补发。"""

        records, _ = await self._read_after(run_id, after_seq=after_seq)
        return records

    async def events(self, run_id: str, *, after_seq: int) -> AsyncIterator[str]:
        """先补历史，再在已建立的通知窗口二次读取，避免提交和订阅之间漏事件。"""

        cursor = after_seq
        while True:
            records, terminal = await self._read_after(run_id, after_seq=cursor)
            if records:
                for record in records:
                    cursor = record.seq
                    yield _encode_event(record)
                    if record.type in _TERMINAL_EVENTS:
                        return
                continue
            if terminal:
                return

            # 先持有通知锁，再补读一次数据库。提交刚好发生在两次读取之间时，
            # 这里仍能读到事件；否则再进入 condition.wait，不会丢掉通知。
            async with self._notifier.subscription(run_id) as subscription:
                records, terminal = await self._read_after(run_id, after_seq=cursor)
                if not records and not terminal:
                    notified = await subscription.wait(timeout_seconds=_HEARTBEAT_SECONDS)
                else:
                    notified = True

            if records:
                for record in records:
                    cursor = record.seq
                    yield _encode_event(record)
                    if record.type in _TERMINAL_EVENTS:
                        return
                continue
            if terminal:
                return
            if not notified:
                # SSE 注释不进入 RunEvent，不占 seq，也不会污染历史回放。
                yield ": heartbeat\n\n"

    async def _read_after(self, run_id: str, *, after_seq: int) -> tuple[list[RunEventRead], bool]:
        """每次发送前都重新从 Metadata 读取，通知本身不承载事件内容。"""

        async with self._session_factory() as db:
            pipeline = RunEventPipeline(db)
            records = await pipeline.list_after(
                run_id,
                after_seq=after_seq,
                limit=_EVENT_BATCH_SIZE,
            )
            run = await RunRepository(db).get(run_id)
            return records, run is None or run.status in _TERMINAL_STATUSES


def _encode_event(event: RunEventRead) -> str:
    """编码最小 SSE 帧；payload 已在写入前按事件白名单收紧。"""

    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type.value}\ndata: {data}\n\n"
