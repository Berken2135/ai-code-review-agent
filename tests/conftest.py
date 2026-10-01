import socket

import pytest

from reviewer.config import get_settings
from reviewer.db.models import Base
from reviewer.db.session import get_engine, get_session_factory
from tests.helpers import RUNS_TOKEN


def _reset_caches() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


# Fixture files (e.g. a sample repo's test_service.py) are data, not tests to collect.
collect_ignore = ["fixtures"]

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _block_external_network(request, monkeypatch):
    """No test may reach a non-loopback host (real LLM/GitHub calls). Integration tests are exempt.

    Loopback stays open: asyncio on Windows builds its self-pipe with a loopback connect.
    """
    if request.node.get_closest_marker("integration"):
        return
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(real):
        def guarded(self, address):
            host = address[0] if isinstance(address, tuple) else None  # None: unix socket
            if host is not None and host not in _LOOPBACK:
                raise RuntimeError(f"network access blocked in tests: {host}")
            return real(self, address)

        return guarded

    monkeypatch.setattr(socket.socket, "connect", guard(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(real_connect_ex))


@pytest.fixture(autouse=True)
def _test_env(monkeypatch, tmp_path):
    """Isolated settings for every test: SQLite DB, no ambient secrets, no .env file."""
    monkeypatch.chdir(tmp_path)  # keeps a developer's local .env out of the tests
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("RUNS_API_TOKEN", RUNS_TOKEN)
    for name in ("GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    _reset_caches()
    yield
    _reset_caches()


@pytest.fixture
def db_tables():
    """Create the schema on the per-test SQLite database."""
    Base.metadata.create_all(get_engine())


@pytest.fixture
def switch_database(monkeypatch):
    """Point the app at another DATABASE_URL from inside a test (settings/engine are cached)."""

    def switch(url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        _reset_caches()

    return switch
