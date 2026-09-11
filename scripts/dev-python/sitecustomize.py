"""Keep local DataPilot startup independent from an unhealthy Windows WMI service."""

from __future__ import annotations

import platform
import sys


def _skip_windows_wmi(*_args: object) -> None:
    raise OSError("WMI is disabled for the local DataPilot development service")


if sys.platform == "win32":
    platform._wmi_query = _skip_windows_wmi
