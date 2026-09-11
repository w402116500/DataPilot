from __future__ import annotations

from pathlib import Path, PurePosixPath

from contracts.datalink import DataLinkErrorCode, validate_relative_ref
from contracts.status import DataSourceType

from server.connector.base import ConnectorError

_ALLOWED_SUFFIXES: dict[DataSourceType, frozenset[str]] = {
    DataSourceType.CSV: frozenset({".csv"}),
    DataSourceType.SQLITE: frozenset({".sqlite", ".sqlite3", ".db"}),
}


def resolve_source_path(source_root: Path, source_ref: str, source_type: DataSourceType) -> Path:
    """把相对引用解析为根目录内的普通文件，并拒绝所有符号链接。"""

    try:
        normalized_ref = validate_relative_ref(source_ref)
    except ValueError as exc:
        raise ConnectorError(
            DataLinkErrorCode.PATH_OUTSIDE_ROOT, "Source reference is invalid"
        ) from exc

    root = source_root.resolve()
    if not root.is_dir():
        raise ConnectorError(DataLinkErrorCode.PATH_OUTSIDE_ROOT, "Source root is unavailable")
    candidate = root.joinpath(*PurePosixPath(normalized_ref).parts)
    _reject_symbolic_links(candidate, root)
    try:
        target = candidate.resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ConnectorError(
            DataLinkErrorCode.PATH_OUTSIDE_ROOT, "Source reference is invalid"
        ) from exc
    if not target.is_file():
        raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "Source file is unavailable")
    if target.suffix.lower() not in _ALLOWED_SUFFIXES[source_type]:
        raise ConnectorError(DataLinkErrorCode.BUILD_FAILED, "Source type does not match the file")
    return target


def _reject_symbolic_links(candidate: Path, root: Path) -> None:
    """逐层检查相对引用，避免中间目录的链接绕过受控根目录。"""

    current = candidate
    while current != root:
        if current.is_symlink():
            raise ConnectorError(DataLinkErrorCode.PATH_OUTSIDE_ROOT, "Source reference is invalid")
        current = current.parent
