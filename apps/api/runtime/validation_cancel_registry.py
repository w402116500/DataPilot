"""In-process cancel handles for DataLink relation validations."""

from __future__ import annotations

from runtime.run_cancel_registry import RunCancellation


class ValidationCancelRegistry:
    """Process-local validation cancel signals; restart recovery does not restore them."""

    def __init__(self) -> None:
        self._signals: dict[str, RunCancellation] = {}

    def register(
        self, validation_id: str, cancellation: RunCancellation | None = None
    ) -> RunCancellation:
        signal = cancellation or RunCancellation()
        existing = self._signals.get(validation_id)
        if existing is not None:
            return existing
        self._signals[validation_id] = signal
        return signal

    def cancel(self, validation_id: str) -> bool:
        signal = self._signals.get(validation_id)
        if signal is None:
            return False
        signal.cancel()
        return True

    def unregister(self, validation_id: str) -> None:
        self._signals.pop(validation_id, None)
