"""一次性清理当前本地 Metadata 里的全部 Session。

只打 METADATA_DATABASE_URL 默认指向的 storage/metadata.db，不碰 pytest 临时库。
删除走 SessionService.delete（含 Artifact 文件与 Session 工作区）。不可恢复。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "packages"))

from application.artifacts import ArtifactStore  # noqa: E402
from application.script_workspace import SessionWorkspaceManager  # noqa: E402
from application.sessions import SessionService  # noqa: E402
from metadata.database import create_session_factory, create_sqlite_engine  # noqa: E402
from metadata.repositories import (  # noqa: E402
    ArtifactRepository,
    DataSourceRepository,
    SessionRepository,
)
from server.config import Settings  # noqa: E402
from sqlalchemy import event  # noqa: E402


def _assert_local_metadata(url: str) -> None:
    normalized = url.replace("\\", "/").lower()
    if "pytest" in normalized or "/tmp/" in normalized or "pytest-of" in normalized:
        raise SystemExit(f"拒绝清理测试库: {url}")
    if "storage/metadata.db" not in normalized:
        raise SystemExit(f"拒绝非本地 Metadata 路径: {url}")


async def _wipe(*, confirm: bool) -> None:
    settings = Settings()
    _assert_local_metadata(settings.metadata_database_url)
    engine = create_sqlite_engine(settings.metadata_database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _busy_timeout(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()

    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as db:
            _items, before = await SessionRepository(db).list(offset=0, limit=1)
        print(f"database={settings.metadata_database_url}")
        print(f"sessions_before={before}")
        if not confirm:
            print("dry-run: pass --confirm to delete")
            return
        deleted = 0
        while True:
            async with session_factory() as db:
                items, remaining = await SessionRepository(db).list(offset=0, limit=50)
                ids = [item.id for item in items]
            if not ids:
                print(f"sessions_after={remaining}")
                print(f"deleted={deleted}")
                return
            for session_id in ids:
                async with session_factory() as db:
                    service = SessionService(
                        SessionRepository(db),
                        DataSourceRepository(db),
                        artifact_store=ArtifactStore(
                            ArtifactRepository(db),
                            settings.artifact_root,
                        ),
                        session_workspace=SessionWorkspaceManager(
                            settings.session_workspace_root
                        ),
                    )
                    await service.delete(session_id)
                    await db.commit()
                deleted += 1
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Wipe all Sessions in local Metadata.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually delete; default is dry-run",
    )
    args = parser.parse_args()
    asyncio.run(_wipe(confirm=args.confirm))


if __name__ == "__main__":
    main()
