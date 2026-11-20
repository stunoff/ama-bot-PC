from alembic import context

from ama_rs.db import create_database_engine


def run_migrations() -> None:
    engine = create_database_engine()
    if context.is_offline_mode():
        context.configure(dialect_name=engine.dialect.name, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
