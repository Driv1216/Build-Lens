from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from buildlens.core.config import Settings


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    return create_session_factory_from_engine(engine)


def create_session_factory_from_engine(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def create_database_engine(settings: Settings) -> Engine:
    """Expose engine creation for operational health checks such as the CLI doctor."""
    return create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
