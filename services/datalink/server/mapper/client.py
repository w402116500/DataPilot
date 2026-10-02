"""受控的 OpenAI-compatible 语义模型客户端。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from threading import Lock
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from server.config import Settings
from server.mapper.schemas import (
    MappingColumnInput,
    MergeCandidate,
    SemanticMappingResponse,
    SemanticMergeResponse,
    SemanticNodeInput,
)

_EMBEDDING_CANDIDATE_THRESHOLD = 0.75
_MERGE_TEMPERATURE = 0.0
_MAPPING_SYSTEM_PROMPT = """You are a data semantic analyzer. Given tabular columns and
their metadata, identify the semantic concepts and record groups they represent.

Return exactly one JSON object that conforms to the required output contract. Do not emit markdown,
comments, prose, or fields outside that schema. Every object property is required, including
arrays that have no values.

JSON keys stay in English. Concept and Entity name and description must be Simplified Chinese
readable by a Chinese-speaking analyst. Do not use a physical column name, table name, or English
identifier as name. Put physical names and English synonyms into aliases only.

Example for an input that includes one supplied column ID:
{
  "concepts": [
    {
      "name": "normalized_concept_name",
      "description": "列出字段共享的业务含义。",
      "aliases": ["physical_or_english_name"],
      "columns": ["column:source:table:field"],
      "confidence": 0.95
    }
  ],
  "entities": [
    {
      "name": "normalized_entity_name",
      "description": "所列概念描述的记录分组。",
      "aliases": ["physical_or_english_name"],
      "concept_names": ["normalized_concept_name"],
      "confidence": 0.95
    }
  ]
}

The example names and IDs are format placeholders, not fixed business meanings. Real name values
must still be Simplified Chinese. Use only supplied column IDs. Merge columns with the same
semantic meaning into one concept.
If no concepts or entities can be justified, return exactly {"concepts": [], "entities": []}."""

_MERGE_SYSTEM_PROMPT = """You are a data ontology merger. Decide which NEW concepts or
entities mean the same real-world thing as an EXISTING concept or entity, and which new nodes
must remain separate.

You will receive JSON with three arrays: new_nodes, existing_nodes, and candidates. Only
candidate pairs may be merged. The new_id must be mapped to the existing_id; the existing node
is kept and the new node is absorbed. Never invent node IDs, nodes, edges, or candidate pairs.

Return exactly one JSON object that conforms to the required output contract. Do not emit markdown,
comments, prose, or fields outside that schema. Every object property is required, including
arrays that have no values.

Example:
{
  "merges": [
    {
      "new_id": "concept:provided-new-id",
      "existing_id": "concept:provided-existing-id",
      "reason": "both labels denote the same supplied semantic concept",
      "confidence": 0.95
    }
  ]
}

Use an empty array when no candidate should be merged: {"merges": []}.

Merge only when the two nodes represent the same supplied semantic meaning, even if their names
differ or one name is Simplified Chinese and the other is English or a physical identifier.
Do not merge nodes merely because their labels share a word or field type. Use confidence
0.95 for an obvious match, 0.8 for a reasonable match, and 0.6 for an uncertain match; omit
uncertain pairs below 0.6.

Review every supplied candidate before returning the JSON object."""
_CAPABILITY_PROBE_SYSTEM_PROMPT = """This is a structured-output capability probe.
Return exactly this JSON object: {"concepts": [], "entities": []}. Do not emit markdown,
comments, prose, or any other fields."""

_OutputMode = Literal["json_schema", "json_object"]


class ModelConfigurationError(RuntimeError):
    """模型配置不完整时的安全失败，不携带配置内容。"""


class ModelResponseError(RuntimeError):
    """模型网络、响应或 JSON 校验失败时的安全失败。"""


class ModelRequestTimeoutError(ModelResponseError):
    """模型请求在固定时限内未完成，不包含上游响应或连接信息。"""


class ModelUpstreamError(ModelResponseError):
    """模型服务的网络或 HTTP 故障，不包含上游地址、状态正文或凭据。"""


class ModelResponseFormatError(ModelResponseError):
    """模型返回无法按既定 JSON 契约读取的内容，不保留原始文本。"""


class ModelResponseSchemaUnsupportedError(ModelResponseError):
    """严格 Schema 探测被兼容端点拒绝时的内部分类，不携带上游详情。"""


class SemanticModelClient:
    """映射器与合并器依赖的窄模型端口，便于替换为受控测试双。"""

    def map_batch(self, columns: Sequence[MappingColumnInput]) -> SemanticMappingResponse:
        """一次调用产生一批字段的结构化语义映射。"""

        raise NotImplementedError

    def judge_merges(
        self,
        new_nodes: Sequence[SemanticNodeInput],
        existing_nodes: Sequence[SemanticNodeInput],
        candidates: Sequence[MergeCandidate],
    ) -> SemanticMergeResponse:
        """判断给定同类节点对是否实际表达同一业务含义。"""

        raise NotImplementedError

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        """可选地为节点文本生成向量；不可用时返回 None 而不是失败 Build。"""

        raise NotImplementedError


class OpenAICompatibleSemanticClient(SemanticModelClient):
    """通过 DataLink 自己的环境配置调用 OpenAI-compatible Chat API。"""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self._output_mode: _OutputMode | None = None
        self._capability_lock = Lock()

    def map_batch(self, columns: Sequence[MappingColumnInput]) -> SemanticMappingResponse:
        """请求严格 Schema 约束的完整字段语义映射。"""

        response_text = self._complete(
            system_prompt=_MAPPING_SYSTEM_PROMPT,
            payload={"columns": [column.model_dump(mode="json") for column in columns]},
            response_model=SemanticMappingResponse,
            schema_name="datalink_semantic_mapping",
        )
        try:
            return SemanticMappingResponse.model_validate_json(response_text, strict=True)
        except ValidationError as exc:
            raise ModelResponseFormatError("DataLink model returned invalid mapping JSON") from exc

    def judge_merges(
        self,
        new_nodes: Sequence[SemanticNodeInput],
        existing_nodes: Sequence[SemanticNodeInput],
        candidates: Sequence[MergeCandidate],
    ) -> SemanticMergeResponse:
        """让模型只在候选对中确认合并，禁止它创建或修改业务节点。"""

        response_text = self._complete(
            system_prompt=_MERGE_SYSTEM_PROMPT,
            payload={
                "new_nodes": [node.model_dump(mode="json") for node in new_nodes],
                "existing_nodes": [node.model_dump(mode="json") for node in existing_nodes],
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            },
            temperature=_MERGE_TEMPERATURE,
            response_model=SemanticMergeResponse,
            schema_name="datalink_semantic_merge",
        )
        try:
            return SemanticMergeResponse.model_validate_json(response_text, strict=True)
        except ValidationError as exc:
            raise ModelResponseFormatError("DataLink model returned invalid merge JSON") from exc

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...] | None:
        """可选 Embedding 只用于缩小合并候选；失败时由模型直接比较。"""

        if not texts or not self.settings.embedding_model or not self.settings.model_configured:
            return None
        try:
            payload = {"model": self.settings.embedding_model, "input": list(texts)}
            response = self._request("embeddings", payload)
            values = tuple(
                tuple(float(value) for value in item["embedding"]) for item in response["data"]
            )
        except (KeyError, TypeError, ValueError, ModelResponseError):
            return None
        return values if len(values) == len(texts) else None

    def _complete(
        self,
        system_prompt: str,
        payload: dict[str, object],
        *,
        temperature: float | None = None,
        response_model: type[BaseModel],
        schema_name: str,
    ) -> str:
        """一次性等待已探测输出模式的响应，不使用业务层自动重试或语法修复。"""

        output_mode = self._require_output_mode()
        return self._chat_completion(
            system_prompt=system_prompt,
            user_content=json.dumps(payload, ensure_ascii=True),
            response_format=_response_format(response_model, schema_name, output_mode),
            temperature=temperature,
        )

    def _require_output_mode(self) -> _OutputMode:
        """首次真实语义调用前探测并缓存本进程可用的结构化输出模式。"""

        with self._capability_lock:
            if self._output_mode is not None:
                return self._output_mode
            try:
                self._run_mapping_capability_probe("json_schema")
            except (ModelResponseSchemaUnsupportedError, ModelResponseFormatError):
                pass
            else:
                self._output_mode = "json_schema"
                return self._output_mode

            try:
                self._run_mapping_capability_probe("json_object")
            except ModelResponseError as exc:
                raise ModelResponseError(
                    "DataLink model does not support required structured output"
                ) from exc
            self._output_mode = "json_object"
            return self._output_mode

    def _run_mapping_capability_probe(self, output_mode: _OutputMode) -> None:
        """验证一个无业务数据的完整空映射响应，绝不将探测结果写入图谱。"""

        response_text = self._chat_completion(
            system_prompt=_CAPABILITY_PROBE_SYSTEM_PROMPT,
            user_content="Return the required empty JSON object now.",
            response_format=_response_format(
                SemanticMappingResponse,
                "datalink_semantic_mapping_probe",
                output_mode,
            ),
            classify_schema_unsupported=output_mode == "json_schema",
        )
        try:
            response = SemanticMappingResponse.model_validate_json(response_text, strict=True)
        except ValidationError as exc:
            if output_mode == "json_schema":
                raise ModelResponseSchemaUnsupportedError(
                    "DataLink model rejected strict JSON Schema output"
                ) from exc
            raise ModelResponseFormatError(
                "DataLink model returned invalid structured output probe"
            ) from exc
        if response.concepts or response.entities:
            if output_mode == "json_schema":
                raise ModelResponseSchemaUnsupportedError(
                    "DataLink model rejected strict JSON Schema output"
                )
            raise ModelResponseFormatError(
                "DataLink model returned invalid structured output probe"
            )

    def _chat_completion(
        self,
        *,
        system_prompt: str,
        user_content: str,
        response_format: dict[str, object],
        temperature: float | None = None,
        classify_schema_unsupported: bool = False,
    ) -> str:
        """执行一个完整响应请求并仅返回非空文本内容。"""

        response = self._request(
            "chat/completions",
            {
                "model": self.settings.llm_model,
                "temperature": (
                    self.settings.llm_temperature if temperature is None else temperature
                ),
                "max_tokens": self.settings.llm_max_tokens,
                "response_format": response_format,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            },
            classify_schema_unsupported=classify_schema_unsupported,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ModelResponseFormatError(
                "DataLink model returned an incomplete response"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelResponseFormatError("DataLink model returned an empty response")
        return content

    def _request(
        self,
        endpoint: str,
        payload: dict[str, object],
        *,
        classify_schema_unsupported: bool = False,
    ) -> dict[str, object]:
        """以固定超时访问模型，不向日志或异常传播上游响应、URL 或密钥。"""

        if not self.settings.model_configured:
            raise ModelConfigurationError("DataLink model configuration is invalid")
        if self.settings.llm_provider != "openai-compatible":
            raise ModelConfigurationError("DataLink model provider is unsupported")
        api_key = self.settings.llm_api_key
        if api_key is None:
            raise ModelConfigurationError("DataLink model configuration is invalid")
        url = f"{self.settings.llm_base_url.rstrip('/')}/{endpoint}"
        try:
            with httpx.Client(
                timeout=self.settings.llm_timeout_seconds, transport=self.transport
            ) as client:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
                    json=payload,
                )
                response.raise_for_status()
                parsed = response.json()
        except httpx.TimeoutException as exc:
            raise ModelRequestTimeoutError("DataLink model request timed out") from exc
        except httpx.HTTPStatusError as exc:
            if classify_schema_unsupported and exc.response.status_code in {400, 404, 422}:
                raise ModelResponseSchemaUnsupportedError(
                    "DataLink model rejected strict JSON Schema output"
                ) from exc
            raise ModelUpstreamError("DataLink model request failed") from exc
        except httpx.HTTPError as exc:
            raise ModelUpstreamError("DataLink model request failed") from exc
        except ValueError as exc:
            raise ModelResponseFormatError("DataLink model returned an invalid response") from exc
        if not isinstance(parsed, dict):
            raise ModelResponseFormatError("DataLink model returned an invalid response")
        return parsed


def should_keep_embedding_candidate(similarity: float) -> bool:
    """集中保存阶段三固定的预筛阈值，避免合并器复制该规则。"""

    return similarity >= _EMBEDDING_CANDIDATE_THRESHOLD


def _response_format(
    response_model: type[BaseModel], schema_name: str, output_mode: _OutputMode
) -> dict[str, object]:
    """按已验证模式创建请求格式；json_object 仍由本地严格 DTO 守住契约。"""

    if output_mode == "json_object":
        return {"type": "json_object"}

    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": response_model.model_json_schema(),
        },
    }
