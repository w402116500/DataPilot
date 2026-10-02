from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    script = Path(__file__).parents[1] / "scripts" / "evaluate_run_protocol_routing.py"
    spec = importlib.util.spec_from_file_location("routing_evaluation", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_routing_evaluation_separates_both_misroute_directions() -> None:
    module = _load_module()
    rows = [
        {
            "sample_id": "general",
            "category": "general_concept",
            "expected_protocol": "general-task",
            "expected_mode": "general-task",
            "repair_expected": False,
            "allowed_terminal_kind": ["completed"],
            "max_opening_model_calls": 1,
            "max_repair_calls": 0,
            "forbidden_resources": ["datalink", "sql", "python", "claim", "artifact"],
        },
        {
            "sample_id": "analysis",
            "category": "current_data",
            "expected_protocol": "data-analysis",
            "expected_mode": "ready",
            "allowed_execution_modes": ["ready", "discovery"],
            "repair_expected": True,
            "allowed_terminal_kind": ["completed", "partial"],
            "max_opening_model_calls": 2,
            "max_repair_calls": 1,
            "forbidden_resources": [],
        },
    ]
    predictions = [
        {
            "sample_id": "general",
            "protocol_id": "data-analysis",
            "opening_model_calls": 1,
            "opening_repair_calls": 0,
            "planning_mode": "general-task",
            "terminal_kind": "completed",
            "resource_kinds": [],
        },
        {
            "sample_id": "analysis",
            "protocol_id": "general-task",
            "opening_model_calls": 2,
            "opening_repair_calls": 1,
            "planning_mode": "general-task",
            "terminal_kind": "completed",
            "resource_kinds": [],
        },
    ]

    result = module.evaluate(rows, predictions)

    assert result["protocol_accuracy"] == 0
    assert result["first_opening_legal_rate"] == 0.5
    assert result["general_as_analysis_count"] == 1
    assert result["analysis_as_general_count"] == 1
    assert result["execution_mode_mismatch_count"] == 1
    assert result["current_data_as_general_count"] == 1
    assert result["unresolved_protocol_count"] == 0
    assert result["opening_calls_over_limit"] == 0
    assert result["repair_calls_over_limit"] == 0


def test_routing_evaluation_keeps_unresolved_separate_from_misrouting() -> None:
    module = _load_module()
    rows = [
        {
            "sample_id": "analysis",
            "category": "mixed",
            "expected_protocol": "data-analysis",
            "expected_mode": "ready",
            "repair_expected": False,
            "allowed_terminal_kind": ["completed", "partial"],
            "max_opening_model_calls": 1,
            "max_repair_calls": 0,
            "forbidden_resources": [],
        }
    ]
    predictions = [
        {
            "sample_id": "analysis",
            "protocol_id": None,
            "opening_model_calls": 1,
            "opening_repair_calls": 0,
            "planning_mode": None,
            "terminal_kind": "failed",
            "resource_kinds": [],
        }
    ]

    result = module.evaluate(rows, predictions)

    assert result["protocol_accuracy"] == 0
    assert result["first_opening_legal_rate"] == 0
    assert result["analysis_as_general_count"] == 0
    assert result["mixed_as_general_count"] == 0
    assert result["unresolved_protocol_count"] == 1


def test_repair_expected_allows_legal_first_round_without_repair() -> None:
    module = _load_module()
    rows = [
        {
            "sample_id": "clarification",
            "category": "clarification",
            "expected_protocol": "data-analysis",
            "expected_mode": "clarification",
            "repair_expected": True,
            "allowed_terminal_kind": ["clarification"],
            "max_opening_model_calls": 2,
            "max_repair_calls": 1,
            "forbidden_resources": ["sql", "artifact"],
        }
    ]
    predictions = [
        {
            "sample_id": "clarification",
            "protocol_id": "data-analysis",
            "opening_model_calls": 1,
            "opening_repair_calls": 0,
            "planning_mode": "clarification",
            "terminal_kind": "clarification",
            "resource_kinds": [],
        }
    ]

    result = module.evaluate(rows, predictions)

    assert result["repair_expectation_mismatch_count"] == 0
    assert result["repair_calls_over_limit"] == 0


def test_artifact_projection_reads_public_type_field() -> None:
    module = _load_module()

    assert module._artifact_types(
        [
            {"id": "table-1", "type": "table"},
            {"id": "markdown-1", "type": "markdown", "kind": "wrong-field"},
            {"id": "ignored", "kind": "file"},
        ]
    ) == ["markdown", "table"]
