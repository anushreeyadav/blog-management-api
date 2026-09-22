"""
Phase 12 -- consolidated JWT/security audit for GET /dashboard/me.

Unlike test_dashboard_api.py (which spreads auth wiring, response shape,
and per-feature isolation across several narrowly-scoped test classes),
this file is the single consolidated evidence for exactly the three
scenarios named in the security review: an authenticated user gets their
own dashboard, an unauthenticated/invalid request is rejected, and two
users with fully independent activity (posts, comments, likes, and views)
never see even a trace of each other's data.

No production code changes were needed for this audit, the same
conclusion test_subscription_endpoint_security_audit.py reached for the
subscription/billing endpoints: GET /dashboard/me already derives its
user exclusively from the JWT via get_current_user (see
app/routers/dashboard.py), there is no user_id anywhere in its path/query/
body, and every app/services/dashboard_service.py query is explicitly
filtered by that resolved user's id.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import create_access_token
from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}


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
    resp = client.post("/auth/register", json=user)
    user_id = resp.json()["id"]
    login = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user_id


def _upgrade_to_pro(client: TestClient, headers: dict) -> None:
    """Basic caps a user at 1 post -- the isolation test needs more than that."""
    plans = client.get("/subscriptions/plans").json()["plans"]
    pro_id = next(p["id"] for p in plans if p["slug"] == "pro")
    resp = client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers)
    assert resp.status_code == 201


def _create_post(client: TestClient, headers: dict, title: str) -> dict:
    resp = client.post("/posts", json={"title": title, "content": "body"}, headers=headers)
    assert resp.status_code == 201
    return resp.json()


def _dashboard(client: TestClient, headers: dict) -> dict:
    resp = client.get("/dashboard/me", headers=headers)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Test 1 -- Authenticated user
# ---------------------------------------------------------------------------


class TestAuthenticatedUserGetsOnlyTheirOwnDashboard:
    def test_logged_in_user_can_access_the_dashboard(self, client):
        headers, _ = _register(client, USER_A)
        _create_post(client, headers, "Hello")

        resp = client.get("/dashboard/me", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["username"] == USER_A["username"]
        assert body["statistics"]["total_posts"] == 1

    def test_response_identifies_the_caller_and_nobody_else(self, client):
        headers_a, _ = _register(client, USER_A)
        _register(client, USER_B)  # exists in the DB; never requested by A

        body = _dashboard(client, headers_a)

        assert body["user"]["username"] == "user_a"
        assert body["user"]["username"] != "user_b"


# ---------------------------------------------------------------------------
# Test 2 -- Unauthenticated user
# ---------------------------------------------------------------------------


class TestUnauthenticatedRequestIsRejected:
    def test_no_authorization_header_returns_401(self, client):
        resp = client.get("/dashboard/me")
        assert resp.status_code == 401

    def test_missing_bearer_scheme_returns_401(self, client):
        resp = client.get("/dashboard/me", headers={"Authorization": "not-a-bearer-token"})
        assert resp.status_code == 401

    def test_malformed_token_returns_401(self, client):
        resp = client.get("/dashboard/me", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    def test_expired_token_returns_401(self, client):
        _, user_id = _register(client, USER_A)
        expired_token = create_access_token(data={"sub": str(user_id)}, expires_delta=timedelta(minutes=-5))

        resp = client.get("/dashboard/me", headers={"Authorization": f"Bearer {expired_token}"})

        assert resp.status_code == 401

    def test_token_for_a_nonexistent_user_returns_401(self, client):
        token = create_access_token(data={"sub": "999999"})

        resp = client.get("/dashboard/me", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 401

    def test_response_never_leaks_dashboard_data_on_rejection(self, client):
        headers, _ = _register(client, USER_A)
        _create_post(client, headers, "Hello")

        resp = client.get("/dashboard/me")

        assert resp.status_code == 401
        assert "statistics" not in resp.json()
        assert "post_analytics" not in resp.json()


# ---------------------------------------------------------------------------
# Test 3 -- User isolation
# ---------------------------------------------------------------------------


class TestUserIsolation:
    """
    User A and User B each get their own posts, and each acts on the
    other's post (a like + a comment), so every metric has a real
    opportunity to leak across the boundary if the isolation were broken:
    total_posts, total_comments_received, total_likes_received,
    total_views, post_analytics, and post_activity all get checked in
    both directions.
    """

    @pytest.fixture()
    def scenario(self, client):
        headers_a, _ = _register(client, USER_A)
        headers_b, _ = _register(client, USER_B)
        _upgrade_to_pro(client, headers_a)
        _upgrade_to_pro(client, headers_b)

        post_a1 = _create_post(client, headers_a, "Alice's First Post")
        post_a2 = _create_post(client, headers_a, "Alice's Second Post")
        post_b1 = _create_post(client, headers_b, "Bob's Only Post")

        # Cross-activity: each user acts on the other's post.
        client.post(f"/posts/{post_b1['id']}/like", headers=headers_a)
        client.post(f"/posts/{post_b1['id']}/comments", json={"text": "nice, Bob"}, headers=headers_a)
        client.post(f"/posts/{post_a1['id']}/like", headers=headers_b)
        client.post(f"/posts/{post_a1['id']}/comments", json={"text": "nice, Alice"}, headers=headers_b)

        # Distinct view counts per user's posts.
        client.get(f"/posts/{post_a1['id']}")
        client.get(f"/posts/{post_a1['id']}")
        client.get(f"/posts/{post_a2['id']}")
        client.get(f"/posts/{post_b1['id']}")

        return {
            "headers_a": headers_a,
            "headers_b": headers_b,
            "post_a1": post_a1,
            "post_a2": post_a2,
            "post_b1": post_b1,
        }

    def test_user_a_sees_only_her_own_posts_comments_likes_and_views(self, client, scenario):
        body = _dashboard(client, scenario["headers_a"])

        assert body["statistics"] == {
            "total_posts": 2,
            "total_comments_received": 1,  # Bob's comment on her post_a1
            "total_likes_received": 1,  # Bob's like on her post
            "total_views": 3,  # 2 + 1 views across her own two posts
        }

        post_ids = {item["post_id"] for item in body["post_analytics"]}
        assert post_ids == {scenario["post_a1"]["id"], scenario["post_a2"]["id"]}
        assert scenario["post_b1"]["id"] not in post_ids

        titles = {item["title"] for item in body["post_analytics"]}
        assert "Bob's Only Post" not in titles

    def test_user_b_sees_only_his_own_posts_comments_likes_and_views(self, client, scenario):
        body = _dashboard(client, scenario["headers_b"])

        assert body["statistics"] == {
            "total_posts": 1,
            "total_comments_received": 1,  # Alice's comment on his post_b1
            "total_likes_received": 1,  # Alice's like on his post
            "total_views": 1,
        }

        post_ids = {item["post_id"] for item in body["post_analytics"]}
        assert post_ids == {scenario["post_b1"]["id"]}
        assert scenario["post_a1"]["id"] not in post_ids
        assert scenario["post_a2"]["id"] not in post_ids

        titles = {item["title"] for item in body["post_analytics"]}
        assert "Alice's First Post" not in titles
        assert "Alice's Second Post" not in titles

    def test_the_two_dashboards_are_not_the_same(self, client, scenario):
        body_a = _dashboard(client, scenario["headers_a"])
        body_b = _dashboard(client, scenario["headers_b"])

        assert body_a["statistics"] != body_b["statistics"]
        assert body_a["post_analytics"] != body_b["post_analytics"]
        assert body_a["user"]["id"] != body_b["user"]["id"]

    def test_post_activity_never_crosses_the_boundary(self, client, scenario):
        body_a = _dashboard(client, scenario["headers_a"])
        body_b = _dashboard(client, scenario["headers_b"])

        # Both users created their posts today, so both activity lists have
        # one entry for today -- but the *counts* must reflect only each
        # user's own post creations (2 for Alice, 1 for Bob), not the
        # combined total of 3.
        assert sum(point["posts"] for point in body_a["post_activity"]) == 2
        assert sum(point["posts"] for point in body_b["post_activity"]) == 1
