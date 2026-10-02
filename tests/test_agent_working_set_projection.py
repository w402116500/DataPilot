from __future__ import annotations

import json

from agent_runtime.graph import _messages_for_model
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def _pair(call_id: str, script: str, *, status: str = "failed") -> list[BaseMessage]:
    return [
        AIMessage(
            content="旧的模型分析说明",
            tool_calls=[
                {
                    "id": call_id,
                    "name": "run_python",
                    "args": {"script": script, "output_paths": ["charts/regions.png"]},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            tool_call_id=call_id,
            name="run_python",
            content=json.dumps(
                {
                    "status": status,
                    "summary": {
                        "output_count": 1 if status == "succeeded" else 0,
                        "python_failure": {
                            "retryable": True,
                            "remaining_attempts": 1,
                            "diagnostic_facts": {"exception_type": "KeyError", "line_number": 18},
                            "repair_constraints": ["修改后重试"],
                        },
                        "stderr": "raw traceback must not reach model",
                    },
                }
            ),
        ),
    ]


def test_working_set_keeps_only_latest_failed_script_and_safe_diagnostics() -> None:
    messages = [
        SystemMessage(content="rules"),
        HumanMessage(content="question"),
        *_pair("old", "obsolete script"),
        *_pair("latest", "current script"),
    ]
    working = _messages_for_model(messages)

    calls = [item for item in working if isinstance(item, AIMessage)]
    assert len(calls) == 1
    assert calls[0].tool_calls[0]["args"]["script"] == "current script"
    tool = next(item for item in working if isinstance(item, ToolMessage))
    payload = json.loads(str(tool.content))
    assert tool.tool_call_id == "latest"
    assert payload["summary"]["python_failure"]["remaining_attempts"] == 1
    assert payload["summary"]["python_failure"]["diagnostic_facts"]["line_number"] == 18
    assert "raw traceback" not in str(working)
    assert messages[2].tool_calls[0]["args"]["script"] == "obsolete script"


def test_success_retires_previous_failure_and_executed_code() -> None:
    messages = [
        SystemMessage(content="rules"),
        HumanMessage(content="question"),
        *_pair("old", "obsolete script"),
        *_pair("success", "successful script", status="succeeded"),
    ]
    working = _messages_for_model(messages)

    calls = [item for item in working if isinstance(item, AIMessage)]
    assert len(calls) == 1
    assert calls[0].tool_calls[0]["id"] == "success"
    assert "script" not in calls[0].tool_calls[0]["args"]
    assert calls[0].content == ""
    tool = next(item for item in working if isinstance(item, ToolMessage))
    assert json.loads(str(tool.content))["summary"]["output_count"] == 1
    assert messages[-2].tool_calls[0]["args"]["script"] == "successful script"


def test_failed_result_validation_keeps_sql_for_repair() -> None:
    messages = [
        SystemMessage(content="rules"),
        HumanMessage(content="question"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "sql",
                    "name": "run_sql_readonly",
                    "args": {"sql": "SELECT wrong_value FROM facts"},
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            tool_call_id="sql",
            content=json.dumps(
                {
                    "status": "succeeded",
                    "summary": {"validation_status": "failed", "verified_values": []},
                }
            ),
        ),
    ]
    working = _messages_for_model(messages)
    assert working[2].tool_calls[0]["args"]["sql"] == "SELECT wrong_value FROM facts"
