from datetime import datetime, timezone
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import ROOT, settings


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


settings.storage_dir.mkdir(parents=True, exist_ok=True)
settings.documents_dir.mkdir(parents=True, exist_ok=True)
connect_args = {'check_same_thread': False, 'timeout': 30} if settings.database_url.startswith('sqlite') else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
if settings.database_url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def sqlite_setup(connection, _):
        cursor = connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        yield session


def init_db():
    from . import models  # noqa: F401
    from . import workflows  # noqa: F401
    from . import mail_models  # noqa: F401
    from alembic import command
    from alembic.config import Config
    tables = set(inspect(engine).get_table_names())
    if tables - {'alembic_version'} and 'alembic_version' not in tables:
        raise RuntimeError('База содержит таблицы без истории миграций. Используйте отдельную новую базу; автоматическое присвоение версии запрещено.')
    command.upgrade(Config(str(ROOT / 'alembic.ini')), 'head')
