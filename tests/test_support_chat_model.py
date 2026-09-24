"""
Model-level tests for SupportChatMessage (the AI Support Chat activity log)
and its relationship to the existing User model.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base
from app.services.subscription import get_or_create_basic_plan


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Same pragma app/database.py sets, so ON DELETE CASCADE behaves as in the app.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(db, username="alice") -> models.User:
    user = models.User(
        username=username,
        email=f"{username}@example.com",
        password_hash="not-a-real-hash",
        subscription_plan_id=get_or_create_basic_plan(db).id,
    )
    db.add(user)
    db.commit()
    return user


def _add_message(db, user, question="How do I create a post?") -> models.SupportChatMessage:
    message = models.SupportChatMessage(
        user_id=user.id, question=question, response="To create a post, ...", response_source="predefined"
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def test_created_at_is_set_automatically(db):
    message = _add_message(db, _make_user(db))
    assert isinstance(message.created_at, datetime)


def test_relationship_works_in_both_directions(db):
    alice = _make_user(db, "alice")
    bob = _make_user(db, "bob")
    first = _add_message(db, alice, "First")
    second = _add_message(db, alice, "Second")
    _add_message(db, bob, "Bob's")

    db.refresh(alice)
    assert first.user is alice
    assert {m.id for m in alice.support_chat_messages} == {first.id, second.id}
    assert [m.question for m in bob.support_chat_messages] == ["Bob's"]


def test_deleting_a_user_deletes_only_their_chat_history(db):
    alice = _make_user(db, "alice")
    bob = _make_user(db, "bob")
    _add_message(db, alice)
    _add_message(db, bob)

    db.delete(alice)
    db.commit()

    remaining = db.query(models.SupportChatMessage).all()
    assert [m.user_id for m in remaining] == [bob.id]


def test_user_id_is_required(db):
    db.add(models.SupportChatMessage(question="q", response="r", response_source="predefined"))
    with pytest.raises(Exception):
        db.commit()


def test_table_has_user_id_indexes(db):
    indexes = inspect(db.get_bind()).get_indexes("support_chat_messages")
    indexed_columns = {tuple(index["column_names"]) for index in indexes}
    assert ("user_id",) in indexed_columns
    assert ("user_id", "created_at") in indexed_columns


def test_table_stores_no_authentication_data(db):
    columns = {column["name"] for column in inspect(db.get_bind()).get_columns("support_chat_messages")}
    assert columns == {"id", "user_id", "question", "response", "response_source", "created_at"}


def test_existing_user_relationships_are_unchanged():
    relationships = set(inspect(models.User).relationships.keys())
    assert {
        "posts",
        "comments",
        "likes",
        "subscriptions",
        "billing_history",
        "subscription_plan",
        "notifications",
    } <= relationships
    assert relationships - {
        "posts",
        "comments",
        "likes",
        "subscriptions",
        "billing_history",
        "subscription_plan",
        "notifications",
    } == {"support_chat_messages"}
