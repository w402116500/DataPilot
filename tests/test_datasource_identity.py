from __future__ import annotations

from agent_runtime.conversation_context import datasource_identity
from contracts.datasources import SchemaSummaryRead


def _schema() -> SchemaSummaryRead:
    return SchemaSummaryRead(
        datasource_id="datasource_1",
        dialect="duckdb",
        tables=[
            {
                "name": "orders",
                "columns": [{"name": "amount", "type": "INTEGER", "nullable": False}],
            }
        ],
    )


def test_datasource_identity_omits_blank_description() -> None:
    identity = datasource_identity(
        name="销售订单",
        source_type="csv",
        schema=_schema(),
        schema_revision=1,
        description="   ",
    )
    assert identity.description is None
    dumped = identity.model_dump()
    assert dumped["name"] == "销售订单"
    assert dumped["source_type"] == "csv"
    assert dumped["dialect"] == "duckdb"
    assert dumped["schema_revision"] == 1
    assert dumped["description"] is None


def test_datasource_identity_includes_nonempty_description() -> None:
    identity = datasource_identity(
        name="销售订单",
        source_type="csv",
        schema=_schema(),
        schema_revision=2,
        description="  电商订单主表  ",
    )
    assert identity.description == "电商订单主表"
