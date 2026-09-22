"""
HTTP-level tests for GET /dashboard/me, wired into app.main.app.

Unit coverage of the underlying aggregation lives in
test_dashboard_service.py; this file covers the router itself: auth
enforcement, response shape, and that there is no way -- via URL, query
string, or request body -- to ask for another user's dashboard.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app


@pytest.fixture()
def auth_client():
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


def _register_and_login(client: TestClient, username: str, email: str, password: str = "strongpass123") -> dict:
    client.post("/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestDashboardAuth:
    def test_requires_authentication(self, auth_client):
        resp = auth_client.get("/dashboard/me")
        assert resp.status_code == 401

    def test_rejects_invalid_token(self, auth_client):
        resp = auth_client.get("/dashboard/me", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    def test_no_user_id_parameter_accepted(self, auth_client):
        """
        There is no /dashboard/{user_id} route at all -- confirms a client
        cannot target another user's dashboard by supplying an id anywhere
        in the request. A query string id is silently ignored (FastAPI
        drops unrecognized params) rather than changing whose data comes
        back, since the handler never reads one.
        """
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        auth_client.post("/auth/register", json={"username": "bob", "email": "bob@example.com", "password": "strongpass123"})

        resp = auth_client.get("/dashboard/me?user_id=999999", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "alice"


class TestDashboardResponseShape:
    def test_returns_caller_identity_and_zeroed_statistics_for_new_user(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")

        resp = auth_client.get("/dashboard/me", headers=headers)
        assert resp.status_code == 200
        body = resp.json()

        assert body["user"]["username"] == "alice"
        assert isinstance(body["user"]["id"], int)
        assert body["statistics"] == {
            "total_posts": 0,
            "total_comments_received": 0,
            "total_likes_received": 0,
            "total_views": 0,
        }
        assert body["post_analytics"] == []
        assert body["post_activity"] == []

    def test_never_exposes_sensitive_fields(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")

        resp = auth_client.get("/dashboard/me", headers=headers)
        body = resp.json()

        assert set(body.keys()) == {"user", "statistics", "post_analytics", "post_activity"}
        assert set(body["user"].keys()) == {"id", "username"}
        for forbidden in ("password", "password_hash", "email", "subscription_plan_id", "access_token", "token"):
            assert forbidden not in body["user"]

    def test_statistics_reflect_real_activity(self, auth_client):
        alice_headers = _register_and_login(auth_client, "alice", "alice@example.com")
        bob_headers = _register_and_login(auth_client, "bob", "bob@example.com")

        post_id = auth_client.post(
            "/posts", json={"title": "Hello", "content": "World"}, headers=alice_headers
        ).json()["id"]

        # Bob likes and comments on Alice's post -- both count as *received*
        # for Alice (her post), and as neither "received" for Bob (he owns
        # no posts of his own for anything to be received on).
        auth_client.post(f"/posts/{post_id}/like", headers=bob_headers)
        auth_client.post(f"/posts/{post_id}/comments", json={"text": "nice!"}, headers=bob_headers)

        # Two views of the post, by two different (unauthenticated) callers.
        auth_client.get(f"/posts/{post_id}")
        auth_client.get(f"/posts/{post_id}")

        alice_stats = auth_client.get("/dashboard/me", headers=alice_headers).json()["statistics"]
        assert alice_stats == {
            "total_posts": 1,
            "total_comments_received": 1,
            "total_likes_received": 1,
            "total_views": 2,
        }

        bob_stats = auth_client.get("/dashboard/me", headers=bob_headers).json()["statistics"]
        assert bob_stats == {
            "total_posts": 0,
            "total_comments_received": 0,
            "total_likes_received": 0,
            "total_views": 0,
        }


def _upgrade_to_pro(client: TestClient, headers: dict) -> None:
    """Basic caps a user at 1 post -- Pro is unlimited, needed for tests with several posts."""
    plans = client.get("/subscriptions/plans").json()["plans"]
    pro_id = next(p["id"] for p in plans if p["slug"] == "pro")
    resp = client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers)
    assert resp.status_code == 201


class TestPostAnalytics:
    def test_includes_only_this_users_own_posts_with_their_counts(self, auth_client):
        alice_headers = _register_and_login(auth_client, "alice", "alice@example.com")
        bob_headers = _register_and_login(auth_client, "bob", "bob@example.com")
        _upgrade_to_pro(auth_client, alice_headers)

        post_1 = auth_client.post(
            "/posts", json={"title": "FastAPI Best Practices", "content": "..."}, headers=alice_headers
        ).json()
        post_2 = auth_client.post(
            "/posts", json={"title": "Learning SQLAlchemy", "content": "..."}, headers=alice_headers
        ).json()
        bobs_post = auth_client.post(
            "/posts", json={"title": "Bob's post", "content": "..."}, headers=bob_headers
        ).json()

        # post_1: one like, one comment, two views.
        auth_client.post(f"/posts/{post_1['id']}/like", headers=bob_headers)
        auth_client.post(f"/posts/{post_1['id']}/comments", json={"text": "nice"}, headers=bob_headers)
        auth_client.get(f"/posts/{post_1['id']}")
        auth_client.get(f"/posts/{post_1['id']}")
        # post_2: no activity at all.
        # Bob's own post gets a like -- must never appear in Alice's analytics.
        auth_client.post(f"/posts/{bobs_post['id']}/like", headers=alice_headers)

        resp = auth_client.get("/dashboard/me", headers=alice_headers)
        assert resp.status_code == 200
        analytics = resp.json()["post_analytics"]

        assert len(analytics) == 2
        assert {item["post_id"] for item in analytics} == {post_1["id"], post_2["id"]}
        assert bobs_post["id"] not in {item["post_id"] for item in analytics}

        by_id = {item["post_id"]: item for item in analytics}
        assert by_id[post_1["id"]] == {
            "post_id": post_1["id"],
            "title": "FastAPI Best Practices",
            "likes": 1,
            "comments": 1,
            "views": 2,
        }
        assert by_id[post_2["id"]] == {
            "post_id": post_2["id"],
            "title": "Learning SQLAlchemy",
            "likes": 0,
            "comments": 0,
            "views": 0,
        }

    def test_bobs_dashboard_never_shows_alices_posts(self, auth_client):
        alice_headers = _register_and_login(auth_client, "alice", "alice@example.com")
        bob_headers = _register_and_login(auth_client, "bob", "bob@example.com")

        auth_client.post("/posts", json={"title": "Alice's post", "content": "..."}, headers=alice_headers)

        resp = auth_client.get("/dashboard/me", headers=bob_headers)
        assert resp.status_code == 200
        assert resp.json()["post_analytics"] == []


class TestPostActivity:
    """
    Day-grouping correctness (multiple posts/days, ordering) is covered at
    the service level in test_dashboard_service.py, where created_at can be
    controlled directly. Posts created through the real HTTP endpoint all
    land on "today" (created_at is server-assigned -- see
    app/routers/posts.py), so these just confirm the router wires that
    aggregation in and keeps it user-scoped.
    """

    def test_includes_an_entry_for_todays_post_creation(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)

        today = datetime.now(timezone.utc).date().isoformat()

        resp = auth_client.get("/dashboard/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["post_activity"] == [{"date": today, "posts": 1}]

    def test_never_includes_another_users_post_activity(self, auth_client):
        alice_headers = _register_and_login(auth_client, "alice", "alice@example.com")
        bob_headers = _register_and_login(auth_client, "bob", "bob@example.com")

        auth_client.post("/posts", json={"title": "Bob's post", "content": "..."}, headers=bob_headers)

        resp = auth_client.get("/dashboard/me", headers=alice_headers)
        assert resp.status_code == 200
        assert resp.json()["post_activity"] == []


class TestViewTrackingPolicy:
    """
    Confirms the pre-existing view-tracking policy (see Post.view_count in
    app/models.py) actually behaves as documented: only GET /posts/{id}
    counts as a view, repeated requests are not deduplicated, and
    unrelated post actions never inflate the count.
    """

    def test_each_fetch_of_a_post_counts_as_a_separate_view(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        post_id = auth_client.post(
            "/posts", json={"title": "Hello", "content": "World"}, headers=headers
        ).json()["id"]

        for _ in range(3):
            auth_client.get(f"/posts/{post_id}")

        stats = auth_client.get("/dashboard/me", headers=headers).json()["statistics"]
        assert stats["total_views"] == 3

    def test_unrelated_post_actions_do_not_count_as_views(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        post_id = auth_client.post(
            "/posts", json={"title": "Hello", "content": "World"}, headers=headers
        ).json()["id"]

        auth_client.get("/posts")
        auth_client.get("/posts/mine", headers=headers)
        auth_client.put(f"/posts/{post_id}", json={"title": "Updated"}, headers=headers)

        stats = auth_client.get("/dashboard/me", headers=headers).json()["statistics"]
        assert stats["total_views"] == 0
