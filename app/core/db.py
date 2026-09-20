import os
import sqlite3
import time

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings


def _make_engine():
    url = settings.db_url
    if url.startswith("sqlite+aiosqlite:///"):
        # Для SQLite: либо StaticPool (один коннект), либо NullPool
        if settings.sqla_staticpool:
            return create_async_engine(
                url,
                future=True,
                pool_pre_ping=True,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        if settings.sqla_nullpool:
            return create_async_engine(
                url,
                future=True,
                pool_pre_ping=True,
                poolclass=NullPool,
            )
        # По умолчанию для sqlite — NullPool
        return create_async_engine(
            url,
            future=True,
            pool_pre_ping=True,
            poolclass=NullPool,
        )
    if settings.sqla_nullpool:
        # Принудительно без пула для любых СУБД
        return create_async_engine(
            url,
            future=True,
            pool_pre_ping=True,
            poolclass=NullPool,
        )
    # Для прочих СУБД используем пул
    return create_async_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=60,
    )


engine = _make_engine()
AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


def _sqlite_file_path(db_url: str) -> str | None:
    # expect sqlite+aiosqlite:///./data/bot.db
    if not db_url.startswith("sqlite+aiosqlite:///"):
        return None
    path = db_url.removeprefix("sqlite+aiosqlite:///")
    return path


def prepare_db_storage_sync() -> None:
    """Create only filesystem prerequisites; never mutate database schema."""
    path = _sqlite_file_path(settings.db_url)
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _is_legacy_schema(sqlite_path: str) -> bool:
    if not os.path.exists(sqlite_path):
        return False
    try:
        with sqlite3.connect(sqlite_path) as conn:
            cur = conn.cursor()
            # check channels table
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='channels'"
            )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("PRAGMA table_info(channels)")
            cols = [r[1] for r in cur.fetchall()]
            # legacy table usually had no 'id' pk column
            return "id" not in cols
    except Exception:
        return False


def init_db_if_needed_sync() -> None:
    """Legacy pre-Alembic repair helper; never call from application startup.

    Kept only for explicit/manual recovery of historical SQLite layouts. The
    supported runtime startup contract is owned by app.core.schema and Alembic.
    """

    path = _sqlite_file_path(settings.db_url)
    if not path:
        return
    prepare_db_storage_sync()
    if _is_legacy_schema(path):
        backup = f"{path}.backup.{int(time.time())}.db"
        os.replace(path, backup)
    # ensure new columns for existing sqlite schemas (simple migrations)
    try:
        import sqlite3 as _sl

        with _sl.connect(path) as _conn:
            _cur = _conn.cursor()

            def _has_col(table: str, col: str) -> bool:
                _cur.execute(f"PRAGMA table_info({table})")
                cols = [r[1] for r in _cur.fetchall()]
                return col in cols

            def _add_col(table: str, col: str, decl: str) -> None:
                try:
                    _cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    _conn.commit()
                except Exception:
                    pass

            # join_requests new columns
            if _has_col("join_requests", "id"):
                if not _has_col("join_requests", "challenge_type"):
                    _add_col("join_requests", "challenge_type", "TEXT")
                if not _has_col("join_requests", "challenge_payload"):
                    _add_col("join_requests", "challenge_payload", "TEXT")
                if not _has_col("join_requests", "expires_at"):
                    _add_col("join_requests", "expires_at", "TIMESTAMP")
                if not _has_col("join_requests", "attempts_left"):
                    _add_col("join_requests", "attempts_left", "INTEGER DEFAULT 3")
                if not _has_col("join_requests", "invite_link"):
                    _add_col("join_requests", "invite_link", "VARCHAR(512)")
            # subscribers tags
            if _has_col("subscribers", "id"):
                if not _has_col("subscribers", "tags"):
                    _add_col("subscribers", "tags", "TEXT")
            # clients ui settings
            if _has_col("clients", "id"):
                if not _has_col("clients", "ui_settings"):
                    _add_col("clients", "ui_settings", "JSON")
                if not _has_col("clients", "last_channel_id"):
                    _add_col("clients", "last_channel_id", "INTEGER")
    except Exception:
        pass
