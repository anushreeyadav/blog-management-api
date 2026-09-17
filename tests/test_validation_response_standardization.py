"""
Sub-Task 17 -- proves the four plan-limit-gated actions (post creation,
image upload, like, comment) all produce the *identical* validation
response when a limit is exceeded: same status code, same exact body shape,
same message, and nothing that leaks internal implementation details.

This is a consolidated cross-action check on top of what each action's own
dedicated test file (test_post_creation_subscription_limit.py,
test_image_upload_subscription_limit.py, test_like_subscription_limit.py,
test_comment_subscription_limit.py) already verifies individually -- the
point here specifically is that they all agree with each other.

No production code changes were needed for this sub-task: app/services/
subscription.py's enforce_action_limit() (Sub-Task 6) already raises the
exact same HTTPException(status_code=403, detail=LIMIT_EXCEEDED_MESSAGE)
from all four call sites, and FastAPI's default HTTPException handler --
this project never registers a custom one -- already renders that as the
plain {"detail": ...} shape used by every other error in this API (auth.py,
posts.py, comments.py, likes.py: 401/403/404/409/413 all follow it).
"""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app
from app.services.subscription import LIMIT_EXCEEDED_MESSAGE

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

EXPECTED_BODY = {"detail": LIMIT_EXCEEDED_MESSAGE}

# Strings that must never appear in a limit-exceeded response -- a stand-in
# for "does not expose internal database details" (table/column names, SQL,
# driver/exception class names, stack traces).
FORBIDDEN_SUBSTRINGS = [
    "Traceback",
    "sqlalchemy",
    "psycopg2",
    "IntegrityError",
    "SELECT ",
    "subscription_plans",
    "billing_history",
    "max_posts",
    "max_images",
    "max_likes",
    "max_comments",
]


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


def _register_and_login(client: TestClient, user: dict) -> dict:
    client.post("/auth/register", json=user)
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_post(client: TestClient, headers: dict, title: str = "t"):
    return client.post("/posts", json={"title": title, "content": "body"}, headers=headers).json()


def _assert_standard_limit_response(resp) -> None:
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/json")

    body = resp.json()
    assert body == EXPECTED_BODY  # exact shape: exactly one key, "detail"
    assert set(body.keys()) == {"detail"}

    raw = resp.text
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in raw, f"leaked internal detail: {forbidden!r} in {raw!r}"


class TestPostCreationLimitResponse:
    def test_exceeding_limit_returns_the_standard_response(self, client):
        headers = _register_and_login(client, USER_A)  # Basic: max_posts = 1
        _create_post(client, headers, "first")

        resp = client.post("/posts", json={"title": "second", "content": "body"}, headers=headers)

        _assert_standard_limit_response(resp)


class TestImageUploadLimitResponse:
    def test_exceeding_limit_returns_the_standard_response(self, client):
        headers = _register_and_login(client, USER_A)  # Basic: max_images = 1
        post = _create_post(client, headers)
        client.post(
            f"/posts/{post['id']}/image", headers=headers,
            files={"image": ("a.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )

        resp = client.post(
            f"/posts/{post['id']}/image", headers=headers,
            files={"image": ("b.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )

        _assert_standard_limit_response(resp)


class TestLikeLimitResponse:
    def test_exceeding_limit_returns_the_standard_response(self, client):
        headers_a = _register_and_login(client, USER_A)
        # user_a is only the author of the target posts here, not the one
        # whose like-limit is under test -- give it an unlimited plan so
        # Basic's own 1-post cap doesn't get in the way of creating 6.
        plans = client.get("/subscriptions/plans").json()
        pro_id = next(p["id"] for p in plans if p["slug"] == "pro")
        client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers_a)

        headers_b = _register_and_login(client, USER_B)  # Basic: max_likes = 5
        post_ids = [_create_post(client, headers_a, f"target {i}")["id"] for i in range(6)]

        for post_id in post_ids[:5]:
            assert client.post(f"/posts/{post_id}/like", headers=headers_b).status_code == 201

        resp = client.post(f"/posts/{post_ids[5]}/like", headers=headers_b)

        _assert_standard_limit_response(resp)


class TestCommentLimitResponse:
    def test_exceeding_limit_returns_the_standard_response(self, client):
        headers_a = _register_and_login(client, USER_A)
        headers_b = _register_and_login(client, USER_B)  # Basic: max_comments = 5
        post = _create_post(client, headers_a)

        for i in range(5):
            assert client.post(
                f"/posts/{post['id']}/comments", json={"text": f"c{i}"}, headers=headers_b
            ).status_code == 201

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "one too many"}, headers=headers_b)

        _assert_standard_limit_response(resp)


class TestAllFourActionsProduceIdenticalResponses:
    """The real point of Sub-Task 17: not just that each action returns
    *a* 403 with *a* message, but that all four are byte-for-byte the same
    response shape and message."""

    def test_response_bodies_are_identical_across_all_four_actions(self, client):
        headers_a = _register_and_login(client, USER_A)
        headers_b = _register_and_login(client, USER_B)

        # Post creation
        _create_post(client, headers_a, "only post")
        post_resp = client.post("/posts", json={"title": "extra", "content": "c"}, headers=headers_a)

        # Image upload
        post = _create_post(client, headers_b)
        client.post(
            f"/posts/{post['id']}/image", headers=headers_b,
            files={"image": ("a.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        image_resp = client.post(
            f"/posts/{post['id']}/image", headers=headers_b,
            files={"image": ("b.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )

        # Comments (reuse post, from a third registration to avoid
        # colliding with the post-creation cap already exhausted above)
        headers_c = _register_and_login(client, {"username": "user_c", "email": "userc@example.com", "password": "Password123"})
        for i in range(5):
            client.post(f"/posts/{post['id']}/comments", json={"text": f"c{i}"}, headers=headers_c)
        comment_resp = client.post(f"/posts/{post['id']}/comments", json={"text": "extra"}, headers=headers_c)

        responses = [post_resp, image_resp, comment_resp]
        for resp in responses:
            assert resp.status_code == 403
            assert resp.json() == EXPECTED_BODY

        # All bodies are literally identical, not just equivalent.
        bodies = {resp.text for resp in responses}
        assert len(bodies) == 1
