"""Async SQLAlchemy engine and session factory.

Usage in route handlers or services::

    from app.db import async_session

    async with async_session() as session:
        ...
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,  # recycle connections older than 5 minutes, proactively
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
