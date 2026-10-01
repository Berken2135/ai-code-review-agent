"""Engine and session handling. Engine and factory are created lazily (and cached)."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from reviewer.config import get_settings


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url.get_secret_value()
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    else:  # fail fast if the DB is unreachable, so the startup sweep cannot hang the app
        connect_args = {"connect_timeout": 5}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency for read-only endpoints."""
    with get_session_factory()() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on success, roll back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
