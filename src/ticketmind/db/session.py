from collections.abc import Generator
from sqlalchemy.orm import sessionmaker, Session
from ticketmind.db.engine import engine
SesstionLocal = sessionmaker(bind=engine,autoflush=False,expire_on_commit=False)
def get_db_session() -> Generator[Session,None,None]:
    """Return a database session."""
    session: Session = SesstionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()