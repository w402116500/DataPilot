from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _upgrade(settings) -> str:
    database_url = settings.metadata_database_url.replace("+aiosqlite", "")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return database_url


def test_core_migration_removes_fixed_step_schema(settings) -> None:
    database_url = _upgrade(settings)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "run_steps" not in tables

        run_columns = {column["name"] for column in inspector.get_columns("runs")}
        assert {"config_snapshot_json", "analysis_plan_json", "current_step_id"}.isdisjoint(
            run_columns
        )

        for table in ("run_events", "tool_calls", "artifacts"):
            columns = {column["name"] for column in inspector.get_columns(table)}
            assert "step_id" not in columns

        audit_columns = {column["name"] for column in inspector.get_columns("sql_audit_logs")}
        assert "tool_call_id" in audit_columns
    finally:
        engine.dispose()
