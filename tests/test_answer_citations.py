from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_runtime.answer_citations import citation_numbers, cited_material_numbers
from agent_runtime.answer_materials import material_from_units


@pytest.mark.parametrize("fixture", json.loads(
    (Path(__file__).parent / "fixtures" / "answer_citations.json").read_text(encoding="utf-8")
), ids=lambda item: item["name"])
def test_shared_citation_grammar(fixture):
    assert citation_numbers(fixture["markdown"]) == fixture["numbers"]


def test_existence_only_and_first_appearance_order():
    assert cited_material_numbers("错误语义也只校验引用[2][1][2][999]", [1, 2]) == ([2, 1], [999])


def test_material_crops_complete_units_and_discloses_scope():
    material = material_from_units(kind="table", title="数值", sources=[],
                                   units=[{"value": 123456}, {"value": 987654}],
                                   scope="查询返回的结果", max_chars=20)
    assert material is not None
    assert json.loads(material.content) == {"value": 123456}
    assert "1/2" in material.scope
    assert "截断" in material.scope
