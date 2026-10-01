from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from reviewer.config import get_settings
from reviewer.db.models import Base

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def test_migration_creates_every_model_table():
    """The hand-written migration must stay in sync with the models."""
    command.upgrade(Config(str(ALEMBIC_INI)), "head")

    engine = create_engine(get_settings().database_url.get_secret_value())
    inspector = inspect(engine)
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())
    for name, table in Base.metadata.tables.items():
        assert {c["name"] for c in inspector.get_columns(name)} == set(table.columns.keys())
    engine.dispose()
