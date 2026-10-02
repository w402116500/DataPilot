"""最终答案材料与正文引用的跨层契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChartExplanation(BaseModel):
    """The analysis model supplies this with the chart generation call."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2_000)
    main_finding: str = Field(min_length=1, max_length=2_000)
    source_refs: list[str] = Field(min_length=1, max_length=32)


class ChartIntent(BaseModel):
    """模型声明的绘图意图；不承载行数、范围或统计事实。"""

    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1, max_length=500)
    source_ref: str = Field(min_length=1, max_length=120)
    x_field: str = Field(min_length=1, max_length=120)
    y_metric: str = Field(min_length=1, max_length=120)
    group_by: str | None = Field(default=None, max_length=120)
    sort: Literal["asc", "desc", "query"] = "query"


class ChartSpec(BaseModel):
    """由系统从成功查询结果和绘图意图生成的图表事实摘要。"""

    model_config = ConfigDict(extra="forbid")

    intent: ChartIntent
    row_count: int = Field(ge=0)
    coverage: str = Field(min_length=1, max_length=500)
    verified_columns: list[str] = Field(default_factory=list, max_length=100)
    metric_min: float | None = None
    metric_max: float | None = None
    source_ref: str = Field(min_length=1, max_length=120)
    confirmed_joins: list[str] = Field(default_factory=list, max_length=32)
    limitations: list[str] = Field(default_factory=list, max_length=16)


class AnswerMaterialSource(BaseModel):
    """材料对应的系统来源；这些字段不会注入最终答案模型。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["audit", "artifact", "schema", "datalink"]
    ref_id: str | None = Field(default=None, min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=240)
    schema_revision: int | None = Field(default=None, ge=1)
    graph_version: str | None = Field(default=None, max_length=120)


class AnswerMaterial(BaseModel):
    """最终答案模型可见的一份有界材料。"""

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1, le=999)
    kind: Literal[
        "query_result", "chart_spec", "table", "schema", "relationship",
        "chart", "markdown", "file", "warning",
    ]
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=20_000)
    scope: str | None = Field(default=None, max_length=2_000)
    sources: list[AnswerMaterialSource] = Field(default_factory=list, max_length=100)
    supports: list[str] = Field(default_factory=list, max_length=16)
    limitations: list[str] = Field(default_factory=list, max_length=16)


class AnswerEvidenceSnapshot(BaseModel):
    """保存实际发送给最终模型的材料及正文引用结果。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    materials: list[AnswerMaterial] = Field(default_factory=list, max_length=100)
    cited_numbers: list[int] = Field(default_factory=list, max_length=100)
    invalid_numbers: list[int] = Field(default_factory=list, max_length=100)
