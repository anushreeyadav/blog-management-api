"""
Sub-Task 7 -- post creation subscription limit, exercised through the real
POST /posts endpoint (not the service directly -- that's
tests/test_subscription_service.py's job) with the real seeded Basic /
Premium / Pro plans, to prove the wiring in app/routers/posts.py itself:
authenticate -> resolve active plan -> count existing posts -> compare
against max_posts -> allow or reject with the standard message.
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
    plans = client.get("/subscriptions/plans").json()["plans"]
    return next(p["id"] for p in plans if p["slug"] == slug)


def _create_post(client: TestClient, headers: dict, title: str):
    return client.post("/posts", json={"title": title, "content": "body"}, headers=headers)


class TestBasicUserPostLimit:
    def test_basic_user_creates_first_post_successfully(self, client):
        headers = _register_and_login(client)  # registration defaults to Basic

        resp = _create_post(client, headers, "First post")

        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "First post"
        assert body["author_id"] is not None

    def test_basic_user_second_post_is_rejected(self, client):
        headers = _register_and_login(client)
        assert _create_post(client, headers, "First post").status_code == 201

        resp = _create_post(client, headers, "Second post")

        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE


class TestPremiumUserPostLimit:
    def _subscribe_to_premium(self, client, headers):
        plan_id = _plan_id(client, "premium")
        resp = client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)
        assert resp.status_code == 201

    def test_premium_user_can_create_two_posts(self, client):
        headers = _register_and_login(client)
        self._subscribe_to_premium(client, headers)

        assert _create_post(client, headers, "Post 1").status_code == 201
        assert _create_post(client, headers, "Post 2").status_code == 201

    def test_premium_user_is_rejected_on_third_post(self, client):
        headers = _register_and_login(client)
        self._subscribe_to_premium(client, headers)
        _create_post(client, headers, "Post 1")
        _create_post(client, headers, "Post 2")

        resp = _create_post(client, headers, "Post 3")

        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE


class TestProUserPostLimit:
    def test_pro_user_can_create_many_posts_without_an_artificial_limit(self, client):
        headers = _register_and_login(client)
        plan_id = _plan_id(client, "pro")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers).status_code == 201

        for i in range(10):
            resp = _create_post(client, headers, f"Post {i}")
            assert resp.status_code == 201


class TestExistingBehaviorUnchanged:
    """
    Everything about post creation other than the limit itself -- auth
    requirement, response shape, ownership -- must be exactly as before.
    """

    def test_unauthenticated_request_still_returns_401(self, client):
        resp = client.post("/posts", json={"title": "t", "content": "c"})
        assert resp.status_code == 401

    def test_successful_response_shape_is_unchanged(self, client):
        headers = _register_and_login(client)
        resp = _create_post(client, headers, "Shape check")

        body = resp.json()
        assert set(body.keys()) == {"id", "title", "content", "author_id", "created_at", "image", "images"}
        assert body["image"] is None
        assert body["images"] == []

    def test_within_limit_creation_does_not_add_extra_fields_or_change_status(self, client):
        headers = _register_and_login(client)
        resp = _create_post(client, headers, "Still 201")
        assert resp.status_code == 201
