import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./blog.db")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# check_same_thread=False is required for SQLite when the same connection
# may be accessed by different threads, which happens under FastAPI's
# threaded dev server (each request can be handled on a different thread).
connect_args = {"check_same_thread": False} if IS_SQLITE else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

if IS_SQLITE:
    # SQLite ignores foreign key constraints (and ON DELETE CASCADE) unless
    # this pragma is set on every connection, so model-level cascades need it.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_post_image_column() -> None:
    """
    Base.metadata.create_all only creates tables that don't exist yet -- it
    never alters an existing one. Databases created before the image column
    was added need it backfilled by hand, without touching any existing
    rows (they end up with image = NULL, same as any other nullable column).
    """
    inspector = inspect(engine)
    if "posts" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("posts")}
    if "image" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE posts ADD COLUMN image VARCHAR(500)"))
