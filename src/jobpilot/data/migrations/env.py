from alembic import context
from sqlalchemy import create_engine

from jobpilot.data import tables  # noqa: F401  (registers models on Base.metadata)
from jobpilot.data.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_path" in x_args:
        return f"sqlite:///{x_args['db_path']}"
    return config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
