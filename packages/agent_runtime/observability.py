"""LangSmith runtime spans for DataPilot Run phases."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from langsmith import trace

type TraceMetadataValue = str | int | float | bool | None


@dataclass
class RuntimeTraceSpan:
    """Small safe facade over LangSmith's RunTree."""

    run: Any | None
    started_at: float
    phase: str
    finished: bool = False

    def finish(
        self,
        *,
        error_code: str | None = None,
        protocol_id: str | None = None,
        **metadata: TraceMetadataValue,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        if self.run is None:
            return
        safe_metadata: dict[str, TraceMetadataValue] = {
            "phase": self.phase,
            "duration_ms": max(0, round((time.perf_counter() - self.started_at) * 1_000)),
            **metadata,
        }
        if protocol_id is not None:
            safe_metadata["protocol_id"] = protocol_id
            self.run.tags = sorted({*(self.run.tags or []), f"protocol:{protocol_id}"})
        if error_code is not None:
            safe_metadata["error_code"] = error_code
        self.run.end(error=error_code, metadata=safe_metadata)


@asynccontextmanager
async def runtime_trace(
    name: str,
    *,
    run_id: str,
    session_id: str,
    phase: str,
    run_type: str = "chain",
    protocol_id: str | None = None,
    **metadata: TraceMetadataValue,
) -> AsyncIterator[RuntimeTraceSpan]:
    """Create a Run phase span; LangSmith's environment switch controls tracing."""

    manager = None
    span = RuntimeTraceSpan(run=None, started_at=time.perf_counter(), phase=phase)
    tags = ["datapilot", "run-protocol", f"phase:{phase}"]
    if protocol_id is not None:
        tags.append(f"protocol:{protocol_id}")
    try:
        manager = trace(
            name,
            run_type=run_type,
            inputs={},
            tags=tags,
            metadata={
                "run_id": run_id,
                "session_id": session_id,
                "thread_id": session_id,
                "datapilot_session_id": session_id,
                "phase": phase,
                **({"protocol_id": protocol_id} if protocol_id is not None else {}),
                **metadata,
            },
        )
        span.run = await manager.__aenter__()
    except Exception:
        manager = None
        span.run = None

    try:
        yield span
    except BaseException:
        span.finish(error_code="RUNTIME_PHASE_FAILED", protocol_id=protocol_id)
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass
        raise
    else:
        span.finish(protocol_id=protocol_id)
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass
