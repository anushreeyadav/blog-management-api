"""
Sub-Task 9 -- like subscription limits, exercised through the real
POST/DELETE /posts/{post_id}/like endpoints with the real seeded Basic /
Premium / Pro plans: authenticate -> resolve active plan -> count current
active likes -> compare against max_likes -> allow or reject with the
standard message.

Chosen meaning of max_likes (see app/services/subscription.py for the full
rationale): "current active likes", a live count of the user's existing
Like rows, not a lifetime total and not reset on a billing-period boundary.
Unliking a post frees its slot immediately -- this is exercised explicitly
below (TestUnlikeFreesUpCapacity).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}

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
        yield test_client
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient) -> dict:
    client.post("/auth/register", json=USER_A)
    resp = client.post("/auth/login", json={"username": USER_A["username"], "password": USER_A["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()
    return next(p["id"] for p in plans if p["slug"] == slug)


def _create_posts(client: TestClient, headers: dict, count: int) -> list[int]:
    """Posts to like need an author -- use a second user so USER_A's own
    post-creation limit never interferes with testing the like limit."""
    client.post("/auth/register", json={"username": "author", "email": "author@example.com", "password": "Password123"})
    login = client.post("/auth/login", json={"username": "author", "password": "Password123"})
    author_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    ids = []
    for i in range(count):
        resp = client.post("/posts", json={"title": f"post {i}", "content": "body"}, headers=author_headers)
        if resp.status_code != 201:
            # The author account is also Basic by default (max 1 post) --
            # give it an unlimited plan once, lazily, the first time more
            # than 1 post is needed.
            plan_id = _plan_id(client, "pro")
            client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=author_headers)
            resp = client.post("/posts", json={"title": f"post {i}", "content": "body"}, headers=author_headers)
        ids.append(resp.json()["id"])
    return ids


class TestLikeWithinLimit:
    def test_like_well_under_the_limit_succeeds(self, client):
        headers = _register_and_login(client)  # Basic: max_likes = 5
        post_ids = _create_posts(client, headers, 1)

        resp = client.post(f"/posts/{post_ids[0]}/like", headers=headers)
        assert resp.status_code == 201


class TestLikeAtLimit:
    def test_the_last_available_like_still_succeeds(self, client):
        headers = _register_and_login(client)  # Basic: max_likes = 5
        post_ids = _create_posts(client, headers, 5)

        for i, post_id in enumerate(post_ids[:4]):
            assert client.post(f"/posts/{post_id}/like", headers=headers).status_code == 201

        # The 5th like brings usage exactly to the limit -- still allowed.
        resp = client.post(f"/posts/{post_ids[4]}/like", headers=headers)
        assert resp.status_code == 201


class TestLikeBeyondLimit:
    def test_the_next_like_past_the_limit_is_rejected(self, client):
        headers = _register_and_login(client)  # Basic: max_likes = 5
        post_ids = _create_posts(client, headers, 6)

        for post_id in post_ids[:5]:
            assert client.post(f"/posts/{post_id}/like", headers=headers).status_code == 201

        resp = client.post(f"/posts/{post_ids[5]}/like", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE


class TestDuplicateLikeStillProtected:
    def test_liking_the_same_post_twice_returns_409_not_the_limit_error(self, client):
        headers = _register_and_login(client)
        post_ids = _create_posts(client, headers, 1)

        assert client.post(f"/posts/{post_ids[0]}/like", headers=headers).status_code == 201

        resp = client.post(f"/posts/{post_ids[0]}/like", headers=headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Post already liked"

    def test_duplicate_rejection_does_not_consume_a_limit_slot(self, client):
        """The duplicate check runs before the limit check, so a rejected
        duplicate must not itself count against -- or be miscounted by --
        the plan's limit."""
        headers = _register_and_login(client)
        post_ids = _create_posts(client, headers, 5)
        assert client.post(f"/posts/{post_ids[0]}/like", headers=headers).status_code == 201

        for _ in range(3):
            assert client.post(f"/posts/{post_ids[0]}/like", headers=headers).status_code == 409

        # Still 4 slots left (1 used, Basic allows 5), not exhausted by the
        # repeated duplicate attempts above.
        for post_id in post_ids[1:5]:
            assert client.post(f"/posts/{post_id}/like", headers=headers).status_code == 201


class TestProUnlimitedLikes:
    def test_pro_user_can_like_far_beyond_basics_cap(self, client):
        headers = _register_and_login(client)
        plan_id = _plan_id(client, "pro")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers).status_code == 201
        post_ids = _create_posts(client, headers, 8)

        for post_id in post_ids:
            resp = client.post(f"/posts/{post_id}/like", headers=headers)
            assert resp.status_code == 201


class TestUnlikeFreesUpCapacity:
    """Documents the chosen semantics: max_likes counts *current* active
    likes, so unliking immediately frees a slot for a new like."""

    def test_existing_unlike_endpoint_still_works(self, client):
        headers = _register_and_login(client)
        post_ids = _create_posts(client, headers, 1)
        client.post(f"/posts/{post_ids[0]}/like", headers=headers)

        resp = client.delete(f"/posts/{post_ids[0]}/like", headers=headers)
        assert resp.status_code == 204

    def test_unliking_at_the_limit_allows_a_new_like(self, client):
        headers = _register_and_login(client)  # Basic: max_likes = 5
        post_ids = _create_posts(client, headers, 6)

        for post_id in post_ids[:5]:
            assert client.post(f"/posts/{post_id}/like", headers=headers).status_code == 201

        # At the limit: a 6th like is rejected.
        assert client.post(f"/posts/{post_ids[5]}/like", headers=headers).status_code == 403

        # Unlike one of the first five -- frees a slot immediately.
        assert client.delete(f"/posts/{post_ids[0]}/like", headers=headers).status_code == 204

        # Now the 6th post can be liked.
        resp = client.post(f"/posts/{post_ids[5]}/like", headers=headers)
        assert resp.status_code == 201

    def test_unlike_of_a_post_never_liked_still_returns_404(self, client):
        headers = _register_and_login(client)
        post_ids = _create_posts(client, headers, 1)

        resp = client.delete(f"/posts/{post_ids[0]}/like", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Like not found"
