"""
STEP 15 -- SQLite database structure and integrity verification.

These tests inspect the actual SQLite schema (via PRAGMA / sqlite_master),
not just the SQLAlchemy model definitions, and exercise real inserts to
confirm constraints are enforced by the database itself. They run against
an isolated in-memory SQLite database (never the project's real blog.db).
"""

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from app import models
from app.auth import hash_password, verify_password
from app.database import Base
from app.services import subscription as subscription_service

REQUIRED_TABLES = {"users", "posts", "comments", "likes"}

REQUIRED_COLUMNS = {
    "users": {"id", "username", "email", "password_hash"},
    "posts": {"id", "title", "content", "author_id", "created_at"},
    "comments": {"id", "post_id", "user_id", "text", "created_at"},
    "likes": {"id", "post_id", "user_id"},
}


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    session = sessionmaker(bind=db_engine)()
    yield session
    session.close()


def _raw_cursor(db_engine):
    return db_engine.raw_connection().cursor()


def _new_user(db_session, **overrides) -> models.User:
    """
    User.subscription_plan_id is required (every user has an active plan --
    see app/services/subscription.py), so tests that build a User directly
    go through this helper rather than repeating the plan lookup everywhere.
    """
    defaults = dict(password_hash=hash_password("Password123"))
    defaults.update(overrides)
    defaults.setdefault("subscription_plan_id", subscription_service.get_or_create_basic_plan(db_session).id)
    return models.User(**defaults)


# ---------------------------------------------------------------------------
# Required tables
# ---------------------------------------------------------------------------


class TestRequiredTablesExist:
    def test_users_posts_comments_likes_tables_exist(self, db_engine):
        cur = _raw_cursor(db_engine)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        assert REQUIRED_TABLES.issubset(tables)


# ---------------------------------------------------------------------------
# Required columns
# ---------------------------------------------------------------------------


class TestRequiredColumnsExist:
    @pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
    def test_table_has_required_columns(self, db_engine, table):
        cur = _raw_cursor(db_engine)
        cur.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cur.fetchall()}
        assert REQUIRED_COLUMNS[table].issubset(columns)


# ---------------------------------------------------------------------------
# Primary keys
# ---------------------------------------------------------------------------


class TestPrimaryKeys:
    @pytest.mark.parametrize("table", sorted(REQUIRED_COLUMNS))
    def test_id_is_the_primary_key(self, db_engine, table):
        cur = _raw_cursor(db_engine)
        cur.execute(f"PRAGMA table_info({table})")
        pk_columns = {row[1] for row in cur.fetchall() if row[5] == 1}
        assert pk_columns == {"id"}


# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------


class TestForeignKeys:
    def test_posts_author_id_references_users(self, db_engine):
        cur = _raw_cursor(db_engine)
        cur.execute("PRAGMA foreign_key_list(posts)")
        fks = {(r[3], r[2], r[4]) for r in cur.fetchall()}
        assert ("author_id", "users", "id") in fks

    def test_comments_post_id_and_user_id_reference_correctly(self, db_engine):
        cur = _raw_cursor(db_engine)
        cur.execute("PRAGMA foreign_key_list(comments)")
        fks = {(r[3], r[2], r[4]) for r in cur.fetchall()}
        assert ("post_id", "posts", "id") in fks
        assert ("user_id", "users", "id") in fks

    def test_likes_post_id_and_user_id_reference_correctly(self, db_engine):
        cur = _raw_cursor(db_engine)
        cur.execute("PRAGMA foreign_key_list(likes)")
        fks = {(r[3], r[2], r[4]) for r in cur.fetchall()}
        assert ("post_id", "posts", "id") in fks
        assert ("user_id", "users", "id") in fks


# ---------------------------------------------------------------------------
# Like unique constraint -- both structural and enforced-at-insert-time
# ---------------------------------------------------------------------------


class TestLikeUniqueConstraint:
    def test_unique_index_exists_on_post_id_user_id(self, db_engine):
        cur = _raw_cursor(db_engine)
        cur.execute("PRAGMA index_list(likes)")
        found = False
        for row in cur.fetchall():
            name, is_unique = row[1], row[2]
            if is_unique:
                cur.execute(f"PRAGMA index_info({name})")
                cols = {r[2] for r in cur.fetchall()}
                if cols == {"post_id", "user_id"}:
                    found = True
        assert found

    def test_duplicate_like_rejected_at_database_level(self, db_session):
        user = _new_user(db_session, username="dbtest", email="dbtest@example.com")
        db_session.add(user)
        db_session.commit()
        post = models.Post(title="t", content="c", author_id=user.id)
        db_session.add(post)
        db_session.commit()

        db_session.add(models.Like(post_id=post.id, user_id=user.id))
        db_session.commit()

        # Insert a second identical (post_id, user_id) row directly,
        # bypassing any application-level duplicate check entirely.
        db_session.add(models.Like(post_id=post.id, user_id=user.id))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        count = db_session.query(models.Like).filter_by(post_id=post.id, user_id=user.id).count()
        assert count == 1


# ---------------------------------------------------------------------------
# Data persistence and relationships
# ---------------------------------------------------------------------------


class TestDataPersistenceAndRelationships:
    def test_user_post_comment_like_chain_persists_with_correct_ids(self, db_session):
        user_a = _new_user(db_session, username="chain_a", email="chain_a@example.com")
        user_b = _new_user(db_session, username="chain_b", email="chain_b@example.com")
        db_session.add_all([user_a, user_b])
        db_session.commit()

        post = models.Post(title="Chain Post", content="Body", author_id=user_a.id)
        db_session.add(post)
        db_session.commit()

        comment = models.Comment(post_id=post.id, user_id=user_b.id, text="Nice post!")
        like = models.Like(post_id=post.id, user_id=user_b.id)
        db_session.add_all([comment, like])
        db_session.commit()

        # User -> Post
        assert post.author_id == user_a.id
        # Post -> Comment
        assert comment.post_id == post.id
        # User -> Comment
        assert comment.user_id == user_b.id
        # Post -> Like
        assert like.post_id == post.id
        # User -> Like
        assert like.user_id == user_b.id
        assert comment.created_at is not None
        assert post.created_at is not None

    def test_password_is_hashed_not_stored_as_plaintext(self, db_session):
        plaintext = "Password123"
        user = _new_user(
            db_session, username="hashcheck", email="hashcheck@example.com", password_hash=hash_password(plaintext)
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        assert user.password_hash != plaintext
        assert verify_password(plaintext, user.password_hash) is True


# ---------------------------------------------------------------------------
# Orphan record checks
# ---------------------------------------------------------------------------


class TestNoOrphanRecords:
    def test_no_orphans_after_normal_usage(self, db_session, db_engine):
        user = _new_user(db_session, username="orphan_check", email="orphan_check@example.com")
        db_session.add(user)
        db_session.commit()
        post = models.Post(title="t", content="c", author_id=user.id)
        db_session.add(post)
        db_session.commit()
        db_session.add_all(
            [
                models.Comment(post_id=post.id, user_id=user.id, text="hi"),
                models.Like(post_id=post.id, user_id=user.id),
            ]
        )
        db_session.commit()

        cur = _raw_cursor(db_engine)
        cur.execute("SELECT p.id FROM posts p LEFT JOIN users u ON p.author_id = u.id WHERE u.id IS NULL")
        assert cur.fetchall() == []
        cur.execute("SELECT c.id FROM comments c LEFT JOIN posts p ON c.post_id = p.id WHERE p.id IS NULL")
        assert cur.fetchall() == []
        cur.execute("SELECT c.id FROM comments c LEFT JOIN users u ON c.user_id = u.id WHERE u.id IS NULL")
        assert cur.fetchall() == []
        cur.execute("SELECT l.id FROM likes l LEFT JOIN posts p ON l.post_id = p.id WHERE p.id IS NULL")
        assert cur.fetchall() == []
        cur.execute("SELECT l.id FROM likes l LEFT JOIN users u ON l.user_id = u.id WHERE u.id IS NULL")
        assert cur.fetchall() == []

    def test_no_duplicate_likes_group_by_check(self, db_session, db_engine):
        user = _new_user(db_session, username="group_check", email="group_check@example.com")
        db_session.add(user)
        db_session.commit()
        post = models.Post(title="t", content="c", author_id=user.id)
        db_session.add(post)
        db_session.commit()
        db_session.add(models.Like(post_id=post.id, user_id=user.id))
        db_session.commit()

        cur = _raw_cursor(db_engine)
        cur.execute(
            "SELECT post_id, user_id, COUNT(*) FROM likes GROUP BY post_id, user_id HAVING COUNT(*) > 1"
        )
        assert cur.fetchall() == []
