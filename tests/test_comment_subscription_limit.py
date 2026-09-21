"""
Sub-Task 10 -- comment subscription limits, exercised through the real
POST/GET /posts/{post_id}/comments endpoints with the real seeded Basic /
Premium / Pro plans: authenticate -> resolve active plan -> count current
comment usage -> compare against max_comments -> allow or reject with the
standard message, before either the Comment row or its notification side
effect exist.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

LIMIT_MESSAGE = "You’ve reached your plan limit. Kindly upgrade your plan to continue."


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    main_app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_app) as test_client:
        yield test_client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register(client: TestClient, user: dict) -> dict:
    client.post("/auth/register", json=user)
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()["plans"]
    return next(p["id"] for p in plans if p["slug"] == slug)


def _create_post(client: TestClient, headers: dict, title: str = "target") -> int:
    return client.post("/posts", json={"title": title, "content": "body"}, headers=headers).json()["id"]


class TestCommentWithinLimit:
    def test_comment_well_under_the_limit_succeeds(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)  # Basic: max_comments = 5
        headers_b = _register(client, USER_B)
        post_id = _create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(f"/posts/{post_id}/comments", json={"text": "nice post"}, headers=headers_b)

        assert resp.status_code == 201
        mock_notify.assert_called_once()


class TestCommentAtLimit:
    def test_the_last_available_comment_still_succeeds(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)  # Basic: max_comments = 5
        post_id = _create_post(client, headers_a)

        for i in range(4):
            assert client.post(f"/posts/{post_id}/comments", json={"text": f"c{i}"}, headers=headers_b).status_code == 201

        resp = client.post(f"/posts/{post_id}/comments", json={"text": "the 5th"}, headers=headers_b)
        assert resp.status_code == 201


class TestCommentExceedingLimit:
    def test_the_next_comment_past_the_limit_is_rejected(self, client):
        client, session_factory = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)  # Basic: max_comments = 5
        post_id = _create_post(client, headers_a)

        for i in range(5):
            assert client.post(f"/posts/{post_id}/comments", json={"text": f"c{i}"}, headers=headers_b).status_code == 201

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(f"/posts/{post_id}/comments", json={"text": "one too many"}, headers=headers_b)

        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE

        # The check ran before either side effect: no notification fired,
        # and no Comment row was created for the rejected attempt.
        mock_notify.assert_not_called()
        db = session_factory()
        try:
            comments = db.query(models.Comment).filter(models.Comment.post_id == post_id).all()
            assert len(comments) == 5
            assert all(c.text != "one too many" for c in comments)
        finally:
            db.close()


class TestPremiumUserCommentLimit:
    """Basic and Pro's boundaries are covered above/below; Premium has its
    own distinct max_comments (25, not Basic's 5) that must be enforced at
    its own boundary too, not just reported correctly by GET
    /subscriptions/usage (see test_subscription_usage.py)."""

    def test_premium_user_can_post_up_to_twenty_five_comments(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        plan_id = _plan_id(client, "premium")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers_b).status_code == 201
        post_id = _create_post(client, headers_a)

        for i in range(25):
            resp = client.post(f"/posts/{post_id}/comments", json={"text": f"comment {i}"}, headers=headers_b)
            assert resp.status_code == 201

    def test_premium_users_twenty_sixth_comment_is_rejected(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        plan_id = _plan_id(client, "premium")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers_b).status_code == 201
        post_id = _create_post(client, headers_a)

        for i in range(25):
            assert client.post(f"/posts/{post_id}/comments", json={"text": f"comment {i}"}, headers=headers_b).status_code == 201

        resp = client.post(f"/posts/{post_id}/comments", json={"text": "one too many"}, headers=headers_b)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE


class TestProUnlimitedComments:
    def test_pro_user_can_comment_far_beyond_basics_cap(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        plan_id = _plan_id(client, "pro")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers_b).status_code == 201
        post_id = _create_post(client, headers_a)

        for i in range(10):
            resp = client.post(f"/posts/{post_id}/comments", json={"text": f"comment {i}"}, headers=headers_b)
            assert resp.status_code == 201


class TestExistingCommentRetrieval:
    """GET /posts/{post_id}/comments is public and unaffected by the
    commenter's limit -- it must keep working exactly as before."""

    def test_comments_are_still_publicly_listable_in_order(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        post_id = _create_post(client, headers_a)
        client.post(f"/posts/{post_id}/comments", json={"text": "first"}, headers=headers_b)
        client.post(f"/posts/{post_id}/comments", json={"text": "second"}, headers=headers_b)

        resp = client.get(f"/posts/{post_id}/comments")
        assert resp.status_code == 200
        texts = [c["text"] for c in resp.json()]
        assert texts == ["first", "second"]

    def test_response_shape_is_unchanged(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        post_id = _create_post(client, headers_a)
        client.post(f"/posts/{post_id}/comments", json={"text": "hello"}, headers=headers_b)

        resp = client.get(f"/posts/{post_id}/comments")
        comment = resp.json()[0]
        assert set(comment.keys()) == {"id", "post_id", "user_id", "text", "created_at"}


class TestExistingNotificationBehavior:
    def test_comment_by_other_user_still_notifies_post_owner(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        post_id = _create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(f"/posts/{post_id}/comments", json={"text": "great post"}, headers=headers_b)

        assert resp.status_code == 201
        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        assert kwargs["post_owner_email"] == USER_A["email"]
        assert kwargs["actor_username"] == USER_B["username"]

    def test_own_comment_on_own_post_still_does_not_notify(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        post_id = _create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(f"/posts/{post_id}/comments", json={"text": "my own comment"}, headers=headers_a)

        assert resp.status_code == 201
        mock_notify.assert_not_called()
