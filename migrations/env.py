from alembic import context
from sqlalchemy import engine_from_config, pool
from aifanyi.config import get_settings
from aifanyi.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

# LangGraph owns these tables and their migration sequence, even when sharing the database.
CHECKPOINT_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}


def include_name(name, type_, parent_names):
    return not (type_ == "table" and name in CHECKPOINT_TABLES)


if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=Base.metadata,
        literal_binds=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, compare_type=True, include_name=include_name
        )
        with context.begin_transaction():
            context.run_migrations()
