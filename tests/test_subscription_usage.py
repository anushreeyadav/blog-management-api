"""
Sub-Task 16 -- GET /subscriptions/usage: current plan-limit usage for the
authenticated user, reusing the same centralized counting logic
(app/services/subscription.py) that actually enforces the limits.
"""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


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
        yield test_client
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register(client: TestClient, user: dict) -> dict:
    client.post("/auth/register", json=user)
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()
    return next(p["id"] for p in plans if p["slug"] == slug)


def _usage(client: TestClient, headers: dict) -> dict:
    resp = client.get("/subscriptions/usage", headers=headers)
    assert resp.status_code == 200
    return resp.json()


class TestBasicUserUsage:
    def test_starts_at_zero_with_basic_limits(self, client):
        headers = _register(client, USER_A)

        usage = _usage(client, headers)

        assert usage["plan"] == "Basic"
        assert usage == {
            "plan": "Basic",
            "posts": {"used": 0, "limit": 1},
            "images": {"used": 0, "limit": 1},
            "likes": {"used": 0, "limit": 5},
            "comments": {"used": 0, "limit": 5},
        }

    def test_used_counts_increment_as_actions_are_taken(self, client):
        headers = _register(client, USER_A)
        headers_b = _register(client, USER_B)

        post = client.post("/posts", json={"title": "t", "content": "c"}, headers=headers).json()
        client.post(
            f"/posts/{post['id']}/image", headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        client.post(f"/posts/{post['id']}/like", headers=headers_b)
        client.post(f"/posts/{post['id']}/comments", json={"text": "nice"}, headers=headers_b)

        usage = _usage(client, headers)
        assert usage["posts"]["used"] == 1
        assert usage["images"]["used"] == 1

        # Likes/comments here were made by USER_B against USER_A's post --
        # they count toward USER_B's usage, not USER_A's.
        assert usage["likes"]["used"] == 0
        assert usage["comments"]["used"] == 0

        usage_b = _usage(client, headers_b)
        assert usage_b["likes"]["used"] == 1
        assert usage_b["comments"]["used"] == 1

    def test_hitting_the_post_limit_is_reflected_in_usage(self, client):
        headers = _register(client, USER_A)
        client.post("/posts", json={"title": "t", "content": "c"}, headers=headers)

        usage = _usage(client, headers)
        assert usage["posts"] == {"used": 1, "limit": 1}


class TestPremiumUserUsage:
    def test_shows_premium_limits(self, client):
        headers = _register(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, "premium")}, headers=headers)

        usage = _usage(client, headers)

        assert usage["plan"] == "Premium"
        assert usage["posts"]["limit"] == 2
        assert usage["images"]["limit"] == 2
        assert usage["likes"]["limit"] == 25
        assert usage["comments"]["limit"] == 25

    def test_images_used_sums_across_all_of_the_users_posts(self, client):
        headers = _register(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, "premium")}, headers=headers)

        post1 = client.post("/posts", json={"title": "p1", "content": "c"}, headers=headers).json()
        post2 = client.post("/posts", json={"title": "p2", "content": "c"}, headers=headers).json()
        client.post(
            f"/posts/{post1['id']}/image", headers=headers,
            files={"image": ("a.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        client.post(
            f"/posts/{post2['id']}/image", headers=headers,
            files={"image": ("b.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )

        usage = _usage(client, headers)
        assert usage["posts"]["used"] == 2
        assert usage["images"]["used"] == 2  # one on each post, summed


class TestProUserUsage:
    def test_unlimited_values_are_represented_as_null(self, client):
        headers = _register(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, "pro")}, headers=headers)

        usage = _usage(client, headers)

        assert usage["plan"] == "Pro"
        assert usage["posts"]["limit"] is None
        assert usage["images"]["limit"] is None
        assert usage["likes"]["limit"] is None
        assert usage["comments"]["limit"] is None

    def test_used_counts_still_accurate_despite_no_limit(self, client):
        headers = _register(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, "pro")}, headers=headers)

        for i in range(4):
            client.post("/posts", json={"title": f"p{i}", "content": "c"}, headers=headers)

        usage = _usage(client, headers)
        assert usage["posts"] == {"used": 4, "limit": None}


class TestUsageDoesNotExposeOtherUsers:
    def test_two_users_see_only_their_own_usage(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        client.post("/posts", json={"title": "a1", "content": "c"}, headers=headers_a)

        usage_a = _usage(client, headers_a)
        usage_b = _usage(client, headers_b)

        assert usage_a["posts"]["used"] == 1
        assert usage_b["posts"]["used"] == 0

    def test_requires_authentication(self, client):
        resp = client.get("/subscriptions/usage")
        assert resp.status_code == 401
