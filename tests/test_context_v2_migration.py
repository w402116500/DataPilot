from __future__ import annotations

import importlib
import io

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from server.config import Settings
from sqlalchemy import create_engine, inspect

from tests.conftest import sync_sqlite_url

_REVISION = "0024_preserve_historical_summary_owners"
_PREVIOUS_REVISION = "0021_pending_context_snapshot"
_SUMMARY_INDEX = "ix_historical_answer_summaries_session_datasource_finished"
_SUMMARY_INDEX_COLUMNS = [
    "session_id",
    "datasource_id",
    "source_run_finished_at",
    "source_run_id",
]


def _config(settings: Settings) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", sync_sqlite_url(settings.metadata_database_url))
    return config


def test_context_v2_migration_upgrades_empty_database(settings: Settings) -> None:
    command.upgrade(_config(settings), "head")

    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "conversation_summaries" not in tables
        assert "historical_answer_summaries" in tables

        columns = {
            column["name"]: column
            for column in inspector.get_columns("historical_answer_summaries")
        }
        assert columns["source_run_finished_at"]["nullable"] is False
        assert columns["provenance"]["nullable"] is False

        tool_columns = {column["name"]: column for column in inspector.get_columns("tool_calls")}
        assert tool_columns["tool_call_id"]["nullable"] is False

        indexes = {
            index["name"]: index for index in inspector.get_indexes("historical_answer_summaries")
        }
        assert indexes[_SUMMARY_INDEX]["column_names"] == _SUMMARY_INDEX_COLUMNS
    finally:
        engine.dispose()


def test_context_v2_migration_upgrades_existing_0021_summary(settings: Settings) -> None:
    config = _config(settings)
    command.upgrade(config, _PREVIOUS_REVISION)

    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    try:
        assert "conversation_summaries" in set(inspect(engine).get_table_names())
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql(
                "INSERT INTO data_sources "
                "(id, name, type, source_ref, created_at, updated_at) "
                "VALUES ('datasource_1', 'Orders', 'sqlite', 'datasource_1/source.sqlite', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO sessions (id, title, selected_datasource_id, created_at, updated_at) "
                "VALUES ('session_1', 'Migration test', 'datasource_1', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO runs "
                "(id, session_id, datasource_id, question, status, finished_at, "
                "created_at, updated_at) "
                "VALUES ('run_1', 'session_1', 'datasource_1', 'How many orders?', 'succeeded', "
                "'2026-08-31 12:34:56', '2026-08-31 12:00:00', CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO historical_answer_summaries "
                "(id, session_id, datasource_id, source_run_id, topic, content_text, "
                "data_freshness, created_at, updated_at) "
                "VALUES ('history_1', 'session_1', 'datasource_1', 'run_1', 'Order count', "
                "'Historical answer', 'historical_not_current', "
                "'2026-08-31 12:35:00', '2026-08-31 12:35:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO conversation_summaries "
                "(session_id, datasource_id, through_message_position, content_text, "
                "created_at, updated_at) "
                "VALUES ('session_1', 'datasource_1', 1, 'Legacy mixed summary', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION)

    engine = create_engine(sync_sqlite_url(settings.metadata_database_url))
    try:
        inspector = inspect(engine)
        assert "conversation_summaries" not in set(inspector.get_table_names())
        columns = {
            column["name"]: column
            for column in inspector.get_columns("historical_answer_summaries")
        }
        assert columns["source_run_finished_at"]["nullable"] is False
        assert columns["provenance"]["nullable"] is False
        indexes = {
            index["name"]: index for index in inspector.get_indexes("historical_answer_summaries")
        }
        assert indexes[_SUMMARY_INDEX]["column_names"] == _SUMMARY_INDEX_COLUMNS

        foreign_keys = inspector.get_foreign_keys("historical_answer_summaries")
        source_run_foreign_key = next(
            foreign_key
            for foreign_key in foreign_keys
            if foreign_key["constrained_columns"] == ["source_run_id"]
        )
        assert source_run_foreign_key["options"] == {"ondelete": "RESTRICT"}
        owner_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["options"]
            for foreign_key in foreign_keys
            if tuple(foreign_key["constrained_columns"]) in {("session_id",), ("datasource_id",)}
        }
        assert owner_foreign_keys == {
            ("session_id",): {"ondelete": "RESTRICT"},
            ("datasource_id",): {"ondelete": "RESTRICT"},
        }

        with engine.connect() as connection:
            migrated = connection.exec_driver_sql(
                "SELECT source_run_finished_at FROM historical_answer_summaries "
                "WHERE source_run_id = 'run_1'"
            ).scalar_one()
        assert str(migrated).startswith("2026-08-31 12:34:56")
    finally:
        engine.dispose()


def test_context_v2_non_sql_ddl_sets_not_null_and_rebuilds_stable_index(
    monkeypatch,
) -> None:
    migration = importlib.import_module("migrations.versions.0022_context_v2_diagnostics")
    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    sql = " ".join(output.getvalue().lower().split())
    assert (
        "alter table historical_answer_summaries alter column "
        "source_run_finished_at set not null" in sql
    )
    assert f"drop index {_SUMMARY_INDEX.lower()}" in sql
    assert (
        f"create index {_SUMMARY_INDEX.lower()} on historical_answer_summaries "
        "(session_id, datasource_id, source_run_finished_at, source_run_id)" in sql
    )
    assert "drop table conversation_summaries" in sql
