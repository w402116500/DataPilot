from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from application.artifacts import ArtifactStore
from contracts.api import ApiEnvelope
from contracts.datalink import DataLinkConnectionGrantRead
from contracts.datasources import (
    DataSourceRead,
    SchemaColumnRead,
    SchemaSummaryRead,
    SchemaTableRead,
    TableDataRead,
)
from contracts.errors import AppError
from contracts.sensitive_fields import SensitiveFieldPolicy
from contracts.status import DataSourceType
from data_gateway.adapters import CsvAdapter
from data_gateway.exceptions import QueryCanceledError, SqlGuardBlockedError
from data_gateway.registry import AdapterRegistry
from data_gateway.serialization import serialize_table_result
from data_gateway.sql_guard import guard_sql
from data_gateway.types import QueryCancelToken
from fastapi.encoders import jsonable_encoder
from metadata.database import create_session_factory, create_sqlite_engine
from metadata.repositories import DataSourceRepository
from server.dependencies import build_datasource_service
from server.responses import encode_utc_datetime


def unwrap(response):
    body = response.json()
    assert body["request_id"]
    assert body["error"] is None
    return body["data"]


def upload_csv(client, content: bytes, *, name: str = "Orders") -> dict[str, object]:
    response = client.post(
        "/datasources/upload",
        data={"type": "csv", "name": name},
        files={"file": ("orders.csv", content, "text/csv")},
    )
    assert response.status_code == 202
    return unwrap(response)


def _guard_schema(*column_names: str) -> SchemaSummaryRead:
    """构造不依赖上传流程的固定 Schema，直接验证 Guard AST 行为。"""

    return SchemaSummaryRead(
        datasource_id="datasource_guard",
        dialect="duckdb",
        tables=[
            {
                "name": "dataset",
                "columns": [
                    {"name": column_name, "type": "TEXT", "nullable": True}
                    for column_name in column_names
                ],
            }
        ],
    )


def test_supported_datasource_types_and_extension_validation(client, migrated_settings) -> None:
    supported = unwrap(client.get("/datasource-types"))
    assert supported["items"] == [
        {
            "type": "csv",
            "label": "CSV",
            "description": "单表文件数据源",
            "enabled": True,
            "accepted_extensions": [".csv"],
            "dialect": "duckdb",
            "upload_mode": "file",
        },
        {
            "type": "sqlite",
            "label": "SQLite",
            "description": "本地 SQLite 数据库文件",
            "enabled": True,
            "accepted_extensions": [".sqlite", ".db"],
            "dialect": "sqlite",
            "upload_mode": "file",
        },
        {
            "type": "mysql",
            "label": "MySQL",
            "description": "MySQL 关系型数据库",
            "enabled": True,
            "accepted_extensions": [],
            "dialect": "mysql",
            "upload_mode": "connection",
            "parameters": [
                {
                    "name": "host",
                    "label": "主机",
                    "type": "string",
                    "required": True,
                    "default": None,
                    "secret": False,
                },
                {
                    "name": "port",
                    "label": "端口",
                    "type": "integer",
                    "required": True,
                    "default": 3306,
                    "secret": False,
                },
                {
                    "name": "database",
                    "label": "数据库",
                    "type": "string",
                    "required": True,
                    "default": None,
                    "secret": False,
                },
                {
                    "name": "username",
                    "label": "用户名",
                    "type": "string",
                    "required": True,
                    "default": None,
                    "secret": False,
                },
                {
                    "name": "password",
                    "label": "密码",
                    "type": "string",
                    "required": True,
                    "default": None,
                    "secret": True,
                },
                {
                    "name": "tls",
                    "label": "验证 TLS",
                    "type": "boolean",
                    "required": True,
                    "default": True,
                    "secret": False,
                },
                {
                    "name": "connect_timeout_seconds",
                    "label": "连接超时（秒）",
                    "type": "integer",
                    "required": True,
                    "default": 10,
                    "secret": False,
                },
            ],
            "capabilities": {
                "schema": True,
                "preview": True,
                "readonly_sql": True,
                "datalink": True,
                "python_snapshot": False,
            },
        },
    ]

    invalid = client.post(
        "/datasources/upload",
        data={"type": "csv", "name": "wrong extension"},
        files={"file": ("orders.db", b"SQLite format 3\x00", "application/octet-stream")},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert list(migrated_settings.datasource_root.iterdir()) == []

    missing_type = client.post(
        "/datasources/upload",
        data={"name": "missing type"},
        files={"file": ("orders.csv", b"id\n1\n", "text/csv")},
    )
    assert missing_type.status_code == 422
    assert list(migrated_settings.datasource_root.iterdir()) == []


def test_connection_grant_json_keeps_password_and_public_read_does_not() -> None:
    password = "test-only-mysql-password"
    grant = DataLinkConnectionGrantRead(
        datasource_id="datasource_test",
        schema_revision=1,
        connection_revision=1,
        config={
            "host": "127.0.0.1",
            "port": 3306,
            "database": "demo",
            "username": "reader",
            "tls": False,
        },
        password=password,
        schema={
            "datasource_id": "datasource_test",
            "dialect": "mysql",
            "tables": [],
        },
    )
    encoded = jsonable_encoder(
        ApiEnvelope(data=grant, request_id="req_test", error=None),
        custom_encoder={datetime: encode_utc_datetime},
    )
    assert encoded["data"]["password"] == password
    roundtrip = DataLinkConnectionGrantRead.model_validate(encoded["data"])
    assert roundtrip.password.get_secret_value() == password

    public = jsonable_encoder(
        DataSourceRead(
            id="datasource_test",
            name="MySQL public",
            description=None,
            type="mysql",
            status="schema_ready",
            schema_revision=1,
            source_kind="connection",
            connection_revision=1,
            has_credentials=True,
            connection_summary={
                "host": "127.0.0.1",
                "port": 3306,
                "database": "demo",
                "username": "reader",
                "tls": False,
            },
            datalink_build_id=None,
            datalink_graph_version=None,
            last_error_code=None,
            last_error_message=None,
            last_test_at=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    assert public["has_credentials"] is True
    assert "password" not in public
    assert "credential_ref" not in public
    assert password not in json.dumps(public)


def test_csv_upload_inspects_schema_without_guessing_mask_fields(client) -> None:
    uploaded = upload_csv(
        client,
        b"order_id,amount,customer_email,code\n1,10.20,a@example.com,00123\n2,20.30,b@example.com,2e3\n",
    )
    assert uploaded["status"] == "inspecting"
    datasource_id = uploaded["id"]

    detail = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert detail["status"] == "schema_ready"
    assert detail["schema_revision"] == 1
    assert "source_ref" not in detail
    assert "schema" in detail
    assert "schema_summary" not in detail

    schema = unwrap(client.get(f"/datasources/{datasource_id}/schema"))
    columns = {column["name"]: column for column in schema["tables"][0]["columns"]}
    assert columns["order_id"]["type"] == "HUGEINT"
    assert columns["amount"]["type"] == "DECIMAL(4,2)"
    assert columns["code"]["type"] == "TEXT"

    preview_url = f"/datasources/{datasource_id}/tables/dataset/preview?limit=999"
    preview = unwrap(client.get(preview_url))
    assert preview["row_count"] == 2
    assert preview["rows"][0] == [1, "10.20", "a@example.com", "00123"]

    masked = unwrap(
        client.patch(
            f"/datasources/{datasource_id}/mask-fields",
            json={"mask_fields": ["customer_email"]},
        )
    )
    assert masked["mask_fields"] == ["customer_email"]
    assert masked["mask_fields_confirmed"] is True
    preview_after_confirmation = unwrap(client.get(preview_url))
    assert preview_after_confirmation["rows"][0] == [1, "10.20", "***", "00123"]


def test_mask_fields_match_schema_columns_case_insensitively(client) -> None:
    uploaded = upload_csv(
        client,
        b"phone,email\n13800138000,buyer@example.com\n",
        name="Case-insensitive mask fields",
    )
    datasource_id = uploaded["id"]

    masked = unwrap(
        client.patch(
            f"/datasources/{datasource_id}/mask-fields",
            json={"mask_fields": ["PHONE"]},
        )
    )

    assert masked["mask_fields"] == ["PHONE"]
    preview = unwrap(client.get(f"/datasources/{datasource_id}/tables/dataset/preview"))
    assert preview["rows"] == [["***", "buyer@example.com"]]


def test_invalid_csv_is_retained_as_failed_datasource(client, migrated_settings) -> None:
    uploaded = upload_csv(client, b"Name,name\nAda,Ada\n", name="Invalid headers")
    datasource_id = uploaded["id"]

    detail = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert detail["status"] == "failed"
    assert detail["last_error_code"] == "CSV_HEADER_INVALID"
    assert (migrated_settings.datasource_root / datasource_id / "source.csv").is_file()


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"first,second\n1,2,3\n", "CSV_FORMAT_INVALID"),
        (b"\n", "CSV_HEADER_INVALID"),
        (b"first\n\xff\n", "CSV_ENCODING_INVALID"),
    ],
)
def test_csv_check_reports_stable_format_errors(client, content: bytes, expected_code: str) -> None:
    uploaded = upload_csv(client, content, name="Broken CSV")
    detail = unwrap(client.get(f"/datasources/{uploaded['id']}"))
    assert detail["status"] == "failed"
    assert detail["last_error_code"] == expected_code


@pytest.mark.parametrize(
    "content",
    [
        b"only_header\n",
        b"\xef\xbb\xbfid\n1\n",
    ],
)
def test_csv_allows_header_only_files_and_utf8_bom(client, content: bytes) -> None:
    uploaded = upload_csv(client, content, name="CSV edge cases")
    datasource_id = uploaded["id"]

    detail = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert detail["status"] == "schema_ready"
    preview = unwrap(client.get(f"/datasources/{datasource_id}/tables/dataset/preview"))
    if content == b"only_header\n":
        assert preview == {"columns": ["only_header"], "rows": [], "row_count": 0}
    else:
        assert preview == {"columns": ["id"], "rows": [[1]], "row_count": 1}


def test_csv_normalizes_short_rows_and_keeps_strict_column_types(client) -> None:
    uploaded = upload_csv(
        client,
        (
            b" identifier ,integer,decimal,calendar_day,local_time,global_time,leading,scientific,"
            b"thousands,mixed,boolean_value,empty\n"
            b"alpha,1,10.20,2026-08-11,2026-08-11T08:00:00,2026-08-11T08:00:00Z,001,1e3,"
            b'"1,000",one,true,\n'
            b"beta,2,20.30,2026-08-12,2026-08-12 09:30:00,2026-08-12T09:30:00+00:00,01,2e3,"
            b'"2,000",2,false,\n'
        ),
        name="Strict CSV types",
    )
    datasource_id = uploaded["id"]

    schema = unwrap(client.get(f"/datasources/{datasource_id}/schema"))
    columns = {column["name"]: column for column in schema["tables"][0]["columns"]}
    assert columns["identifier"]["type"] == "TEXT"
    assert columns["integer"]["type"] == "HUGEINT"
    assert columns["decimal"]["type"] == "DECIMAL(4,2)"
    assert columns["calendar_day"]["type"] == "DATE"
    assert columns["local_time"]["type"] == "TIMESTAMP"
    assert columns["global_time"]["type"] == "TIMESTAMPTZ"
    assert {columns[name]["type"] for name in ["leading", "scientific", "thousands", "mixed"]} == {
        "TEXT"
    }
    assert columns["boolean_value"]["type"] == "TEXT"
    assert columns["empty"] == {"name": "empty", "type": "TEXT", "nullable": True}


def test_csv_decimal_schema_reserves_integer_digits_when_scale_peaks_elsewhere(client) -> None:
    uploaded = upload_csv(
        client,
        b"value\n11.35040654\n0.123456789\n",
        name="Decimal precision boundary",
    )

    schema = unwrap(client.get(f"/datasources/{uploaded['id']}/schema"))
    column = schema["tables"][0]["columns"][0]
    assert column["type"] == "DECIMAL(11,9)"

    preview = unwrap(client.get(f"/datasources/{uploaded['id']}/tables/dataset/preview"))
    assert preview["rows"] == [["11.350406540"], ["0.123456789"]]


def test_csv_treats_short_rows_as_nulls_and_ignores_blank_rows(client) -> None:
    uploaded = upload_csv(client, b"id,label\n1,Ada\n2\n  ,  \n", name="Short rows")
    preview = unwrap(client.get(f"/datasources/{uploaded['id']}/tables/dataset/preview"))
    assert preview == {
        "columns": ["id", "label"],
        "rows": [[1, "Ada"], [2, None]],
        "row_count": 2,
    }


def test_upload_size_failure_keeps_no_file_or_metadata(client, migrated_settings) -> None:
    migrated_settings.max_upload_mb = 0
    response = client.post(
        "/datasources/upload",
        data={"type": "csv", "name": "Too large"},
        files={"file": ("orders.csv", b"id\n1\n", "text/csv")},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert list(migrated_settings.datasource_root.iterdir()) == []


def test_retry_clears_error_and_schema_revision_only_changes_for_new_structure(
    client,
    migrated_settings,
) -> None:
    uploaded = upload_csv(client, b"Name,name\nAda,Ada\n", name="Retry schema")
    datasource_id = uploaded["id"]
    source = migrated_settings.datasource_root / datasource_id / "source.csv"
    source.write_bytes(b"id,name\n1,Ada\n")

    retried = unwrap(client.post(f"/datasources/{datasource_id}/test"))
    assert retried["status"] == "inspecting"
    ready = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert ready["status"] == "schema_ready"
    assert ready["schema_revision"] == 1
    assert ready["last_error_code"] is None
    assert ready["last_error_message"] is None
    first_test_at = ready["last_test_at"]

    client.post(f"/datasources/{datasource_id}/test")
    unchanged = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert unchanged["schema_revision"] == 1
    assert unchanged["last_test_at"] >= first_test_at

    source.write_bytes(b"id,name,region\n1,Ada,East\n")
    client.post(f"/datasources/{datasource_id}/test")
    changed = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert changed["schema_revision"] == 2
    assert [column["name"] for column in changed["schema"]["tables"][0]["columns"]] == [
        "id",
        "name",
        "region",
    ]


def test_sqlite_schema_and_blob_preview(client, tmp_path: Path) -> None:
    source = tmp_path / "example.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, payload BLOB, title TEXT)"
        )
        connection.execute("INSERT INTO documents (payload, title) VALUES (?, ?)", (b"abc", "Plan"))
        connection.execute("CREATE VIEW document_titles AS SELECT title FROM documents")

    with source.open("rb") as handle:
        uploaded = client.post(
            "/datasources/upload",
            data={"type": "sqlite", "name": "Documents"},
            files={"file": ("example.sqlite", handle, "application/octet-stream")},
        )
    datasource = unwrap(uploaded)
    datasource_id = datasource["id"]

    schema = unwrap(client.get(f"/datasources/{datasource_id}/schema"))
    assert [table["name"] for table in schema["tables"]] == ["documents"]
    preview = unwrap(client.get(f"/datasources/{datasource_id}/tables/documents/preview"))
    assert preview["rows"] == [[1, "[二进制数据，3 字节]", "Plan"]]


def test_sqlite_only_exposes_user_tables_and_allows_empty_tables(client, tmp_path: Path) -> None:
    source = tmp_path / "objects.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        connection.execute("CREATE TABLE empty_records (id INTEGER)")
        connection.execute("CREATE VIEW records_view AS SELECT id FROM records")
        connection.execute(
            "CREATE TRIGGER records_after_insert AFTER INSERT ON records "
            "BEGIN UPDATE records SET name = name WHERE id = NEW.id; END"
        )

    with source.open("rb") as handle:
        uploaded = client.post(
            "/datasources/upload",
            data={"type": "sqlite", "name": "SQLite objects"},
            files={"file": ("objects.sqlite", handle, "application/octet-stream")},
        )
    datasource_id = unwrap(uploaded)["id"]
    schema = unwrap(client.get(f"/datasources/{datasource_id}/schema"))
    assert [table["name"] for table in schema["tables"]] == ["empty_records", "records"]
    empty = unwrap(client.get(f"/datasources/{datasource_id}/tables/empty_records/preview"))
    assert empty == {"columns": ["id"], "rows": [], "row_count": 0}


def test_sqlite_without_user_tables_fails_inspection(client, tmp_path: Path) -> None:
    source = tmp_path / "empty.sqlite"
    with sqlite3.connect(source):
        pass

    with source.open("rb") as handle:
        uploaded = client.post(
            "/datasources/upload",
            data={"type": "sqlite", "name": "No user tables"},
            files={"file": ("empty.sqlite", handle, "application/octet-stream")},
        )
    detail = unwrap(client.get(f"/datasources/{unwrap(uploaded)['id']}"))
    assert detail["status"] == "failed"
    assert detail["last_error_code"] == "SQLITE_NO_USER_TABLES"


def test_invalid_sqlite_error_does_not_leak_storage_path(client, migrated_settings) -> None:
    uploaded = client.post(
        "/datasources/upload",
        data={"type": "sqlite", "name": "Invalid SQLite"},
        files={"file": ("invalid.sqlite", b"not a sqlite database", "application/octet-stream")},
    )
    detail = unwrap(client.get(f"/datasources/{unwrap(uploaded)['id']}"))
    assert detail["status"] == "failed"
    assert detail["last_error_code"] == "SQLITE_INVALID"
    assert str(migrated_settings.datasource_root) not in detail["last_error_message"]


def test_preview_defaults_to_fifty_rows_and_rejects_unknown_tables(client) -> None:
    content = "id\n" + "\n".join(str(index) for index in range(60)) + "\n"
    uploaded = upload_csv(client, content.encode(), name="Preview limits")
    datasource_id = uploaded["id"]

    default_preview = unwrap(client.get(f"/datasources/{datasource_id}/tables/dataset/preview"))
    requested_preview = unwrap(
        client.get(f"/datasources/{datasource_id}/tables/dataset/preview?limit=999")
    )
    unknown = client.get(f"/datasources/{datasource_id}/tables/unknown/preview")
    assert default_preview["row_count"] == 50
    assert requested_preview["row_count"] == 50
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "DATASOURCE_NOT_READY"


def test_deleted_datasource_keeps_upload_but_rejects_new_reads(client, migrated_settings) -> None:
    uploaded = upload_csv(client, b"id\n1\n")
    datasource_id = uploaded["id"]
    saved_file = migrated_settings.datasource_root / datasource_id / "source.csv"
    assert saved_file.is_file()

    deleted = unwrap(client.delete(f"/datasources/{datasource_id}"))
    assert deleted == {"datasource_id": datasource_id, "status": "deleted"}
    assert saved_file.is_file()

    preview = client.get(f"/datasources/{datasource_id}/tables/dataset/preview")
    retry = client.post(f"/datasources/{datasource_id}/test")
    assert preview.status_code == 409
    assert retry.status_code == 409
    assert preview.json()["error"]["code"] == "DATASOURCE_NOT_READY"


def test_startup_marks_interrupted_inspections_as_failed(settings, migrated_settings) -> None:
    async def create_interrupted() -> str:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            repository = DataSourceRepository(session)
            model = await repository.create(
                datasource_id="datasource_interrupted",
                name="Interrupted",
                description=None,
                datasource_type=DataSourceType.CSV,
                source_ref="datasource_interrupted/source.csv",
                file_size=0,
                content_hash="sha256:test",
            )
            await session.commit()
        await engine.dispose()
        return model.id

    datasource_id = asyncio.run(create_interrupted())
    from fastapi.testclient import TestClient
    from server.app import create_app

    with TestClient(create_app(settings)) as client:
        detail = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert detail["status"] == "failed"
    assert detail["last_error_code"] == "DATASOURCE_CHECK_INTERRUPTED"


@pytest.mark.parametrize("outcome", ["complete", "failed"])
def test_late_inspection_result_does_not_restore_deleted_datasource(
    migrated_settings,
    outcome: str,
) -> None:
    async def exercise() -> str:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        session_factory = create_session_factory(engine)
        datasource_id = f"datasource_deleted_{outcome}"
        try:
            async with session_factory() as inspection_session:
                inspection_repository = DataSourceRepository(inspection_session)
                inspecting = await inspection_repository.create(
                    datasource_id=datasource_id,
                    name="Deleting",
                    description=None,
                    datasource_type=DataSourceType.CSV,
                    source_ref=f"{datasource_id}/source.csv",
                    file_size=0,
                    content_hash="sha256:test",
                )
                await inspection_session.commit()

                async with session_factory() as deletion_session:
                    deletion_repository = DataSourceRepository(deletion_session)
                    deleting = await deletion_repository.get(datasource_id)
                    assert deleting is not None
                    await deletion_repository.start_deletion(deleting)
                    await deletion_repository.complete_deletion(deleting)
                    await deletion_session.commit()

                if outcome == "complete":
                    await inspection_repository.complete_inspection(
                        inspecting,
                        SchemaSummaryRead(datasource_id=datasource_id, dialect="duckdb", tables=[]),
                    )
                else:
                    await inspection_repository.fail_inspection(
                        inspecting,
                        code="DATASOURCE_CHECK_FAILED",
                        message="数据源检查失败",
                    )
                await inspection_session.commit()

            async with session_factory() as verification_session:
                saved = await DataSourceRepository(verification_session).get(datasource_id)
                assert saved is not None
                return saved.status
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) == "deleted"


def test_gateway_writes_audit_and_artifact_for_safe_sql(client, migrated_settings) -> None:
    uploaded = upload_csv(client, b"id,amount\n1,10.20\n2,20.30\n")
    datasource_id = uploaded["id"]

    result = _run_gateway_query(
        migrated_settings,
        datasource_id,
        "SELECT id, amount FROM dataset ORDER BY id",
    )
    assert result["rows"] == [[1, "10.20"], [2, "20.30"]]
    assert result["artifact_id"]

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT status, normalized_sql, artifact_id FROM sql_audit_logs"
        ).fetchone()
        artifact = connection.execute(
            "SELECT storage_ref, metadata_json, preview_json FROM artifacts"
        ).fetchone()
    assert audit[0] == "succeeded"
    assert audit[1].endswith("LIMIT 100")
    assert audit[2] == result["artifact_id"]
    assert artifact[0] == f"gateway/{result['artifact_id']}.json"
    assert json.loads(artifact[1]) == {"full_result": True}
    assert artifact[2] is None
    assert (migrated_settings.artifact_root / artifact[0]).is_file()


def test_gateway_marks_discovery_observation_artifact_metadata(client, migrated_settings) -> None:
    uploaded = upload_csv(client, b"id,amount\n1,10.20\n2,20.30\n")

    result = _run_gateway_query(
        migrated_settings,
        uploaded["id"],
        "SELECT amount FROM dataset ORDER BY id",
        max_rows=100,
        artifact_usage="discovery_observation",
    )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        metadata_json = connection.execute(
            "SELECT metadata_json FROM artifacts WHERE id = ?",
            (result["artifact_id"],),
        ).fetchone()[0]

    assert json.loads(metadata_json) == {
        "full_result": True,
        "artifact_usage": "discovery_observation",
    }


def test_gateway_caps_explicit_limit_at_five_hundred_rows(client, migrated_settings) -> None:
    content = ("id\n" + "\n".join(str(index) for index in range(600)) + "\n").encode()
    uploaded = upload_csv(client, content, name="Query limits")

    result = _run_gateway_query(
        migrated_settings,
        uploaded["id"],
        "SELECT id FROM dataset ORDER BY id LIMIT 999",
    )
    assert result["row_count"] == 500

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        normalized_sql = connection.execute("SELECT normalized_sql FROM sql_audit_logs").fetchone()[
            0
        ]
    assert normalized_sql.endswith("LIMIT 500")


def test_gateway_keeps_preview_query_and_artifact_equally_safe(
    client, migrated_settings, tmp_path: Path
) -> None:
    source = tmp_path / "safe-result.sqlite"
    profile = '{"email":"person@example.com","items":[{"phone":"13800000000"}]}'
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE documents (id INTEGER, payload BLOB, profile TEXT)")
        connection.execute("INSERT INTO documents VALUES (?, ?, ?)", (1, b"abc", profile))

    with source.open("rb") as handle:
        uploaded = client.post(
            "/datasources/upload",
            data={"type": "sqlite", "name": "Safe result"},
            files={"file": ("safe-result.sqlite", handle, "application/octet-stream")},
        )
    datasource_id = unwrap(uploaded)["id"]
    unwrap(
        client.patch(
            f"/datasources/{datasource_id}/mask-fields",
            json={"mask_fields": ["profile"]},
        )
    )
    expected_rows = [
        [
            1,
            "[二进制数据，3 字节]",
            "***",
        ]
    ]

    preview = unwrap(client.get(f"/datasources/{datasource_id}/tables/documents/preview"))
    result = _run_gateway_query(
        migrated_settings,
        datasource_id,
        "SELECT id, payload, profile FROM documents",
    )
    artifact = _read_artifact_payload(migrated_settings, result["artifact_id"])
    assert preview["rows"] == expected_rows
    assert result["rows"] == expected_rows
    assert artifact["rows"] == expected_rows


def test_gateway_keeps_precise_values_identical_in_preview_query_and_artifact(
    client,
    migrated_settings,
) -> None:
    uploaded = upload_csv(
        client,
        (
            b"large,amount,calendar_day,local_time,global_time\n"
            b"9007199254740992,0.10,2026-08-11,2026-08-11T08:00:00,2026-08-11T08:00:00Z\n"
        ),
        name="Precise values",
    )
    datasource_id = uploaded["id"]
    expected_rows = [
        [
            "9007199254740992",
            "0.10",
            "2026-08-11",
            "2026-08-11T08:00:00",
            "2026-08-11T08:00:00Z",
        ]
    ]

    preview = unwrap(client.get(f"/datasources/{datasource_id}/tables/dataset/preview"))
    result = _run_gateway_query(
        migrated_settings,
        datasource_id,
        "SELECT large, amount, calendar_day, local_time, global_time FROM dataset",
    )
    artifact = _read_artifact_payload(migrated_settings, result["artifact_id"])
    assert preview["rows"] == expected_rows
    assert result["rows"] == expected_rows
    assert artifact["rows"] == expected_rows


def test_gateway_blocks_unknown_tables_without_artifact(client, migrated_settings) -> None:
    uploaded = upload_csv(client, b"id\n1\n")
    datasource_id = uploaded["id"]

    with pytest.raises(SqlGuardBlockedError) as caught:
        _run_gateway_query(migrated_settings, datasource_id, "SELECT * FROM outside_table")
    assert caught.value.code == "UNKNOWN_TABLE"

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT status, blocked_reason_code FROM sql_audit_logs"
        ).fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit == ("blocked", "UNKNOWN_TABLE")
    assert artifact_count == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv_auto('C:/outside.csv')",
        "SELECT * FROM read_json_auto('C:/outside.json')",
        "SELECT * FROM parquet_scan('C:/outside.parquet')",
    ],
)
def test_gateway_blocks_external_duckdb_table_functions(
    client,
    migrated_settings,
    sql: str,
) -> None:
    uploaded = upload_csv(client, b"id\n1\n")

    with pytest.raises(SqlGuardBlockedError) as caught:
        _run_gateway_query(migrated_settings, uploaded["id"], sql)
    assert caught.value.code == "UNSAFE_DUCKDB_OPERATION"

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT status, blocked_reason_code FROM sql_audit_logs"
        ).fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit == ("blocked", "UNSAFE_DUCKDB_OPERATION")
    assert artifact_count == 0


@pytest.mark.parametrize(
    ("sql", "expected_code"),
    [
        ("", "SQL_EMPTY"),
        ("SELECT * FROM dataset; SELECT * FROM dataset", "MULTIPLE_STATEMENTS"),
        ("DELETE FROM dataset", "WRITE_STATEMENT"),
        ("SELECT * FROM catalog.dataset", "CROSS_DATASOURCE_REFERENCE"),
        ("SELECT * FROM unknown_table", "UNKNOWN_TABLE"),
        ("SELECT * FROM read_csv_auto('C:/outside.csv')", "UNSAFE_DUCKDB_OPERATION"),
    ],
)
def test_sql_guard_returns_stable_rejection_codes(sql: str, expected_code: str) -> None:
    result = guard_sql(
        sql,
        dialect="duckdb",
        schema=_guard_schema("id"),
        default_limit=200,
        max_limit=500,
    )
    assert result.allowed is False
    assert result.reason_code == expected_code


def test_sql_guard_returns_safe_parse_location_and_special_column_hint() -> None:
    result = guard_sql(
        "SELECT AVG(not.fully.paid) FROM dataset",
        dialect="duckdb",
        schema=_guard_schema("not.fully.paid", "amount"),
        default_limit=200,
        max_limit=500,
    )

    assert result.allowed is False
    assert result.issue is not None
    assert result.issue.reason_code == "SQL_PARSE_ERROR"
    assert result.issue.location is not None
    assert (result.issue.location.line, result.issue.location.column) == (1, 15)
    assert "not.fully.paid" in (result.issue.subject or "")
    assert result.issue.hint == (
        "当前 Schema 中的字段 not.fully.paid 含特殊字符，必须逐字写为双引号字段名。"
    )
    assert result.issue.retryable is True


@pytest.mark.parametrize(
    ("sql", "expected_code", "expected_hint"),
    [
        (
            "SELECT missing FROM dataset",
            "UNKNOWN_COLUMN",
            "请只使用当前 Schema 中已提供的字段。",
        ),
        (
            "SELECT AVG(int.rate) FROM dataset",
            "UNKNOWN_COLUMN",
            "当前 Schema 中的字段 int.rate 含特殊字符，必须逐字写为双引号字段名。",
        ),
        (
            "SELECT amount FROM dataset LIMIT 'bad'",
            "INVALID_LIMIT",
            "请改为不超过 500 的整数。",
        ),
    ],
)
def test_sql_guard_returns_schema_based_repair_hints(
    sql: str,
    expected_code: str,
    expected_hint: str,
) -> None:
    result = guard_sql(
        sql,
        dialect="duckdb",
        schema=_guard_schema("amount", "int.rate"),
        default_limit=200,
        max_limit=500,
    )

    assert result.allowed is False
    assert result.issue is not None
    assert result.issue.reason_code == expected_code
    assert result.issue.hint == expected_hint
    assert result.issue.retryable is True


def test_sql_guard_explains_known_field_without_from_source() -> None:
    result = guard_sql(
        'SELECT "credit.policy" AS field_name',
        dialect="duckdb",
        schema=_guard_schema("credit.policy", "purpose"),
        default_limit=200,
        max_limit=500,
    )

    assert result.allowed is False
    assert result.issue is not None
    assert result.issue.reason_code == "COLUMN_SOURCE_MISSING"
    assert result.issue.subject == "credit.policy"
    assert result.issue.hint == (
        "字段 credit.policy 属于当前 Schema 中的数据表 dataset，但当前 SQL 没有 FROM 来源。"
        '如需读取该字段，请写 FROM "dataset"；如只需字段类型或表结构，'
        "请直接使用当前 Schema，不要执行 SQL。"
    )
    assert result.issue.retryable is True


def test_sql_guard_accepts_quoted_special_columns_and_cte_aliases() -> None:
    schema = _guard_schema("amount", "not.fully.paid", "int.rate")
    for sql in (
        'SELECT AVG("not.fully.paid") AS paid_rate FROM dataset',
        "WITH totals AS (SELECT SUM(amount) AS total FROM dataset) SELECT total FROM totals",
        'SELECT AVG("int.rate") AS avg_rate FROM dataset ORDER BY avg_rate',
    ):
        result = guard_sql(
            sql,
            dialect="duckdb",
            schema=schema,
            default_limit=200,
            max_limit=500,
        )
        assert result.allowed is True


def test_sql_guard_accepts_columns_expanded_from_cte_and_subquery_stars() -> None:
    schema = _guard_schema("amount", "category")
    for sql in (
        "WITH selected AS (SELECT * FROM dataset) SELECT amount FROM selected",
        "SELECT amount FROM (SELECT * FROM dataset) AS selected",
        "WITH selected AS (SELECT source.* FROM dataset AS source) SELECT amount FROM selected",
    ):
        result = guard_sql(
            sql,
            dialect="duckdb",
            schema=schema,
            default_limit=200,
            max_limit=500,
        )
        assert result.allowed is True


def test_sql_guard_accepts_columns_inside_scalar_subqueries_and_union_wrappers() -> None:
    """Nested query columns belong to their child scope, not the wrapper SELECT."""

    schema = SchemaSummaryRead(
        datasource_id="datasource_nested",
        dialect="sqlite",
        tables=[
            SchemaTableRead(
                name="customers",
                columns=[SchemaColumnRead(name="customer_id", type="INTEGER", nullable=True)],
            ),
            SchemaTableRead(
                name="orders",
                columns=[SchemaColumnRead(name="customer_id", type="INTEGER", nullable=True)],
            ),
        ],
    )
    result = guard_sql(
        "SELECT 'customers', (SELECT COUNT(*) FROM customers WHERE customer_id IS NULL), '' "
        "UNION ALL SELECT 'orders', (SELECT COUNT(DISTINCT customer_id) FROM orders), ''",
        dialect="sqlite",
        schema=schema,
        default_limit=200,
        max_limit=500,
    )
    assert result.allowed is True


def test_sql_guard_accepts_correlated_subquery_columns_from_outer_scope() -> None:
    result = guard_sql(
        "SELECT amount FROM dataset AS outer_row "
        "WHERE EXISTS (SELECT 1 FROM dataset AS inner_row "
        "WHERE inner_row.id = outer_row.id)",
        dialect="duckdb",
        schema=_guard_schema("id", "amount"),
        default_limit=200,
        max_limit=500,
    )

    assert result.allowed is True


def test_sql_guard_rejects_unknown_correlated_subquery_column() -> None:
    result = guard_sql(
        "SELECT amount FROM dataset AS outer_row "
        "WHERE EXISTS (SELECT 1 FROM dataset AS inner_row "
        "WHERE inner_row.id = outer_row.missing)",
        dialect="duckdb",
        schema=_guard_schema("id", "amount"),
        default_limit=200,
        max_limit=500,
    )

    assert result.allowed is False
    assert result.reason_code == "UNKNOWN_COLUMN"


def test_sql_guard_keeps_dangerous_statement_non_retryable() -> None:
    result = guard_sql(
        "DROP TABLE dataset",
        dialect="duckdb",
        schema=_guard_schema("amount"),
        default_limit=200,
        max_limit=500,
    )

    assert result.allowed is False
    assert result.issue is not None
    assert result.issue.reason_code == "WRITE_STATEMENT"
    assert result.issue.retryable is False


def test_gateway_falls_back_to_preview_artifact_when_file_write_fails(
    client,
    migrated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = upload_csv(client, b"id\n1\n")

    def fail_write(_target: Path, _content: bytes) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(ArtifactStore, "_write_bytes", staticmethod(fail_write))
    result = _run_gateway_query(
        migrated_settings,
        uploaded["id"],
        "SELECT id FROM dataset",
    )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        artifact = connection.execute(
            "SELECT storage_ref, preview_json, metadata_json FROM artifacts"
        ).fetchone()
        audit_status = connection.execute("SELECT status FROM sql_audit_logs").fetchone()[0]
    assert result["artifact_id"]
    assert audit_status == "succeeded"
    assert artifact[0] is None
    assert json.loads(artifact[1])["rows"] == [[1]]
    assert json.loads(artifact[2]) == {"full_result": False}


def test_gateway_preserves_discovery_usage_when_falling_back_to_preview_artifact(
    client,
    migrated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = upload_csv(client, b"id\n1\n")

    def fail_write(_target: Path, _content: bytes) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(ArtifactStore, "_write_bytes", staticmethod(fail_write))
    result = _run_gateway_query(
        migrated_settings,
        uploaded["id"],
        "SELECT id FROM dataset",
        max_rows=100,
        artifact_usage="discovery_observation",
    )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        metadata_json = connection.execute(
            "SELECT metadata_json FROM artifacts WHERE id = ?",
            (result["artifact_id"],),
        ).fetchone()[0]
    assert json.loads(metadata_json) == {
        "full_result": False,
        "artifact_usage": "discovery_observation",
    }


def test_gateway_marks_discovery_usage_on_preview_artifact_when_file_write_fails(
    client,
    migrated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = upload_csv(client, b"id\n1\n")

    def fail_write(_target: Path, _content: bytes) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(ArtifactStore, "_write_bytes", staticmethod(fail_write))
    result = _run_gateway_query(
        migrated_settings,
        uploaded["id"],
        "SELECT id FROM dataset",
        artifact_usage="discovery_observation",
    )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        metadata_json = connection.execute(
            "SELECT metadata_json FROM artifacts WHERE id = ?",
            (result["artifact_id"],),
        ).fetchone()[0]

    assert json.loads(metadata_json) == {
        "full_result": False,
        "artifact_usage": "discovery_observation",
    }


def test_gateway_marks_sql_failed_when_preview_artifact_also_cannot_persist(
    client,
    migrated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = upload_csv(client, b"id\n1\n")

    async def fail_artifact(*_args, **_kwargs) -> str:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(ArtifactStore, "create_table_artifact", fail_artifact)
    with pytest.raises(OSError):
        _run_gateway_query(migrated_settings, uploaded["id"], "SELECT id FROM dataset")

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute("SELECT status, error_code FROM sql_audit_logs").fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit == ("failed", "ARTIFACT_PERSISTENCE_FAILED")
    assert artifact_count == 0


def test_gateway_timeout_does_not_create_an_artifact(client, migrated_settings) -> None:
    source = Path(migrated_settings.datasource_root) / "slow.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE numbers (value INTEGER)")
        connection.execute("INSERT INTO numbers VALUES (1)")

    with source.open("rb") as handle:
        uploaded = client.post(
            "/datasources/upload",
            data={"type": "sqlite", "name": "Slow query"},
            files={"file": ("slow.sqlite", handle, "application/octet-stream")},
        )
    datasource = unwrap(uploaded)
    migrated_settings.query_timeout_seconds = 0

    with pytest.raises(Exception, match="查询超时"):
        _run_gateway_query(
            migrated_settings,
            datasource["id"],
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 100000000"
            ") SELECT sum(value) FROM counter",
        )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute("SELECT status, error_code FROM sql_audit_logs").fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit == ("timeout", "QUERY_TIMEOUT")
    assert artifact_count == 0


def test_gateway_timeout_includes_csv_materialization(
    client, migrated_settings, monkeypatch
) -> None:
    """查询预算必须覆盖 CSV 到 DuckDB 的装载，不只覆盖 execute。"""

    uploaded = upload_csv(client, b"id\n1\n")
    migrated_settings.query_timeout_seconds = 1

    def slow_materialize(self, connection, columns, **kwargs):
        del connection, columns, kwargs
        time.sleep(1.2)

    monkeypatch.setattr(CsvAdapter, "_materialize_snapshot", slow_materialize)
    with pytest.raises(Exception, match="查询超时"):
        _run_gateway_query(migrated_settings, uploaded["id"], "SELECT id FROM dataset")

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT status, error_code, elapsed_ms FROM sql_audit_logs"
        ).fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit[0:2] == ("timeout", "QUERY_TIMEOUT")
    assert audit[2] is not None and audit[2] >= 1_000
    assert artifact_count == 0


def test_gateway_reuses_csv_snapshot_for_same_datasource_revision(
    client, migrated_settings, monkeypatch
) -> None:
    """同一 Gateway 内的多条 SQL 只装载一次同版本 CSV。"""

    uploaded = upload_csv(client, b"id,amount\n1,10\n2,20\n")
    migrated_settings.query_timeout_seconds = 60
    open_calls = 0
    original_open = CsvAdapter._open_connection

    def counted_open(self, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return original_open(self, **kwargs)

    monkeypatch.setattr(CsvAdapter, "_open_connection", counted_open)

    async def execute_twice() -> tuple[int, int]:
        engine = create_sqlite_engine(migrated_settings.metadata_database_url)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                service = build_datasource_service(session, migrated_settings)
                datasource = await DataSourceRepository(session).get(uploaded["id"])
                assert datasource is not None
                source_path = service._source_path_for_model(datasource)
                first = await service.gateway.run_sql_readonly(
                    datasource, source_path, "SELECT SUM(amount) AS total FROM dataset"
                )
                second = await service.gateway.run_sql_readonly(
                    datasource, source_path, "SELECT COUNT(*) AS total FROM dataset"
                )
                await session.commit()
                return first.row_count, second.row_count
        finally:
            await engine.dispose()

    assert asyncio.run(execute_twice()) == (1, 1)
    assert open_calls == 1


def test_gateway_cancels_before_adapter_creation(client, migrated_settings, monkeypatch) -> None:
    uploaded = upload_csv(client, b"id\n1\n")
    token = QueryCancelToken()
    token.cancel()

    def adapter_must_not_be_created(*_args, **_kwargs):
        raise AssertionError("取消后的查询不应创建 Adapter")

    monkeypatch.setattr(AdapterRegistry, "create", adapter_must_not_be_created)
    with pytest.raises(QueryCanceledError, match="查询已取消"):
        _run_gateway_query(
            migrated_settings,
            uploaded["id"],
            "SELECT id FROM dataset",
            cancel_token=token,
        )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute("SELECT status, error_code FROM sql_audit_logs").fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit == ("canceled", "QUERY_CANCELED")
    assert artifact_count == 0


def test_gateway_cancels_running_sqlite_query(client, migrated_settings, tmp_path: Path) -> None:
    source = tmp_path / "cancel.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE numbers (value INTEGER)")
        connection.execute("INSERT INTO numbers VALUES (1)")

    with source.open("rb") as handle:
        uploaded = client.post(
            "/datasources/upload",
            data={"type": "sqlite", "name": "Cancelable query"},
            files={"file": ("cancel.sqlite", handle, "application/octet-stream")},
        )
    datasource = unwrap(uploaded)
    token = QueryCancelToken()

    with pytest.raises(QueryCanceledError, match="查询已取消"):
        _run_gateway_query(
            migrated_settings,
            datasource["id"],
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 100000000"
            ") SELECT sum(value) FROM counter",
            cancel_token=token,
            cancel_after_seconds=0.05,
        )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT status, error_code, elapsed_ms FROM sql_audit_logs"
        ).fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit[0:2] == ("canceled", "QUERY_CANCELED")
    assert audit[2] is not None
    assert artifact_count == 0


def test_gateway_cancels_running_duckdb_query(client, migrated_settings) -> None:
    uploaded = upload_csv(client, b"id\n1\n", name="Cancelable DuckDB query")
    token = QueryCancelToken()

    with pytest.raises(QueryCanceledError, match="查询已取消"):
        _run_gateway_query(
            migrated_settings,
            uploaded["id"],
            "WITH RECURSIVE counter(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 100000000"
            ") SELECT sum(value) FROM counter",
            cancel_token=token,
            cancel_after_seconds=0.05,
        )

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit = connection.execute("SELECT status, error_code FROM sql_audit_logs").fetchone()
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert audit == ("canceled", "QUERY_CANCELED")
    assert artifact_count == 0


def test_deleted_datasource_keeps_existing_audit_and_artifact_as_history(
    client,
    migrated_settings,
) -> None:
    uploaded = upload_csv(client, b"id\n1\n", name="Historical source")
    datasource_id = uploaded["id"]
    result = _run_gateway_query(migrated_settings, datasource_id, "SELECT id FROM dataset")

    unwrap(client.delete(f"/datasources/{datasource_id}"))
    with pytest.raises(AppError, match="数据源尚未完成检查"):
        _run_gateway_query(migrated_settings, datasource_id, "SELECT id FROM dataset")

    database = _database_path(migrated_settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        audit_count = connection.execute("SELECT COUNT(*) FROM sql_audit_logs").fetchone()[0]
        artifact_id = connection.execute("SELECT artifact_id FROM sql_audit_logs").fetchone()[0]
    assert audit_count == 1
    assert artifact_id == result["artifact_id"]
    assert _read_artifact_payload(migrated_settings, artifact_id)["rows"] == [[1]]


def test_result_serialization_does_not_guess_nested_sensitive_values() -> None:
    result = TableDataRead(
        columns=["profile", "phone", "large", "amount", "occurred_at"],
        rows=[
            [
                '{"email":"person@example.com","nested":{"密钥":"secret"}}',
                "13800000000",
                9_007_199_254_740_992,
                Decimal("0.10"),
                datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
            ]
        ],
        row_count=1,
    )
    serialized = serialize_table_result(result, SensitiveFieldPolicy())
    assert serialized.rows == [
        [
            {"email": "person@example.com", "nested": {"密钥": "secret"}},
            "13800000000",
            "9007199254740992",
            "0.10",
            "2026-08-11T08:00:00Z",
        ]
    ]


def test_result_serialization_does_not_guess_format_sensitive_columns() -> None:
    result = TableDataRead(
        columns=["contact", "note"],
        rows=[
            ["a@example.com", "one"],
            ["b@example.com", "two"],
            ["c@example.com", "three"],
            ["d@example.com", "four"],
            ["e@example.com", "five"],
        ],
        row_count=5,
    )

    serialized = serialize_table_result(result, SensitiveFieldPolicy())

    assert serialized.rows == [
        ["a@example.com", "one"],
        ["b@example.com", "two"],
        ["c@example.com", "three"],
        ["d@example.com", "four"],
        ["e@example.com", "five"],
    ]


def test_result_serialization_masks_only_explicit_top_level_columns() -> None:
    result = TableDataRead(
        columns=["profile", "phone"],
        rows=[[{"email": "person@example.com"}, "13800000000"]],
        row_count=1,
    )

    serialized = serialize_table_result(
        result,
        SensitiveFieldPolicy(
            mask_fields=frozenset({"profile", "phone"}),
            confirmed=True,
        ),
    )

    assert serialized.rows == [["***", "***"]]


def _run_gateway_query(
    settings,
    datasource_id: str,
    sql: str,
    *,
    cancel_token: QueryCancelToken | None = None,
    cancel_after_seconds: float | None = None,
    max_rows: int | None = None,
    artifact_usage: str | None = None,
) -> dict[str, object]:
    async def execute() -> dict[str, object]:
        engine = create_sqlite_engine(settings.metadata_database_url)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                service = build_datasource_service(session, settings)
                datasource = await DataSourceRepository(session).get(datasource_id)
                assert datasource is not None
                try:
                    task = asyncio.create_task(
                        service.gateway.run_sql_readonly(
                            datasource,
                            service._source_path_for_model(datasource),
                            sql,
                            cancel_token=cancel_token,
                            max_rows=max_rows,
                            **(
                                {"artifact_usage": artifact_usage}
                                if artifact_usage is not None
                                else {}
                            ),
                        )
                    )
                    if cancel_after_seconds is not None:
                        await asyncio.sleep(cancel_after_seconds)
                        assert cancel_token is not None
                        cancel_token.cancel()
                    result = await task
                except Exception:
                    # Guard 阻断和执行失败也要留下同一条 Audit，不能随会话回滚丢失。
                    await session.commit()
                    raise
                else:
                    await session.commit()
                    return result.model_dump()
        except SqlGuardBlockedError:
            raise
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _database_path(database_url: str) -> Path:
    return Path(database_url.removeprefix("sqlite+aiosqlite:///"))


def _read_artifact_payload(settings, artifact_id: str) -> dict[str, object]:
    database = _database_path(settings.metadata_database_url)
    with sqlite3.connect(database) as connection:
        storage_ref = connection.execute(
            "SELECT storage_ref FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()[0]
    assert storage_ref is not None
    return json.loads((settings.artifact_root / storage_ref).read_text(encoding="utf-8"))
