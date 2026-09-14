from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from ticketmind.core.config import get_settings
engine: Engine = create_engine(get_settings().database_url.unicode_string(),pool_pre_ping=True)