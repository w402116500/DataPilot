"""DataLink 与模型之间使用的严格 JSON 输入输出结构。"""

from __future__ import annotations

from typing import Literal

from contracts.datalink import DataLinkColumnProfileRead
from pydantic import BaseModel, ConfigDict, Field


class _ModelSchema(BaseModel):
    """模型边界只接受声明字段，和严格 JSON Schema 保持同一契约。"""

    model_config = ConfigDict(extra="forbid")


class MappingColumnInput(_ModelSchema):
    """发送给语义模型的一列受控信息，不包含敏感真实样例。"""

    column_id: str = Field(min_length=1, max_length=300)
    column_name: str = Field(min_length=1, max_length=200)
    table_name: str = Field(min_length=1, max_length=200)
    profile: DataLinkColumnProfileRead


class ConceptMapping(_ModelSchema):
    """模型为一个或多个字段给出的完整业务属性。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(max_length=20)
    columns: list[str] = Field(min_length=1, max_length=15)
    confidence: float = Field(ge=0, le=1)


class EntityMapping(_ModelSchema):
    """模型用当前批次 Concept 组织出的业务实体。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(max_length=20)
    concept_names: list[str] = Field(min_length=1, max_length=15)
    confidence: float = Field(ge=0, le=1)


class SemanticMappingResponse(_ModelSchema):
    """一次字段映射调用唯一接受的 JSON 顶层结构。"""

    concepts: list[ConceptMapping] = Field(max_length=15)
    entities: list[EntityMapping] = Field(max_length=15)


class SemanticNodeInput(_ModelSchema):
    """同义节点判断时给模型的有限节点说明。"""

    id: str = Field(min_length=1, max_length=300)
    type: Literal["concept", "entity"]
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class MergeCandidate(_ModelSchema):
    """Embedding 预筛或全量比较产生的一对同类节点。"""

    new_id: str = Field(min_length=1, max_length=300)
    existing_id: str = Field(min_length=1, max_length=300)
    type: Literal["concept", "entity"]


class MergeDecision(_ModelSchema):
    """模型确认一个新节点应合并进哪个已存在节点。"""

    new_id: str = Field(min_length=1, max_length=300)
    existing_id: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)


class SemanticMergeResponse(_ModelSchema):
    """同义节点判断的严格 JSON 顶层结构。"""

    merges: list[MergeDecision] = Field(max_length=200)
