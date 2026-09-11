from __future__ import annotations

from tests.test_datasources_and_gateway import unwrap, upload_csv


def test_datasource_description_can_be_updated_after_upload(client) -> None:
    uploaded = upload_csv(client, b"order_id,amount\n1,10\n", name="Orders")
    datasource_id = uploaded["id"]
    assert uploaded["description"] is None

    updated = unwrap(
        client.patch(
            f"/datasources/{datasource_id}/description",
            json={"description": "电商订单主表，amount 为实付金额"},
        )
    )
    assert updated["id"] == datasource_id
    assert updated["description"] == "电商订单主表，amount 为实付金额"
    assert updated["mask_fields"] == uploaded["mask_fields"]
    assert "datalink_graph_version" in updated

    detail = unwrap(client.get(f"/datasources/{datasource_id}"))
    assert detail["description"] == "电商订单主表，amount 为实付金额"


def test_datasource_description_blank_clears_the_field(client) -> None:
    uploaded = upload_csv(
        client,
        b"order_id\n1\n",
        name="Orders",
    )
    datasource_id = uploaded["id"]
    unwrap(
        client.patch(
            f"/datasources/{datasource_id}/description",
            json={"description": "临时说明"},
        )
    )

    cleared = unwrap(
        client.patch(
            f"/datasources/{datasource_id}/description",
            json={"description": "   "},
        )
    )
    assert cleared["description"] is None


def test_datasource_description_rejects_overlong_text(client) -> None:
    uploaded = upload_csv(client, b"order_id\n1\n", name="Orders")
    response = client.patch(
        f"/datasources/{uploaded['id']}/description",
        json={"description": "x" * 2001},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_datasource_description_requires_the_field(client) -> None:
    uploaded = upload_csv(client, b"order_id\n1\n", name="Orders")
    unwrap(
        client.patch(
            f"/datasources/{uploaded['id']}/description",
            json={"description": "保留说明"},
        )
    )

    response = client.patch(f"/datasources/{uploaded['id']}/description", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    detail = unwrap(client.get(f"/datasources/{uploaded['id']}"))
    assert detail["description"] == "保留说明"
