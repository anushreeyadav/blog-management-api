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


def ensure_post_view_count_column() -> None:
    """
    Same situation as ensure_post_image_column above, for the view_count
    column added later (see app/models.py's Post.view_count): a database
    created before that column existed needs it backfilled by hand.
    Existing rows get view_count = 0, same as any newly created post.
    """
    inspector = inspect(engine)
    if "posts" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("posts")}
    if "view_count" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE posts ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0"))


def ensure_dashboard_indexes() -> None:
    """
    Same situation as ensure_post_image_column above: a database created
    before the dashboard's performance indexes were added (see
    app/models.py's Post.author_id / Comment.post_id / Comment.user_id)
    never gets them from Base.metadata.create_all alone. CREATE INDEX IF
    NOT EXISTS makes this safe to call on every startup, on a database
    that already has them or not.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if "posts" not in existing_tables or "comments" not in existing_tables:
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_posts_author_id ON posts (author_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_comments_post_id ON comments (post_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_comments_user_id ON comments (user_id)"))


def ensure_user_auth0_sub_column() -> None:
    """
    Same situation as ensure_post_image_column above, for users.auth0_sub
    (see app/models.py's User.auth0_sub and app/auth0.py). Existing users
    get auth0_sub = NULL, i.e. they stay plain username/password users.
    The unique index is created separately because SQLite can't add a
    UNIQUE column via ALTER TABLE.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "auth0_sub" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN auth0_sub VARCHAR(255)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_auth0_sub ON users (auth0_sub)"))
