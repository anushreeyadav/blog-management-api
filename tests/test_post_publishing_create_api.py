"""
POST /posts publishing options (publish_now / save_draft / schedule),
exercised through the real endpoint with the real seeded plans -- same
in-memory database setup as tests/test_post_creation_subscription_limit.py.
Covers the three creation paths, their stored state, the validation errors,
and that everything POST /posts already did (auth, limits, image upload,
response shape) is unchanged.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app
from app.schemas import SCHEDULED_AT_ERRORS

USER = {"username": "writer", "email": "writer@example.com", "password": "Password123"}
LIMIT_MESSAGE = "You’ve reached your plan limit. Kindly upgrade your plan to continue."

# A minimal valid 1x1 PNG, for the image-upload regression check.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.fixture()
def env():
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


def _login(client: TestClient, plan: str | None = "pro") -> dict:
    client.post("/auth/register", json=USER)
    token = client.post("/auth/login", json={"username": USER["username"], "password": USER["password"]}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    if plan:
        plans = client.get("/subscriptions/plans").json()["plans"]
        plan_id = next(p["id"] for p in plans if p["slug"] == plan)
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers).status_code == 201
    return headers


def _db_post(session_factory, post_id: int) -> models.Post:
    db = session_factory()
    try:
        return db.query(models.Post).filter(models.Post.id == post_id).one()
    finally:
        db.close()


def _future_iso(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**(delta or {"days": 2}))).isoformat()


def _parse(ts: str) -> datetime:
    value = datetime.fromisoformat(ts)
    # SQLite hands back naive UTC datetimes; Postgres returns aware ones.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The three creation paths
# ---------------------------------------------------------------------------


class TestPublishNow:
    def test_default_body_publishes_immediately(self, env):
        client, sessions = env
        headers = _login(client)
        before = datetime.now(timezone.utc) - timedelta(seconds=5)

        resp = client.post("/posts", json={"title": "Live", "content": "body"}, headers=headers)

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert body["published_at"] is not None
        assert before <= _parse(body["published_at"]) <= datetime.now(timezone.utc) + timedelta(seconds=5)

        stored = _db_post(sessions, body["id"])
        assert stored.status == "published"
        assert stored.scheduled_at is None
        assert stored.published_at is not None

    def test_explicit_publish_now(self, env):
        client, _ = env
        headers = _login(client)

        resp = client.post(
            "/posts", json={"title": "Live", "content": "body", "publish_option": "publish_now"}, headers=headers
        )

        assert resp.status_code == 201
        assert resp.json()["status"] == "published"
        assert resp.json()["published_at"] is not None
        assert resp.json()["scheduled_at"] is None

    def test_published_post_is_readable_by_anyone(self, env):
        client, _ = env
        headers = _login(client)
        post_id = client.post("/posts", json={"title": "Live", "content": "body"}, headers=headers).json()["id"]

        assert client.get(f"/posts/{post_id}").status_code == 200
        assert [p["id"] for p in client.get("/posts").json()["items"]] == [post_id]


class TestSaveDraft:
    def test_creates_a_draft(self, env):
        client, sessions = env
        headers = _login(client)

        resp = client.post(
            "/posts", json={"title": "WIP", "content": "body", "publish_option": "save_draft"}, headers=headers
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "draft"
        assert body["scheduled_at"] is None
        assert body["published_at"] is None

        stored = _db_post(sessions, body["id"])
        assert stored.status == "draft"
        assert stored.scheduled_at is None
        assert stored.published_at is None

    def test_draft_with_scheduled_at_is_rejected(self, env):
        client, sessions = env
        headers = _login(client)

        resp = client.post(
            "/posts",
            json={"title": "WIP", "content": "body", "publish_option": "save_draft", "scheduled_at": _future_iso()},
            headers=headers,
        )

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["body", "scheduled_at"]
        db = sessions()
        assert db.query(models.Post).count() == 0
        db.close()


class TestSchedule:
    def test_creates_a_scheduled_post(self, env):
        client, sessions = env
        headers = _login(client)
        when = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)

        resp = client.post(
            "/posts",
            json={"title": "Later", "content": "body", "publish_option": "schedule", "scheduled_at": when.isoformat()},
            headers=headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "scheduled"
        assert _parse(body["scheduled_at"]) == when
        assert body["published_at"] is None

        stored = _db_post(sessions, body["id"])
        assert stored.status == "scheduled"
        assert stored.published_at is None
        assert stored.scheduled_at is not None

    def test_schedule_with_non_utc_offset_keeps_the_same_instant(self, env):
        client, _ = env
        headers = _login(client)
        when = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
        ist = when.astimezone(timezone(timedelta(hours=5, minutes=30)))

        resp = client.post(
            "/posts",
            json={"title": "Later", "content": "body", "publish_option": "schedule", "scheduled_at": ist.isoformat()},
            headers=headers,
        )

        assert resp.status_code == 201
        assert _parse(resp.json()["scheduled_at"]) == when

    def test_missing_scheduled_at_is_rejected(self, env):
        client, _ = env
        headers = _login(client)

        resp = client.post(
            "/posts", json={"title": "Later", "content": "body", "publish_option": "schedule"}, headers=headers
        )

        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "scheduled_at"]
        assert error["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_required"]

    @pytest.mark.parametrize("delta", [timedelta(minutes=-1), timedelta(days=-30), timedelta(0)])
    def test_past_or_current_time_is_rejected(self, env, delta):
        client, sessions = env
        headers = _login(client)

        resp = client.post(
            "/posts",
            json={
                "title": "Later",
                "content": "body",
                "publish_option": "schedule",
                "scheduled_at": (datetime.now(timezone.utc) + delta).isoformat(),
            },
            headers=headers,
        )

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_not_in_future"]
        db = sessions()
        assert db.query(models.Post).count() == 0
        db.close()

    def test_time_without_timezone_is_rejected(self, env):
        client, _ = env
        headers = _login(client)
        naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat()

        resp = client.post(
            "/posts",
            json={"title": "Later", "content": "body", "publish_option": "schedule", "scheduled_at": naive},
            headers=headers,
        )

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_missing_timezone"]

    def test_publish_now_with_scheduled_at_is_rejected(self, env):
        client, _ = env
        headers = _login(client)

        resp = client.post(
            "/posts",
            json={"title": "Live", "content": "body", "publish_option": "publish_now", "scheduled_at": _future_iso()},
            headers=headers,
        )

        assert resp.status_code == 422


class TestUnknownOption:
    def test_unknown_publish_option_is_rejected(self, env):
        client, _ = env
        headers = _login(client)

        resp = client.post(
            "/posts", json={"title": "t", "content": "body", "publish_option": "publish"}, headers=headers
        )

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["body", "publish_option"]

    def test_client_supplied_status_and_published_at_are_rejected(self, env):
        client, sessions = env
        headers = _login(client)

        resp = client.post(
            "/posts",
            json={
                "title": "t",
                "content": "body",
                "publish_option": "save_draft",
                "status": "published",
                "published_at": "2020-01-01T00:00:00Z",
            },
            headers=headers,
        )

        assert resp.status_code == 422
        errors = {tuple(e["loc"]): e["msg"] for e in resp.json()["detail"]}
        assert errors == {
            ("body", "status"): SCHEDULED_AT_ERRORS["status_read_only"],
            ("body", "published_at"): SCHEDULED_AT_ERRORS["published_at_read_only"],
        }
        db = sessions()
        assert db.query(models.Post).count() == 0
        db.close()


# ---------------------------------------------------------------------------
# Existing behavior still applies to every path
# ---------------------------------------------------------------------------


class TestExistingBehaviorUnchanged:
    @pytest.mark.parametrize(
        "extra",
        [{}, {"publish_option": "save_draft"}, {"publish_option": "schedule", "scheduled_at": _future_iso()}],
    )
    def test_authentication_still_required(self, env, extra):
        client, _ = env
        resp = client.post("/posts", json={"title": "t", "content": "c", **extra})
        assert resp.status_code == 401

    @pytest.mark.parametrize("option", ["publish_now", "save_draft"])
    def test_title_content_validation_unchanged(self, env, option):
        client, _ = env
        headers = _login(client)
        resp = client.post("/posts", json={"title": "   ", "content": "c", "publish_option": option}, headers=headers)
        assert resp.status_code == 422

    def test_author_is_the_caller_for_every_path(self, env):
        client, _ = env
        headers = _login(client)
        me = client.get("/auth/me", headers=headers).json()["id"]
        for extra in ({}, {"publish_option": "save_draft"}, {"publish_option": "schedule", "scheduled_at": _future_iso()}):
            body = client.post("/posts", json={"title": "t", "content": "c", **extra}, headers=headers).json()
            assert body["author_id"] == me

    @pytest.mark.parametrize(
        "extra",
        [{"publish_option": "save_draft"}, {"publish_option": "schedule", "scheduled_at": _future_iso()}],
    )
    def test_drafts_and_scheduled_posts_count_toward_the_plan_limit(self, env, extra):
        client, _ = env
        headers = _login(client, plan=None)  # Basic: 1 post

        assert client.post("/posts", json={"title": "1", "content": "c", **extra}, headers=headers).status_code == 201
        resp = client.post("/posts", json={"title": "2", "content": "c"}, headers=headers)

        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE

    def test_response_keeps_every_existing_field(self, env):
        client, _ = env
        headers = _login(client)
        body = client.post("/posts", json={"title": "t", "content": "c"}, headers=headers).json()
        assert {"id", "title", "content", "author_id", "created_at", "image", "images"}.issubset(body)
        assert body["image"] is None
        assert body["images"] == []

    @pytest.mark.parametrize(
        "extra,status",
        [
            ({}, "published"),
            ({"publish_option": "save_draft"}, "draft"),
            ({"publish_option": "schedule", "scheduled_at": _future_iso()}, "scheduled"),
        ],
    )
    def test_image_upload_works_and_keeps_status(self, env, extra, status):
        client, _ = env
        headers = _login(client)
        post_id = client.post("/posts", json={"title": "t", "content": "c", **extra}, headers=headers).json()["id"]

        resp = client.post(
            f"/posts/{post_id}/image", files={"image": ("cover.png", PNG_1X1, "image/png")}, headers=headers
        )

        assert resp.status_code == 200
        assert resp.json()["image"].startswith("/media/")
        assert resp.json()["status"] == status
