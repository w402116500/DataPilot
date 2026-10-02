"""Run-local Python failure feedback and per-deliverable repair budgets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from contracts.python_diagnostics import PythonDiagnostic, PythonFailureFeedback
from pydantic import ValidationError

from agent_runtime.contracts import (
    AgentToolName,
    AnalysisArtifactRequirementDraft,
    ToolObservation,
)

_DECLARED_PATH = "__DECLARED_PATH__"
_FILE_SUFFIXES = {".csv", ".json", ".png", ".svg", ".tsv", ".txt", ".xlsx"}
PythonCharge = Literal["execute", "named", "invalid", "drop"]


@dataclass(frozen=True)
class DeliverableIdentity:
    """服务端分配的正式产物身份。模型只能引用，不能自行编号。"""

    id: str
    kind: str
    description: str
    minimum_count: int


def assign_deliverable_identities(
    requirements: Sequence[AnalysisArtifactRequirementDraft],
) -> tuple[DeliverableIdentity, ...]:
    """按计划中的产物要求顺序分配 P1、P2……"""

    return tuple(
        DeliverableIdentity(
            id=f"P{index}",
            kind=item.kind,
            description=item.description,
            minimum_count=item.minimum_count,
        )
        for index, item in enumerate(requirements, start=1)
    )


@dataclass(frozen=True)
class PythonCallGate:
    """沙箱前的预算决定。drop 和已构造的拒绝不再改账本。"""

    observation: ToolObservation | None
    charge: PythonCharge
    deliverable_ids: tuple[str, ...] = ()


@dataclass
class _DeliverableLedger:
    attempts: int = 0
    failed_arguments: set[str] = field(default_factory=set)
    error_counts: dict[str, int] = field(default_factory=dict)
    stopped: bool = False


class PythonRepairBudget:
    """Each planned deliverable keeps its own three-attempt repair ledger."""

    def __init__(self) -> None:
        self._accounts: dict[str, _DeliverableLedger] = {}
        self._kinds: dict[str, str] = {}
        self._invalid = _DeliverableLedger()

    def open(self, identities: Sequence[DeliverableIdentity]) -> None:
        for item in identities:
            self._accounts.setdefault(item.id, _DeliverableLedger())
            self._kinds[item.id] = item.kind

    def gate(self, tool_call_id: str, arguments: Mapping[str, object]) -> PythonCallGate:
        if not self._accounts:
            return PythonCallGate(observation=None, charge="execute")
        raw_ids = arguments.get("deliverable_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or not all(isinstance(item, str) and item for item in raw_ids)
        ):
            return PythonCallGate(
                observation=_rejection(tool_call_id, "PYTHON_DELIVERABLE_UNKNOWN"),
                charge="invalid",
            )
        deliverable_ids = tuple(dict.fromkeys(raw_ids))
        if any(item not in self._accounts for item in deliverable_ids):
            return PythonCallGate(
                observation=_rejection(tool_call_id, "PYTHON_DELIVERABLE_UNKNOWN"),
                charge="invalid",
            )
        named = tuple(self._accounts[item] for item in deliverable_ids)
        if any(item.stopped for item in named):
            return PythonCallGate(
                observation=_exhausted_observation(tool_call_id, named),
                charge="drop",
                deliverable_ids=deliverable_ids,
            )
        output_paths = arguments.get("output_paths")
        if isinstance(output_paths, list) and all(isinstance(item, str) for item in output_paths):
            mismatched = [
                item
                for item in deliverable_ids
                if not _kind_satisfied(self._kinds[item], output_paths)
            ]
            if mismatched:
                return PythonCallGate(
                    observation=_rejection(
                        tool_call_id,
                        "PYTHON_DELIVERABLE_KIND",
                        findings=[
                            {
                                "field": "deliverable_ids",
                                "code": "PYTHON_DELIVERABLE_KIND",
                                "message": "声明的输出路径种类与点名的交付物身份不一致。",
                            }
                        ],
                    ),
                    charge="invalid",
                )
        fingerprint = _arguments_key(arguments)
        matched = tuple(
            item for item in deliverable_ids if fingerprint in self._accounts[item].failed_arguments
        )
        if matched:
            return PythonCallGate(
                observation=_rejection(tool_call_id, "PYTHON_REPAIR_UNCHANGED", retryable=True),
                charge="named",
                deliverable_ids=matched,
            )
        return PythonCallGate(observation=None, charge="execute", deliverable_ids=deliverable_ids)

    def record(
        self,
        observation: ToolObservation,
        arguments: Mapping[str, object],
        deliverable_ids: Sequence[str] = (),
    ) -> ToolObservation:
        ids = dict.fromkeys(deliverable_ids)
        ledgers = tuple(self._accounts[item] for item in ids if item in self._accounts)
        if not ledgers:
            return observation
        if observation.status == "succeeded":
            for ledger in ledgers:
                ledger.attempts = 0
                ledger.failed_arguments.clear()
                ledger.error_counts.clear()
                ledger.stopped = False
            return observation
        return self._record_ledgers(observation, arguments, ledgers)

    def has_open_deliverable(self) -> bool:
        """其它身份仍可执行时，一次失败不能结束整个 Run。"""

        return any(not ledger.stopped for ledger in self._accounts.values())

    def record_invalid(self, observation: ToolObservation) -> ToolObservation:
        if observation is None or self._invalid.stopped:
            return observation
        return self._record_ledgers(observation, {"invalid": True}, (self._invalid,))

    def _record_ledgers(
        self,
        observation: ToolObservation,
        arguments: Mapping[str, object],
        ledgers: Sequence[_DeliverableLedger],
    ) -> ToolObservation:
        signature = _failure_signature(observation)
        repairable = _repairable(observation)
        noted: list[tuple[int, int, bool]] = []
        for ledger in ledgers:
            ledger.attempts = min(ledger.attempts + 1, 3)
            ledger.failed_arguments.add(_arguments_key(arguments))
            ledger.error_counts[signature] = ledger.error_counts.get(signature, 0) + 1
            exhausted = ledger.attempts >= 3 or ledger.error_counts[signature] >= 2
            ledger.stopped = ledger.stopped or exhausted or not repairable
            remaining = 0 if ledger.stopped else 3 - ledger.attempts
            noted.append((ledger.attempts, remaining, exhausted or not repairable))
        attempt = max(item[0] for item in noted)
        remaining = min(item[1] for item in noted)
        exhausted = any(item[2] for item in noted)
        return _with_feedback(observation, signature, attempt, remaining, exhausted)


def _kind_satisfied(kind: str, output_paths: Sequence[str]) -> bool:
    if kind == "chart":
        return any(
            path.startswith("charts/") and path.endswith((".png", ".svg")) for path in output_paths
        )
    if kind == "markdown":
        return any(
            path == "report.md" or (path.startswith("outputs/") and path.endswith(".md"))
            for path in output_paths
        )
    if kind == "file":
        return any(
            path.startswith("outputs/") and PurePosixPath(path).suffix in _FILE_SUFFIXES
            for path in output_paths
        )
    return False


def _declared_paths(arguments: Mapping[str, object]) -> list[str]:
    found: list[str] = []
    output_paths = arguments.get("output_paths")
    if isinstance(output_paths, list):
        found.extend(item for item in output_paths if isinstance(item, str) and item)
    for key in ("chart_descriptions", "chart_intents"):
        items = arguments.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("relative_path")
            if isinstance(path, str) and path:
                found.append(path)
    unique = list(dict.fromkeys(found))
    unique.sort(key=len, reverse=True)
    return unique


def _replace_paths(value: object, paths: Sequence[str]) -> object:
    if isinstance(value, str):
        for path in paths:
            value = value.replace(path, _DECLARED_PATH)
        return value
    if isinstance(value, list):
        return [_replace_paths(item, paths) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_paths(item, paths)
            for key, item in value.items()
            if key not in {"purpose", "deliverable_ids"}
        }
    return value


def _arguments_key(arguments: Mapping[str, object]) -> str:
    # Purpose text and deliverable ids cannot turn a renamed file into a new attempt.
    payload = {
        key: value for key, value in arguments.items() if key not in {"purpose", "deliverable_ids"}
    }
    normalized = _replace_paths(payload, _declared_paths(arguments))
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _failure_signature(observation: ToolObservation) -> str:
    summary = {
        key: value
        for key, value in observation.summary.items()
        if key not in {"stdout", "stderr", "diagnostic", "python_failure"}
    }
    findings = summary.get("validation_findings", [])
    findings = findings if isinstance(findings, list) else []
    code = str(
        summary.get("reason_code")
        or next(
            (item["code"] for item in findings if isinstance(item, dict) and item.get("code")),
            summary.get("error_code", "PYTHON_UNKNOWN_FAILURE"),
        )
    )
    diagnostic = _diagnostic(observation)
    if code == "SANDBOX_FAILED":
        code = (
            "PYTHON_EXECUTION_FAILED"
            if diagnostic and diagnostic.exception_type
            else "PYTHON_UNKNOWN_FAILURE"
        )
    violations = [
        str(item.get("field", item.get("code", "arguments")))[:160]
        for item in findings[:12]
        if isinstance(item, dict)
    ]
    return json.dumps(
        [
            code,
            violations,
            diagnostic.exception_type if diagnostic else None,
            diagnostic.line_number if diagnostic else None,
            diagnostic.message if diagnostic else None,
        ],
        ensure_ascii=False,
    )


def _diagnostic(observation: ToolObservation) -> PythonDiagnostic | None:
    diagnostic_value = observation.summary.get("diagnostic")
    try:
        return (
            PythonDiagnostic.model_validate(diagnostic_value)
            if isinstance(diagnostic_value, dict)
            else None
        )
    except ValidationError:
        return PythonDiagnostic(message="未获得有效的 Python 异常诊断。")


def _repairable(observation: ToolObservation) -> bool:
    summary = observation.summary
    fallback = summary.get("error_code") == "MODEL_OUTPUT_INVALID"
    repairable = summary.get("retryable", fallback) is True
    if summary.get("error_code") in {
        "SANDBOX_TIMEOUT",
        "SANDBOX_REJECTED",
        "SANDBOX_NETWORK_DENIED",
        "RUN_CANCELED",
        "ARTIFACT_REJECTED",
    }:
        return False
    diagnostic = _diagnostic(observation)
    if diagnostic and diagnostic.exception_type == "PermissionError":
        return False
    return repairable


def _rejection(
    tool_call_id: str,
    reason_code: str,
    *,
    retryable: bool = True,
    findings: list[dict[str, object]] | None = None,
) -> ToolObservation:
    summary: dict[str, object] = {
        "error_code": "MODEL_OUTPUT_INVALID",
        "reason_code": reason_code,
        "retryable": retryable,
        "execution_status": "not_started",
    }
    if findings:
        summary["validation_findings"] = findings
    return ToolObservation(
        tool_call_id=tool_call_id,
        tool_name=AgentToolName.PYTHON,
        status="failed",
        summary=summary,
    )


def _exhausted_observation(
    tool_call_id: str, ledgers: Sequence[_DeliverableLedger]
) -> ToolObservation:
    attempt = max((ledger.attempts for ledger in ledgers), default=1)
    feedback = PythonFailureFeedback(
        error_code="PYTHON_REPAIR_EXHAUSTED",
        retryable=False,
        attempt=max(attempt, 1),
        remaining_attempts=0,
        repair_constraints=["停止当前 Python 交付物，说明未完成部分。"],
    )
    return ToolObservation(
        tool_call_id=tool_call_id,
        tool_name=AgentToolName.PYTHON,
        status="failed",
        summary={
            "error_code": "MODEL_OUTPUT_INVALID",
            "reason_code": "PYTHON_REPAIR_EXHAUSTED",
            "retryable": False,
            "execution_status": "not_started",
            "repair_exhausted": True,
            "python_failure": feedback.model_dump(mode="json"),
        },
    )


def _with_feedback(
    observation: ToolObservation,
    signature: str,
    attempt: int,
    remaining: int,
    exhausted: bool,
) -> ToolObservation:
    summary = {
        key: value
        for key, value in observation.summary.items()
        if key not in {"stdout", "stderr", "diagnostic", "python_failure"}
    }
    findings = summary.get("validation_findings", [])
    findings = findings if isinstance(findings, list) else []
    payload = json.loads(signature)
    code = str(payload[0])
    violations = [str(item) for item in payload[1]]
    diagnostic = _diagnostic(observation)
    constraints = ["不得原样重试失败的脚本和参数。"]
    if violations:
        constraints.append("按 violations 和 validation_findings 修改对应字段。")
    if diagnostic and diagnostic.line_number:
        constraints.append(f"检查 analysis.py 第 {diagnostic.line_number} 行；系统不推测替换代码。")
    if remaining == 0:
        constraints.append("停止当前 Python 交付物，说明未完成部分。")
    feedback = PythonFailureFeedback(
        error_code=code,
        retryable=remaining > 0,
        attempt=attempt,
        remaining_attempts=remaining,
        diagnostic_facts=diagnostic,
        violations=violations,
        repair_constraints=constraints,
        outputs_created=list(summary.get("outputs_created", []))[:20],
        paths_rejected=list(summary.get("paths_rejected", []))[:20],
    )
    summary.update(
        reason_code=code,
        retryable=feedback.retryable,
        repair_exhausted=exhausted,
        python_failure=feedback.model_dump(mode="json"),
    )
    return observation.model_copy(update={"summary": summary})
