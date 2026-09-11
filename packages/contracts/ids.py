from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def make_id(prefix: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}_{timestamp}_{uuid4().hex[:12]}"
