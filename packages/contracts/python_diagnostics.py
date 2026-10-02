"""Bounded Python failure facts shared by the sandbox and runtime."""

from pydantic import BaseModel, ConfigDict, Field


class PythonDiagnostic(BaseModel):
    """Safe exception projection; raw process output never belongs here."""

    model_config = ConfigDict(extra="forbid")

    exception_type: str | None = Field(default=None, max_length=80)
    message: str = Field(max_length=500)
    line_number: int | None = Field(default=None, ge=1)
    code_line: str | None = Field(default=None, max_length=300)
    traceback_tail: list[str] = Field(default_factory=list, max_length=5)
    exit_code: int | None = None
    message_redacted: bool = True


class PythonFailureFeedback(BaseModel):
    """One repair contract for validation, process and output failures."""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(max_length=100)
    retryable: bool
    attempt: int = Field(ge=1)
    max_attempts: int = Field(default=3, ge=1)
    remaining_attempts: int = Field(ge=0, le=2)
    diagnostic_facts: PythonDiagnostic | None = None
    violations: list[str] = Field(default_factory=list, max_length=12)
    repair_constraints: list[str] = Field(default_factory=list, max_length=8)
    outputs_created: list[str] = Field(default_factory=list, max_length=20)
    paths_rejected: list[str] = Field(default_factory=list, max_length=20)
