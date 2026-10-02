"""Deterministic token estimates shared by Graph working-set fit and the model adapter."""

from __future__ import annotations

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage


def estimate_text_tokens(value: str) -> float:
    """Treat non-ASCII text conservatively because CJK tokenizers vary widely."""

    ascii_count = sum(char.isascii() for char in value)
    non_ascii_count = len(value) - ascii_count
    return ascii_count / 4 + non_ascii_count


def estimate_message_tokens(messages: Sequence[BaseMessage]) -> int:
    """Use a conservative deterministic estimate when no provider tokenizer exists."""

    total_tokens = 0.0
    for message in messages:
        total_tokens += estimate_text_tokens(str(message.content))
        if isinstance(message, AIMessage) and message.tool_calls:
            total_tokens += estimate_text_tokens(
                json.dumps(
                    message.tool_calls,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
            )
    return max(1, round(total_tokens))
