from __future__ import annotations

import pytest
from application.script_workspace import (
    ScriptWorkspaceError,
    ScriptWorkspaceManager,
    SessionWorkspaceManager,
)


async def _manager(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    manager = ScriptWorkspaceManager(
        datasource_root=tmp_path / "sources",
        workspace_root=tmp_path / "workspaces",
        runtime_trace_root=tmp_path / "traces",
    )
    return manager, source


@pytest.mark.asyncio
async def test_run_workspace_has_readonly_input_and_no_two_stage_directories(tmp_path) -> None:
    manager, source = await _manager(tmp_path)
    workspace = await manager.create(run_id="run_1", input_snapshot_path=str(source))

    assert workspace.input_file is not None
    assert workspace.input_file.read_text(encoding="utf-8") == "value\n1\n"
    assert workspace.input_file.stat().st_mode & 0o222 == 0
    assert workspace.outputs_dir.is_dir()
    assert workspace.charts_dir.is_dir()
    assert not (workspace.work_dir / "raw_results").exists()
    assert not (workspace.work_dir / "safe_results").exists()


@pytest.mark.asyncio
async def test_run_workspace_create_without_snapshot_keeps_empty_input(tmp_path) -> None:
    manager, _source = await _manager(tmp_path)
    workspace = await manager.create(run_id="run_empty_input")

    assert workspace.input_file is None
    assert workspace.input_dir.is_dir()
    assert list(workspace.input_dir.iterdir()) == []
    assert workspace.work_dir.is_dir()
    assert workspace.outputs_dir.is_dir()
    assert workspace.charts_dir.is_dir()

    await manager.cleanup(workspace.workspace_id)

    assert not workspace.root.exists()


@pytest.mark.asyncio
async def test_open_still_copies_source_ref_into_readonly_input(tmp_path) -> None:
    manager, _source = await _manager(tmp_path)
    stored = tmp_path / "sources" / "source.csv"
    stored.parent.mkdir()
    stored.write_text("value\n1\n", encoding="utf-8")

    async with manager.open(run_id="run_open", source_ref="source.csv") as workspace:
        assert workspace.input_file is not None
        assert workspace.input_file.read_text(encoding="utf-8") == "value\n1\n"
        assert workspace.input_file.stat().st_mode & 0o222 == 0

    assert not workspace.root.exists()


@pytest.mark.asyncio
async def test_prepare_outputs_cleans_only_declared_files_and_rejects_paths(tmp_path) -> None:
    manager, source = await _manager(tmp_path)
    workspace = await manager.create(run_id="run_1", input_snapshot_path=str(source))
    await manager.prepare_outputs(workspace.workspace_id, ["outputs/result.csv", "report.md"])
    (workspace.outputs_dir / "result.csv").write_text("old", encoding="utf-8")
    workspace.report_path.write_text("old report", encoding="utf-8")

    await manager.prepare_outputs(workspace.workspace_id, ["outputs/result.csv"])

    assert (workspace.outputs_dir / "result.csv").read_text(encoding="utf-8") == ""
    assert workspace.report_path.read_text(encoding="utf-8") == "old report"
    with pytest.raises(ScriptWorkspaceError):
        await manager.prepare_outputs(workspace.workspace_id, ["../outside.csv"])
    with pytest.raises(ScriptWorkspaceError):
        await manager.prepare_outputs(workspace.workspace_id, ["outputs/result.html"])


@pytest.mark.asyncio
async def test_cleanup_keeps_only_internal_script_trace(tmp_path) -> None:
    manager, source = await _manager(tmp_path)
    workspace = await manager.create(run_id="run_1", input_snapshot_path=str(source))
    await manager.write_analysis_script(workspace.workspace_id, "print('ok')")

    await manager.cleanup(workspace.workspace_id)

    assert not workspace.root.exists()
    assert (tmp_path / "traces" / "run_1" / "analysis.py").read_text(
        encoding="utf-8"
    ) == "print('ok')"


@pytest.mark.asyncio
async def test_session_snapshot_is_relative_and_survives_across_run_manager_calls(tmp_path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    manager = SessionWorkspaceManager(tmp_path / "session-workspace")

    snapshot = await manager.create_input_snapshot(
        session_id="session_1",
        run_id="run_1",
        source_path=source,
    )
    resolved = await manager.resolve_input_snapshot(
        session_id="session_1",
        relative_ref=snapshot.relative_ref,
    )

    assert snapshot.relative_ref == "snapshots/run_1/source.csv"
    assert resolved == snapshot.path
    await manager.delete_session("session_1")
    with pytest.raises(ScriptWorkspaceError):
        await manager.resolve_input_snapshot(
            session_id="session_1",
            relative_ref=snapshot.relative_ref,
        )
