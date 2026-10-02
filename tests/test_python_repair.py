import pytest
from agent_runtime.contracts import AgentToolName, ToolObservation
from agent_runtime.python_repair import DeliverableIdentity, PythonRepairBudget
from application.python_diagnostics import python_diagnostic


def opened(*identities: DeliverableIdentity) -> PythonRepairBudget:
    budget = PythonRepairBudget()
    budget.open(identities or (DeliverableIdentity("P1", "markdown", "报告", 1),))
    return budget


def args(script: str, path: str = "outputs/a.md", deliverable_ids: list[str] | None = None) -> dict:
    return {
        "script": script,
        "output_paths": [path],
        "purpose": "生成报告",
        "deliverable_ids": deliverable_ids or ["P1"],
    }


def failure(code="SANDBOX_FAILED", **summary):
    return ToolObservation(
        tool_call_id="python_1",
        tool_name=AgentToolName.PYTHON,
        status="failed",
        summary={"error_code": code, "retryable": True, **summary},
    )


def test_python_diagnostic_keeps_location_without_runtime_values():
    diagnostic = python_diagnostic(
        "Traceback (most recent call last):\n"
        '  File "/workspace/work/analysis.py", line 2, in <module>\n'
        "KeyError: private_customer_value",
        exit_code=1,
        script="import pandas\nframe['private_customer_value']",
    )
    assert diagnostic.exception_type == "KeyError"
    assert diagnostic.line_number == 2
    assert diagnostic.code_line is not None
    assert "private_customer_value" not in diagnostic.model_dump_json()
    assert "/workspace" not in diagnostic.model_dump_json()
    unknown = python_diagnostic("private_custom_stderr", exit_code=137)
    assert unknown.exception_type is None
    assert unknown.exit_code == 137
    assert "private_custom_stderr" not in unknown.model_dump_json()


def test_python_repair_stops_repeated_failure_and_drops_process_output():
    budget = opened()
    diagnostic = python_diagnostic("KeyError: secret", exit_code=1).model_dump()
    first = budget.record(
        failure(diagnostic=diagnostic, stdout="secret", stderr="secret"),
        args("first"),
        ["P1"],
    )
    assert first.summary["python_failure"]["remaining_attempts"] == 2
    assert "secret" not in first.model_dump_json()
    repeated = budget.gate("second", args("first"))
    assert repeated.charge == "named"
    assert repeated.observation is not None
    assert repeated.observation.summary["reason_code"] == "PYTHON_REPAIR_UNCHANGED"
    second = budget.record(failure(diagnostic=diagnostic), args("changed"), ["P1"])
    assert second.summary["python_failure"]["remaining_attempts"] == 0
    assert second.summary["retryable"] is False
    stopped = budget.gate("third", args("changed_again"))
    assert stopped.charge == "drop"
    assert stopped.observation is not None
    assert stopped.observation.summary["reason_code"] == "PYTHON_REPAIR_EXHAUSTED"


def test_python_repair_has_finite_budget_and_never_repairs_timeout():
    budget = opened()
    for index in range(3):
        result = budget.record(failure(reason_code=f"FAILURE_{index}"), args(str(index)), ["P1"])
    assert result.summary["python_failure"]["attempt"] == 3
    assert result.summary["python_failure"]["remaining_attempts"] == 0
    timeout = opened().record(failure("SANDBOX_TIMEOUT"), args("x"), ["P1"])
    assert timeout.summary["retryable"] is False


def test_python_diagnostic_omits_fstring_literals_and_bounds_standard_message():
    diagnostic = python_diagnostic(
        '  File "analysis.py", line 1\nValueError: secret',
        exit_code=1,
        script='raise ValueError(f"private_customer_value {value}")',
    )
    assert diagnostic.code_line is None
    assert "private_customer_value" not in diagnostic.model_dump_json()
    long_message = python_diagnostic(
        "TypeError: missing " + "9" * 600 + " required positional argument", exit_code=1
    )
    assert len(long_message.message) <= 500


@pytest.mark.parametrize(
    "code",
    ["RUN_CANCELED", "SANDBOX_REJECTED", "SANDBOX_NETWORK_DENIED", "ARTIFACT_REJECTED"],
)
def test_python_repair_never_retries_security_or_cancellation(code):
    result = opened().record(failure(code), args("x"), ["P1"])
    assert result.summary["python_failure"]["remaining_attempts"] == 0
    assert result.summary["retryable"] is False


def test_python_repair_invalid_diagnostic_and_rejected_attempt_stay_bounded():
    budget = opened()
    for index in range(3):
        result = budget.record(
            failure(reason_code=f"FAILURE_{index}", diagnostic={"message": None}),
            args(str(index)),
            ["P1"],
        )
    assert result.summary["python_failure"]["diagnostic_facts"]["message"]
    rejected = budget.gate("fourth", args("new"))
    assert rejected.charge == "drop"
    assert rejected.observation is not None
    assert rejected.observation.summary["python_failure"]["attempt"] == 3
    assert rejected.observation.summary["python_failure"]["remaining_attempts"] == 0


def test_python_repair_rename_does_not_open_a_new_attempt():
    budget = opened()
    budget.record(failure(), args("open('outputs/a.md')", "outputs/a.md"), ["P1"])
    renamed = budget.gate("rename", args("open('outputs/renamed.md')", "outputs/renamed.md"))
    assert renamed.charge == "named"
    assert renamed.observation is not None
    assert renamed.observation.summary["reason_code"] == "PYTHON_REPAIR_UNCHANGED"
    changed = budget.gate("changed", args("open('outputs/a.md')\nprint(1)", "outputs/a.md"))
    assert changed.charge == "execute"


def test_python_repair_unknown_identity_does_not_create_an_account():
    budget = opened()
    unknown = budget.gate("unknown", args("print(1)", deliverable_ids=["P9"]))
    assert unknown.charge == "invalid"
    assert unknown.observation is not None
    assert unknown.observation.summary["reason_code"] == "PYTHON_DELIVERABLE_UNKNOWN"
    budget.record_invalid(unknown.observation)
    first = budget.record(failure(), args("print(1)"), ["P1"])
    assert first.summary["python_failure"]["remaining_attempts"] == 2


def test_python_repair_stopped_identity_does_not_charge_its_sibling():
    budget = opened(
        DeliverableIdentity("P1", "markdown", "第一份", 1),
        DeliverableIdentity("P2", "markdown", "第二份", 1),
    )
    budget.record(failure("SANDBOX_TIMEOUT"), args("x"), ["P1"])
    mixed = budget.gate("mixed", args("print(1)", "outputs/second.md", ["P1", "P2"]))
    assert mixed.charge == "drop"
    sibling = budget.record(failure(), args("print(2)", "outputs/second.md", ["P2"]), ["P2"])
    assert sibling.summary["python_failure"]["remaining_attempts"] == 2


def test_python_repair_success_resets_only_the_named_identity():
    budget = opened(
        DeliverableIdentity("P1", "markdown", "第一份", 1),
        DeliverableIdentity("P2", "markdown", "第二份", 1),
    )
    budget.record(failure("SANDBOX_TIMEOUT"), args("x"), ["P1"])
    budget.record(failure("SANDBOX_TIMEOUT"), args("y", deliverable_ids=["P2"]), ["P2"])
    assert budget.gate("blocked", args("again")).charge == "drop"
    success = ToolObservation(
        tool_call_id="python_ok",
        tool_name=AgentToolName.PYTHON,
        status="succeeded",
        summary={},
    )
    budget.record(success, args("fixed"), ["P1"])
    assert budget.gate("retry", args("fixed-again")).charge == "execute"
    assert budget.gate("other", args("other", deliverable_ids=["P2"])).charge == "drop"


def test_python_repair_kind_mismatch_does_not_charge_the_named_identity():
    budget = opened()
    mismatch = budget.gate("kind", args("print(1)", "outputs/table.csv"))
    assert mismatch.charge == "invalid"
    assert mismatch.observation is not None
    findings = mismatch.observation.summary["validation_findings"]
    assert findings[0]["code"] == "PYTHON_DELIVERABLE_KIND"
    budget.record_invalid(mismatch.observation)
    first = budget.record(failure(), args("print(1)"), ["P1"])
    assert first.summary["python_failure"]["remaining_attempts"] == 2
