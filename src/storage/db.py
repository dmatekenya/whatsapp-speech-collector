from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings

# echo stays off everywhere: SQL logs would contain phone numbers.


# ============================================================
# Async (API)
# ============================================================
@lru_cache
def get_async_engine() -> AsyncEngine:
    return create_async_engine(get_settings().async_database_url, echo=False, pool_pre_ping=True)


@lru_cache
def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request."""
    async with get_async_sessionmaker()() as session:
        yield session


# ============================================================
# Sync (worker and scripts)
# ============================================================
@lru_cache
def get_sync_engine() -> Engine:
    return create_engine(get_settings().sync_database_url, echo=False, pool_pre_ping=True)


@lru_cache
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)
