from __future__ import annotations

import pytest
from agent_runtime import observability


class _RecordingRun:
    def __init__(self) -> None:
        self.tags = ["datapilot"]
        self.ended: list[dict[str, object]] = []

    def end(self, **kwargs) -> None:
        self.ended.append(kwargs)


class _RecordingTrace:
    def __init__(self, run: _RecordingRun) -> None:
        self.run = run
        self.exited = False

    async def __aenter__(self):
        return self.run

    async def __aexit__(self, *_args):
        self.exited = True


@pytest.mark.asyncio
async def test_runtime_trace_records_only_safe_metadata(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    run = _RecordingRun()
    manager = _RecordingTrace(run)
    captured: dict[str, object] = {}

    def fake_trace(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return manager

    monkeypatch.setattr(observability, "trace", fake_trace)

    async with observability.runtime_trace(
        "datapilot.run.opening",
        run_id="run_1",
        session_id="session_1",
        phase="run_opening",
        run_type="llm",
    ) as span:
        span.finish(
            protocol_id="general-task",
            opening_model_calls=1,
            opening_repair_calls=0,
        )

    assert captured["name"] == "datapilot.run.opening"
    assert captured["inputs"] == {}
    assert captured["metadata"] == {
        "run_id": "run_1",
        "session_id": "session_1",
        "thread_id": "session_1",
        "datapilot_session_id": "session_1",
        "phase": "run_opening",
    }
    assert run.ended[0]["error"] is None
    metadata = run.ended[0]["metadata"]
    assert metadata["opening_model_calls"] == 1
    assert metadata["opening_repair_calls"] == 0
    assert "protocol:general-task" in run.tags
    assert manager.exited is True


@pytest.mark.asyncio
async def test_runtime_trace_failure_never_blocks_business(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    def broken_trace(*_args, **_kwargs):
        raise RuntimeError("langsmith unavailable")

    monkeypatch.setattr(observability, "trace", broken_trace)

    reached = False
    async with observability.runtime_trace(
        "datapilot.run.agent",
        run_id="run_1",
        session_id="session_1",
        phase="agent",
    ):
        reached = True

    assert reached is True
