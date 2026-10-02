"""DataLink 参考 DataFoundry 的语义映射与同义合并边界测试。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import httpx
import pytest
from contracts.datalink import DataLinkEdgeType, DataLinkNodeType

from server.config import Settings
from server.mapper.client import (
    ModelConfigurationError,
    ModelRequestTimeoutError,
    ModelResponseError,
    ModelResponseFormatError,
    ModelUpstreamError,
    OpenAICompatibleSemanticClient,
    SemanticModelClient,
)
from server.mapper.mapper import SemanticMapper, SemanticMappingValidationError
from server.mapper.merger import SemanticMerger
from server.mapper.schemas import (
    ConceptMapping,
    EntityMapping,
    MappingColumnInput,
    MergeCandidate,
    MergeDecision,
    SemanticMappingResponse,
    SemanticMergeResponse,
    SemanticNodeInput,
)
from server.models.graph import GraphEdge, GraphNode, graph_provenance
from server.models.profile import ColumnProfile


class FakeSemanticClient(SemanticModelClient):
    """记录模型边界输入的测试双，不调用网络或读取真实配置。"""

    def __init__(
        self,
        responses: Sequence[SemanticMappingResponse | Exception],
        *,
        merge_response: SemanticMergeResponse | None = None,
        fail_merge: bool = False,
        merge_error: Exception | None = None,
        embeddings: tuple[tuple[float, ...], ...] | None = None,
        embedding_error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self.merge_response = merge_response or SemanticMergeResponse(merges=[])
        self.fail_merge = fail_merge
        self.merge_error = merge_error
        self.embeddings = embeddings
        self.embedding_error = embedding_error
        self.mapping_calls: list[tuple[MappingColumnInput, ...]] = []
        self.merge_calls = 0

    def map_batch(self, columns: Sequence[MappingColumnInput]) -> SemanticMappingResponse:
        self.mapping_calls.append(tuple(columns))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def judge_merges(self, new_nodes, existing_nodes, candidates) -> SemanticMergeResponse:
        self.merge_calls += 1
        if self.merge_error is not None:
            raise self.merge_error
        if self.fail_merge:
            raise ModelResponseError("model unavailable")
        return self.merge_response

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        if self.embedding_error is not None:
            raise self.embedding_error
        return self.embeddings


def test_mapper_batches_all_columns_including_explicitly_sensitive_profiles() -> None:
    columns = tuple(_column(index) for index in range(16)) + (_column(16, name="email"),)
    profiles = tuple(_profile(column, sensitive=column.name == "email") for column in columns)
    responses = (
        _valid_response(columns[:5], "first"),
        _valid_response(columns[5:10], "second"),
        _valid_response(columns[10:15], "third"),
        _valid_response(columns[15:], "fourth"),
    )
    client = FakeSemanticClient(responses)

    result = SemanticMapper(client).map_columns("ds_demo", columns, profiles)

    assert [len(call) for call in client.mapping_calls] == [5, 5, 5, 2]
    assert any(item.column_name == "email" for call in client.mapping_calls for item in call)
    assert client.merge_calls == 1
    represents = [edge for edge in result.edges if edge.type == DataLinkEdgeType.REPRESENTS]
    assert len(represents) == 17
    assert all(edge.type != DataLinkEdgeType.JOINABLE for edge in result.edges)


def test_mapper_rejects_batch_with_out_of_scope_column_reference() -> None:
    columns = (_column(0), _column(1))
    profiles = tuple(_profile(column) for column in columns)
    response = SemanticMappingResponse(
        concepts=[
            ConceptMapping(
                name="wrong",
                description="wrong field",
                aliases=[],
                columns=["column:other:table:id"],
                confidence=0.8,
            )
        ],
        entities=[],
    )

    with pytest.raises(SemanticMappingValidationError, match="only columns from its mapping batch"):
        SemanticMapper(FakeSemanticClient((response,))).map_columns("ds_demo", columns, profiles)


def test_mapper_keeps_entity_and_ignores_its_unknown_concept_reference() -> None:
    column = _column(0)
    response = SemanticMappingResponse(
        concepts=[
            ConceptMapping(
                name="order identifier",
                description="Identifier for an order",
                aliases=[],
                columns=[column.id],
                confidence=0.9,
            )
        ],
        entities=[
            EntityMapping(
                name="order",
                description="An order",
                aliases=[],
                concept_names=["customer identifier"],
                confidence=0.8,
            )
        ],
    )

    result = SemanticMapper(FakeSemanticClient((response,))).map_columns(
        "ds_demo", (column,), (_profile(column),)
    )

    assert {node.type for node in result.nodes} == {
        DataLinkNodeType.CONCEPT,
        DataLinkNodeType.ENTITY,
    }
    assert not [edge for edge in result.edges if edge.type == DataLinkEdgeType.HAS_CONCEPT]


def test_mapper_propagates_failed_batch_and_stops_current_build() -> None:
    columns = (_column(0), _column(1))
    client = FakeSemanticClient(
        (ModelResponseError("invalid JSON"), _valid_response((columns[1],), "second"))
    )

    with pytest.raises(ModelResponseError, match="invalid JSON"):
        SemanticMapper(client, batch_size=1).map_columns(
            "ds_demo", columns, tuple(_profile(column) for column in columns)
        )

    assert [len(call) for call in client.mapping_calls] == [1]


def test_mapper_allows_an_empty_mapping_set() -> None:
    client = FakeSemanticClient(())

    result = SemanticMapper(client).map_columns("ds_demo", (), ())

    assert result.nodes == ()
    assert result.edges == ()
    assert client.mapping_calls == []


def test_mapper_records_semantic_mapping_provenance() -> None:
    column = _column(0)
    response = SemanticMappingResponse(
        concepts=[
            ConceptMapping(
                name="订单编号",
                description="订单主键",
                aliases=["order_id"],
                columns=[column.id],
                confidence=0.9,
            )
        ],
        entities=[
            EntityMapping(
                name="订单",
                description="一笔订单",
                aliases=[],
                concept_names=["订单编号"],
                confidence=0.8,
            )
        ],
    )

    result = SemanticMapper(FakeSemanticClient((response,))).map_columns(
        "ds_demo", (column,), (_profile(column),)
    )

    concepts = [node for node in result.nodes if node.type == DataLinkNodeType.CONCEPT]
    entities = [node for node in result.nodes if node.type == DataLinkNodeType.ENTITY]
    represents = [edge for edge in result.edges if edge.type == DataLinkEdgeType.REPRESENTS]
    has_concept = [edge for edge in result.edges if edge.type == DataLinkEdgeType.HAS_CONCEPT]
    mapped = (*concepts, *entities, *represents, *has_concept)
    assert len(concepts) == 1 and len(entities) == 1
    assert len(represents) == 1 and len(has_concept) == 1
    assert all(item.properties["provenance"] == "semantic_mapping" for item in mapped)
    assert {graph_provenance(item) for item in mapped} == {"semantic_mapping"}


def test_merge_failure_keeps_duplicate_semantic_nodes_and_does_not_fail_mapping() -> None:
    columns = (_column(0), _column(1))
    profiles = tuple(_profile(column) for column in columns)
    client = FakeSemanticClient((_valid_response(columns, "first"),), fail_merge=True)

    result = SemanticMapper(client, batch_size=15).map_columns("ds_demo", columns, profiles)

    assert len(result.nodes) == 3
    assert client.merge_calls == 1


def test_merge_configuration_failure_keeps_duplicate_semantic_nodes() -> None:
    columns = (_column(0), _column(1))
    client = FakeSemanticClient(
        (_valid_response(columns, "first"),),
        merge_error=ModelConfigurationError("model unavailable"),
    )

    result = SemanticMapper(client, batch_size=15).map_columns(
        "ds_demo", columns, tuple(_profile(column) for column in columns)
    )

    assert len(result.nodes) == 3
    assert client.merge_calls == 1


def test_embedding_prefilter_with_no_candidates_falls_back_to_merge_model() -> None:
    columns = (_column(0), _column(1))
    client = FakeSemanticClient(
        (_valid_response(columns, "first"),),
        embeddings=((1.0, 0.0), (0.0, 1.0)),
    )

    result = SemanticMapper(client, batch_size=15).map_columns(
        "ds_demo", columns, tuple(_profile(column) for column in columns)
    )

    assert client.merge_calls == 1
    assert len(result.nodes) == 3


@pytest.mark.parametrize(
    "error", [ModelConfigurationError("not configured"), ModelResponseError("failed")]
)
def test_embedding_failure_falls_back_to_merge_model(error: Exception) -> None:
    """Embedding 是可选预筛，失败时仍应让模型比较完整候选。"""

    columns = (_column(0), _column(1))
    client = FakeSemanticClient(
        (_valid_response(columns, "first"),),
        embedding_error=error,
    )

    SemanticMapper(client, batch_size=15).map_columns(
        "ds_demo", columns, tuple(_profile(column) for column in columns)
    )

    assert client.merge_calls == 1


def test_chained_merge_plan_keeps_duplicate_nodes() -> None:
    columns = (_column(0), _column(1), _column(2))
    response = _valid_response(columns, "first")
    concept_ids = [
        f"concept:ds_demo:1:{index}:{concept.name}"
        for index, concept in enumerate(response.concepts, start=1)
    ]
    client = FakeSemanticClient(
        (response,),
        merge_response=SemanticMergeResponse(
            merges=[
                MergeDecision(
                    new_id=concept_ids[0],
                    existing_id=concept_ids[1],
                    confidence=0.9,
                    reason="same meaning",
                ),
                MergeDecision(
                    new_id=concept_ids[1],
                    existing_id=concept_ids[2],
                    confidence=0.9,
                    reason="same meaning",
                ),
            ]
        ),
    )

    result = SemanticMapper(client, batch_size=15).map_columns(
        "ds_demo", columns, tuple(_profile(column) for column in columns)
    )

    assert client.merge_calls == 1
    assert len(result.nodes) == 4


def test_same_name_concepts_merge_without_model() -> None:
    client = FakeSemanticClient((), fail_merge=True)
    keeper = _semantic_node("concept:1", "客户标识", aliases=("customer_id",), description="先出现")
    duplicate = _semantic_node("concept:2", "客户标识", description="后出现")

    result = SemanticMerger(client).merge(
        (keeper, duplicate),
        (
            _represents("column:customers:customer_id", keeper.id),
            _represents("column:orders:customer_id", duplicate.id),
        ),
        (),
        (),
    )

    assert client.merge_calls == 0
    concepts = [node for node in result.nodes if node.type == DataLinkNodeType.CONCEPT]
    assert [node.id for node in concepts] == [keeper.id]
    assert concepts[0].description == "先出现"
    assert "customer_id" in concepts[0].aliases
    assert {edge.source_id: edge.target_id for edge in result.edges} == {
        "column:customers:customer_id": keeper.id,
        "column:orders:customer_id": keeper.id,
    }
    assert concepts[0].properties["provenance"] == "semantic_mapping"
    assert all(graph_provenance(edge) == "semantic_mapping" for edge in result.edges)


def test_alias_hit_merges_without_model() -> None:
    client = FakeSemanticClient(())
    named = _semantic_node("concept:1", "客户标识")
    aliased = _semantic_node("concept:2", "客户编号", aliases=("客户标识",))

    result = SemanticMerger(client).merge((named, aliased), (), (), ())

    assert client.merge_calls == 0
    assert [node.id for node in result.nodes] == [named.id]
    assert "客户编号" in result.nodes[0].aliases


def test_casefold_alias_hit_merges_without_model() -> None:
    client = FakeSemanticClient(())
    named = _semantic_node("concept:1", "Customer_ID")
    aliased = _semantic_node("concept:2", "客户标识", aliases=("customer_id",))

    result = SemanticMerger(client).merge((named, aliased), (), (), ())

    assert client.merge_calls == 0
    assert [node.id for node in result.nodes] == [named.id]


def test_different_names_still_ask_merge_model() -> None:
    client = FakeSemanticClient(())
    customer = _semantic_node("concept:1", "客户")
    identifier = _semantic_node("concept:2", "客户标识")

    result = SemanticMerger(client).merge((customer, identifier), (), (), ())

    assert client.merge_calls == 1
    assert {node.id for node in result.nodes} == {customer.id, identifier.id}


def test_same_name_does_not_merge_different_node_types() -> None:
    client = FakeSemanticClient(())
    concept = _semantic_node("concept:1", "订单")
    entity = _semantic_node("entity:1", "订单", node_type=DataLinkNodeType.ENTITY)

    result = SemanticMerger(client).merge((concept, entity), (), (), ())

    assert client.merge_calls == 0
    assert {node.id for node in result.nodes} == {concept.id, entity.id}


def test_model_failure_keeps_unmatched_after_same_name_absorb() -> None:
    client = FakeSemanticClient((), fail_merge=True)
    first = _semantic_node("concept:1", "客户标识")
    duplicate = _semantic_node("concept:2", "客户标识")
    price = _semantic_node("concept:3", "商品单价")

    result = SemanticMerger(client).merge((first, duplicate, price), (), (), ())

    assert client.merge_calls == 1
    assert {node.id for node in result.nodes} == {first.id, price.id}


def test_mapper_same_name_across_batches_merges_without_model() -> None:
    columns = (_column(0), _column(1))
    responses = (
        SemanticMappingResponse(
            concepts=[
                ConceptMapping(
                    name="客户标识",
                    description="客户主键",
                    aliases=["customer_id"],
                    columns=[columns[0].id],
                    confidence=0.9,
                )
            ],
            entities=[],
        ),
        SemanticMappingResponse(
            concepts=[
                ConceptMapping(
                    name="客户标识",
                    description="订单中的客户",
                    aliases=[],
                    columns=[columns[1].id],
                    confidence=0.8,
                )
            ],
            entities=[],
        ),
    )
    client = FakeSemanticClient(responses, fail_merge=True)

    result = SemanticMapper(client, batch_size=1).map_columns(
        "ds_demo", columns, tuple(_profile(column) for column in columns)
    )

    assert client.merge_calls == 0
    concepts = [node for node in result.nodes if node.type == DataLinkNodeType.CONCEPT]
    assert len(concepts) == 1
    assert concepts[0].name == "客户标识"
    represents = [edge for edge in result.edges if edge.type == DataLinkEdgeType.REPRESENTS]
    assert len(represents) == 2
    assert {edge.target_id for edge in represents} == {concepts[0].id}


def test_openai_compatible_client_uses_strict_json_schema_and_rejects_missing_arrays() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"unexpected": true}'}}]},
        )

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    with pytest.raises(ModelResponseFormatError, match="invalid mapping JSON"):
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )

    assert len(requests) == 2
    probe_payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert probe_payload["response_format"]["type"] == "json_schema"
    assert (
        probe_payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe"
    )
    assert probe_payload["response_format"]["json_schema"]["strict"] is True
    _assert_every_object_is_strict(probe_payload["response_format"]["json_schema"]["schema"])
    assert probe_payload["messages"][1]["content"] == "Return the required empty JSON object now."
    assert "columns" not in probe_payload["messages"][1]["content"]

    payload = json.loads(requests[1].content)
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "datalink_semantic_mapping"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    _assert_every_object_is_strict(schema)
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] == 16384
    prompt = payload["messages"][0]["content"]
    assert "conforms to the required output contract" in prompt
    assert '"columns"' in prompt
    assert "Simplified Chinese" in prompt
    assert "aliases" in prompt
    assert "loan" not in prompt
    assert "customer" not in prompt
    assert "order" not in prompt
    assert "客户" not in prompt
    assert "订单" not in prompt


def test_openai_compatible_client_accepts_only_a_complete_schema_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        return _completion_response(_complete_mapping_payload())

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    response = client.map_batch(
        (
            MappingColumnInput(
                column_id=column.id,
                column_name=column.name,
                table_name="orders",
                profile=_profile(column).to_read(),
            ),
        )
    )

    assert response.concepts[0].columns == [column.id]
    assert response.concepts[0].confidence == 0.9
    assert response.entities[0].confidence == 0.9


@pytest.mark.parametrize(
    "response_payload",
    [
        {
            "concepts": [
                {
                    "name": "semantic_value",
                    "description": "Meaning for the supplied field.",
                    "columns": ["column:ds_demo:orders:field_0"],
                    "confidence": 0.9,
                }
            ],
            "entities": [],
        },
        {
            "concepts": [
                {
                    "name": "semantic_value",
                    "description": "Meaning for the supplied field.",
                    "aliases": [],
                    "columns": ["column:ds_demo:orders:field_0"],
                    "confidence": 0.9,
                    "unexpected": True,
                }
            ],
            "entities": [],
        },
        {
            "concepts": [
                {
                    "name": "semantic_value",
                    "description": "Meaning for the supplied field.",
                    "aliases": [],
                    "field_ids": ["column:ds_demo:orders:field_0"],
                    "confidence": 0.9,
                }
            ],
            "entities": [],
        },
    ],
)
def test_openai_compatible_client_rejects_response_outside_the_schema(
    response_payload: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        return _completion_response(response_payload)

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    with pytest.raises(ModelResponseFormatError, match="invalid mapping JSON"):
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )


def test_openai_compatible_client_rejects_fenced_json_without_repair() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\\n{\\"concepts\\": [], \\"entities\\": []}\\n```'
                        }
                    }
                ]
            },
        )

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    with pytest.raises(ModelResponseFormatError, match="invalid mapping JSON"):
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )


def test_openai_compatible_merge_prompt_describes_complete_output_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"merges": []}'}}]},
        )

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    client.judge_merges(
        (
            SemanticNodeInput(
                id="concept:new:customer_id",
                type="concept",
                name="customer_id",
            ),
        ),
        (
            SemanticNodeInput(
                id="concept:existing:person_identifier",
                type="concept",
                name="person_identifier",
            ),
        ),
        (
            MergeCandidate(
                new_id="concept:new:customer_id",
                existing_id="concept:existing:person_identifier",
                type="concept",
            ),
        ),
    )

    assert len(requests) == 2
    payload = json.loads(requests[1].content)
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "datalink_semantic_merge"
    assert response_format["json_schema"]["strict"] is True
    _assert_every_object_is_strict(response_format["json_schema"]["schema"])
    assert payload["temperature"] == 0.0

    prompt = payload["messages"][0]["content"]
    assert '"reason":' in prompt
    assert '"confidence": 0.95' in prompt
    assert '{"merges": []}' in prompt
    assert "candidate pairs may be merged" in prompt
    assert "Simplified Chinese" in prompt
    assert "customer" not in prompt
    assert "order" not in prompt
    assert "客户" not in prompt
    assert "订单" not in prompt


def test_openai_compatible_client_caches_json_schema_for_mapping_and_merge() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        response_format = payload["response_format"]
        assert response_format["type"] == "json_schema"
        schema_name = response_format["json_schema"]["name"]
        if schema_name == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        if schema_name == "datalink_semantic_mapping":
            return _completion_response(_complete_mapping_payload())
        assert schema_name == "datalink_semantic_merge"
        return _completion_response({"merges": []})

    client = _openai_client(handler)
    column = _column(0)
    client.map_batch((_mapping_input(column),))
    client.judge_merges((), (), ())

    assert [payload["response_format"]["json_schema"]["name"] for payload in requests] == [
        "datalink_semantic_mapping_probe",
        "datalink_semantic_mapping",
        "datalink_semantic_merge",
    ]
    assert requests[0]["messages"][1]["content"] == "Return the required empty JSON object now."
    assert "columns" not in requests[0]["messages"][1]["content"]


def test_openai_compatible_client_falls_back_to_json_object_after_schema_rejection() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        response_format = payload["response_format"]
        if response_format["type"] == "json_schema":
            assert response_format["json_schema"]["name"] == "datalink_semantic_mapping_probe"
            return httpx.Response(400, text="response format unsupported")
        if "capability probe" in payload["messages"][0]["content"]:
            return _completion_response({"concepts": [], "entities": []})
        if "data semantic analyzer" in payload["messages"][0]["content"]:
            return _completion_response(_complete_mapping_payload())
        return _completion_response({"merges": []})

    client = _openai_client(handler)
    column = _column(0)
    client.map_batch((_mapping_input(column),))
    client.judge_merges((), (), ())

    assert [payload["response_format"] for payload in requests] == [
        {
            "type": "json_schema",
            "json_schema": {
                "name": "datalink_semantic_mapping_probe",
                "strict": True,
                "schema": SemanticMappingResponse.model_json_schema(),
            },
        },
        {"type": "json_object"},
        {"type": "json_object"},
        {"type": "json_object"},
    ]


@pytest.mark.parametrize(
    "response_content",
    [
        {
            "concepts": [
                {
                    "name": "semantic_value",
                    "description": "Meaning for the supplied field.",
                    "columns": ["column:ds_demo:orders:field_0"],
                    "confidence": 0.9,
                }
            ],
            "entities": [],
        },
        {
            "concepts": [
                {
                    "name": "semantic_value",
                    "description": "Meaning for the supplied field.",
                    "aliases": [],
                    "columns": ["column:ds_demo:orders:field_0"],
                    "confidence": 0.9,
                    "unexpected": True,
                }
            ],
            "entities": [],
        },
        '```json\\n{"concepts": [], "entities": []}\\n```',
    ],
)
def test_openai_compatible_client_keeps_strict_validation_after_json_object_fallback(
    response_content: object,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["response_format"]["type"] == "json_schema":
            return httpx.Response(422, text="strict schema unsupported")
        if "capability probe" in payload["messages"][0]["content"]:
            return _completion_response({"concepts": [], "entities": []})
        return _completion_response(response_content)

    client = _openai_client(handler)
    column = _column(0)

    with pytest.raises(ModelResponseFormatError, match="invalid mapping JSON"):
        client.map_batch((_mapping_input(column),))

    assert [payload["response_format"]["type"] for payload in requests] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


def test_openai_compatible_client_does_not_fallback_after_schema_probe_timeout() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        raise httpx.ReadTimeout("upstream request timed out")

    client = _openai_client(handler)
    column = _column(0)

    with pytest.raises(ModelRequestTimeoutError, match="request timed out"):
        client.map_batch((_mapping_input(column),))

    assert [payload["response_format"]["type"] for payload in requests] == ["json_schema"]


def test_openai_compatible_client_does_not_fallback_after_schema_probe_server_error() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503, text="upstream unavailable")

    client = _openai_client(handler)
    column = _column(0)

    with pytest.raises(ModelUpstreamError, match="request failed"):
        client.map_batch((_mapping_input(column),))

    assert [payload["response_format"]["type"] for payload in requests] == ["json_schema"]


def test_openai_compatible_client_fails_when_both_structured_output_probes_fail() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(400, text="response format unsupported")

    client = _openai_client(handler)
    column = _column(0)

    with pytest.raises(ModelResponseError, match="does not support required structured output"):
        client.map_batch((_mapping_input(column),))

    assert [payload["response_format"]["type"] for payload in requests] == [
        "json_schema",
        "json_object",
    ]


def test_openai_compatible_client_fails_safely_without_model_configuration() -> None:
    client = OpenAICompatibleSemanticClient(
        Settings(llm_model="", llm_base_url="", llm_api_key=None)
    )
    column = _column(0)

    with pytest.raises(ModelConfigurationError, match="configuration is invalid"):
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )


def test_openai_compatible_client_classifies_request_timeout_without_upstream_details() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream request timed out")

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    with pytest.raises(ModelRequestTimeoutError, match="request timed out") as error:
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )

    assert "upstream" not in str(error.value)


def test_openai_compatible_client_classifies_http_failure_without_upstream_details() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="internal upstream detail")

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    with pytest.raises(ModelUpstreamError, match="request failed") as error:
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )

    assert "internal" not in str(error.value)


def test_openai_compatible_client_classifies_invalid_completion_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "datalink_semantic_mapping_probe":
            return _completion_response({"concepts": [], "entities": []})
        return httpx.Response(200, json={"choices": []})

    client = OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )
    column = _column(0)

    with pytest.raises(ModelResponseFormatError, match="incomplete response"):
        client.map_batch(
            (
                MappingColumnInput(
                    column_id=column.id,
                    column_name=column.name,
                    table_name="orders",
                    profile=_profile(column).to_read(),
                ),
            )
        )


def _semantic_node(
    node_id: str,
    name: str,
    *,
    node_type: DataLinkNodeType = DataLinkNodeType.CONCEPT,
    aliases: tuple[str, ...] = (),
    description: str | None = "业务说明",
) -> GraphNode:
    return GraphNode(
        id=node_id,
        type=node_type,
        name=name,
        description=description,
        aliases=aliases,
        properties={"provenance": "semantic_mapping"},
    )


def _represents(source_id: str, target_id: str, confidence: float = 0.9) -> GraphEdge:
    return GraphEdge(
        id=f"edge:{source_id}:{target_id}",
        source_id=source_id,
        target_id=target_id,
        type=DataLinkEdgeType.REPRESENTS,
        confidence=confidence,
        properties={"provenance": "semantic_mapping"},
    )


def _column(index: int, *, name: str | None = None) -> GraphNode:
    field_name = name or f"field_{index}"
    return GraphNode(
        id=f"column:ds_demo:orders:{field_name}",
        type=DataLinkNodeType.COLUMN,
        name=field_name,
        table_name="orders",
    )


def _profile(column: GraphNode, *, sensitive: bool = False) -> ColumnProfile:
    return ColumnProfile(
        id=f"profile:{column.id}",
        column_id=column.id,
        column_name=column.name,
        dtype="integer",
        null_rate=0.0,
        distinct_count=3,
        unique_rate=1.0,
        top_values=() if sensitive else (1, 2, 3),
        sample_values=(),
        is_sensitive=sensitive,
    )


def _valid_response(columns: Sequence[GraphNode], prefix: str) -> SemanticMappingResponse:
    concepts = [
        ConceptMapping(
            name=f"{prefix} {column.name}",
            description=f"Business meaning for {column.name}",
            aliases=[],
            columns=[column.id],
            confidence=0.8,
        )
        for column in columns
    ]
    return SemanticMappingResponse(
        concepts=concepts,
        entities=[
            EntityMapping(
                name=f"{prefix} order",
                description="Order fields grouped together",
                aliases=[],
                concept_names=[concept.name for concept in concepts],
                confidence=0.8,
            )
        ],
    )


def _completion_response(content: object) -> httpx.Response:
    """构造 OpenAI-compatible 的单条文本 completion。"""

    serialized = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"choices": [{"message": {"content": serialized}}]})


def _complete_mapping_payload() -> dict[str, object]:
    """提供一份满足全部必填字段的模型映射响应。"""

    return {
        "concepts": [
            {
                "name": "semantic_value",
                "description": "Meaning for the supplied field.",
                "aliases": ["alternative value"],
                "columns": ["column:ds_demo:orders:field_0"],
                "confidence": 0.9,
            }
        ],
        "entities": [
            {
                "name": "record_group",
                "description": "Group containing the supplied concept.",
                "aliases": [],
                "concept_names": ["semantic_value"],
                "confidence": 0.9,
            }
        ],
    }


def _openai_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAICompatibleSemanticClient:
    """创建使用受控 HTTP handler 的模型客户端。"""

    return OpenAICompatibleSemanticClient(
        Settings(
            llm_model="demo-model",
            llm_base_url="https://model.example/v1",
            llm_api_key="test-only-key",
        ),
        transport=httpx.MockTransport(handler),
    )


def _mapping_input(column: GraphNode) -> MappingColumnInput:
    """构造一条最小的合法模型映射输入。"""

    return MappingColumnInput(
        column_id=column.id,
        column_name=column.name,
        table_name="orders",
        profile=_profile(column).to_read(),
    )


def _assert_every_object_is_strict(schema: object) -> None:
    """递归断言 Pydantic 生成的所有对象层都符合 strict JSON Schema 约束。"""

    if isinstance(schema, dict):
        if schema.get("type") == "object":
            properties = schema.get("properties")
            assert isinstance(properties, dict)
            assert schema.get("additionalProperties") is False
            assert set(schema.get("required", ())) == set(properties)
        for value in schema.values():
            _assert_every_object_is_strict(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_every_object_is_strict(value)
