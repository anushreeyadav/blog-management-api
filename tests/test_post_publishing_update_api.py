"""
PUT /posts/{id} publishing transitions (app/services/post_publishing.py),
exercised through the real endpoint with the real seeded plans -- same
in-memory database setup as tests/test_post_publishing_create_api.py.
Covers every supported transition, the rejected ones (published -> draft /
scheduled: the app has no unpublish), invalid scheduling values, and that
the existing edit behavior (ownership, auth, partial edits, image upload)
is unchanged.
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
from app.services.post_publishing import UNPUBLISH_NOT_ALLOWED_MESSAGE

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


def _login(client: TestClient, username: str = "writer") -> dict:
    creds = {"username": username, "email": f"{username}@example.com", "password": "Password123"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json={"username": username, "password": creds["password"]}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    plans = client.get("/subscriptions/plans").json()["plans"]
    pro = next(p["id"] for p in plans if p["slug"] == "pro")
    assert client.post("/subscriptions/subscribe", json={"plan_id": pro}, headers=headers).status_code == 201
    return headers


def _future(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(**(delta or {"days": 2}))


def _parse(ts: str) -> datetime:
    value = datetime.fromisoformat(ts)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _create(client, headers, state: str, title: str = "Original") -> dict:
    body = {"title": title, "content": "Original body"}
    if state == "draft":
        body["publish_option"] = "save_draft"
    elif state == "scheduled":
        body.update(publish_option="schedule", scheduled_at=_future(days=5).isoformat())
    resp = client.post("/posts", json=body, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == state
    return resp.json()


def _db_post(sessions, post_id: int) -> models.Post:
    db = sessions()
    try:
        return db.query(models.Post).filter(models.Post.id == post_id).one()
    finally:
        db.close()


def _put(client, headers, post_id, **body):
    return client.put(f"/posts/{post_id}", json=body, headers=headers)


# ---------------------------------------------------------------------------
# Supported transitions
# ---------------------------------------------------------------------------


class TestDraftTransitions:
    def test_draft_to_published(self, env):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "draft")
        before = datetime.now(timezone.utc) - timedelta(seconds=5)

        resp = _put(client, headers, post["id"], publish_option="publish_now")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert before <= _parse(body["published_at"]) <= datetime.now(timezone.utc) + timedelta(seconds=5)
        stored = _db_post(sessions, post["id"])
        assert (stored.status, stored.scheduled_at) == ("published", None)
        assert stored.published_at is not None

    def test_draft_to_scheduled(self, env):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "draft")
        when = _future(days=1)

        resp = _put(client, headers, post["id"], publish_option="schedule", scheduled_at=when.isoformat())

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "scheduled"
        assert _parse(body["scheduled_at"]) == when
        assert body["published_at"] is None
        assert _db_post(sessions, post["id"]).status == "scheduled"

    def test_draft_saved_as_draft_again_with_edits(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        resp = _put(client, headers, post["id"], title="Better title", publish_option="save_draft")

        assert resp.status_code == 200
        assert resp.json()["title"] == "Better title"
        assert resp.json()["status"] == "draft"
        assert resp.json()["published_at"] is None


class TestScheduledTransitions:
    def test_scheduled_to_published(self, env):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "scheduled")

        resp = _put(client, headers, post["id"], publish_option="publish_now")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert body["published_at"] is not None
        stored = _db_post(sessions, post["id"])
        assert stored.scheduled_at is None and stored.published_at is not None

    def test_scheduled_to_draft(self, env):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "scheduled")

        resp = _put(client, headers, post["id"], publish_option="save_draft")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "draft"
        assert body["scheduled_at"] is None
        assert body["published_at"] is None
        stored = _db_post(sessions, post["id"])
        assert (stored.status, stored.scheduled_at, stored.published_at) == ("draft", None, None)

    def test_scheduled_rescheduled_to_a_new_time(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "scheduled")
        new_time = _future(hours=3)

        resp = _put(client, headers, post["id"], publish_option="schedule", scheduled_at=new_time.isoformat())

        assert resp.status_code == 200
        assert resp.json()["status"] == "scheduled"
        assert _parse(resp.json()["scheduled_at"]) == new_time
        assert _parse(resp.json()["scheduled_at"]) != _parse(post["scheduled_at"])


class TestPublishedTransitions:
    @pytest.mark.parametrize(
        "body",
        [
            {"publish_option": "save_draft"},
            {"publish_option": "schedule", "scheduled_at": _future().isoformat()},
        ],
    )
    def test_published_cannot_be_unpublished(self, env, body):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "published")

        resp = _put(client, headers, post["id"], title="Should not apply", **body)

        assert resp.status_code == 409
        assert resp.json() == {"detail": UNPUBLISH_NOT_ALLOWED_MESSAGE}
        stored = _db_post(sessions, post["id"])
        assert stored.status == "published"
        assert stored.title == "Original"  # nothing half-applied
        assert stored.published_at is not None
        assert stored.scheduled_at is None

    def test_publish_now_on_published_post_is_a_no_op_keeping_published_at(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "published")

        resp = _put(client, headers, post["id"], title="Edited", publish_option="publish_now")

        assert resp.status_code == 200
        assert resp.json()["title"] == "Edited"
        assert resp.json()["status"] == "published"
        assert resp.json()["published_at"] == post["published_at"]


# ---------------------------------------------------------------------------
# Edits without publish_option leave the publishing state alone
# ---------------------------------------------------------------------------


class TestStatusUnchangedWithoutPublishOption:
    @pytest.mark.parametrize("state", ["draft", "scheduled", "published"])
    def test_title_content_edit_keeps_state(self, env, state):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, state)

        resp = _put(client, headers, post["id"], title="New title", content="New body")

        assert resp.status_code == 200
        body = resp.json()
        assert (body["title"], body["content"]) == ("New title", "New body")
        assert body["status"] == state
        assert body["scheduled_at"] == post["scheduled_at"]
        assert body["published_at"] == post["published_at"]

    def test_partial_edit_still_partial(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        body = _put(client, headers, post["id"], content="Only content").json()

        assert body["title"] == "Original"
        assert body["content"] == "Only content"

    def test_empty_body_changes_nothing(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "scheduled")

        body = _put(client, headers, post["id"]).json()

        assert {k: body[k] for k in ("title", "content", "status", "scheduled_at")} == {
            k: post[k] for k in ("title", "content", "status", "scheduled_at")
        }


# ---------------------------------------------------------------------------
# Invalid scheduling values
# ---------------------------------------------------------------------------


class TestInvalidScheduling:
    @pytest.mark.parametrize(
        "body,message",
        [
            ({"publish_option": "schedule"}, SCHEDULED_AT_ERRORS["scheduled_at_required"]),
            (
                {"publish_option": "schedule", "scheduled_at": "2020-01-01T00:00:00Z"},
                SCHEDULED_AT_ERRORS["scheduled_at_not_in_future"],
            ),
            (
                {"publish_option": "schedule", "scheduled_at": (_future().replace(tzinfo=None)).isoformat()},
                SCHEDULED_AT_ERRORS["scheduled_at_missing_timezone"],
            ),
            ({"scheduled_at": _future().isoformat()}, SCHEDULED_AT_ERRORS["scheduled_at_not_allowed"]),
            (
                {"publish_option": "publish_now", "scheduled_at": _future().isoformat()},
                SCHEDULED_AT_ERRORS["scheduled_at_not_allowed"],
            ),
            (
                {"publish_option": "save_draft", "scheduled_at": _future().isoformat()},
                SCHEDULED_AT_ERRORS["scheduled_at_not_allowed"],
            ),
        ],
    )
    def test_rejected_with_clear_422_and_post_unchanged(self, env, body, message):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        resp = _put(client, headers, post["id"], title="Should not apply", **body)

        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "scheduled_at"]
        assert error["msg"] == message
        stored = _db_post(sessions, post["id"])
        assert (stored.title, stored.status, stored.scheduled_at) == ("Original", "draft", None)

    def test_unknown_publish_option(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        resp = _put(client, headers, post["id"], publish_option="unpublish")

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["body", "publish_option"]


# ---------------------------------------------------------------------------
# Ownership, auth and other existing behavior
# ---------------------------------------------------------------------------


class TestOwnershipAndAuthUnchanged:
    @pytest.mark.parametrize(
        "state,body",
        [
            ("draft", {"publish_option": "publish_now"}),
            ("scheduled", {"publish_option": "save_draft"}),
            ("draft", {"publish_option": "schedule", "scheduled_at": _future().isoformat()}),
            ("published", {"title": "Hijacked"}),
        ],
    )
    def test_other_author_gets_403_and_post_unchanged(self, env, state, body):
        client, sessions = env
        owner = _login(client, "owner")
        intruder = _login(client, "intruder")
        post = _create(client, owner, state)

        resp = _put(client, intruder, post["id"], **body)

        assert resp.status_code == 403
        assert resp.json() == {"detail": "Not authorized to modify this post"}
        stored = _db_post(sessions, post["id"])
        assert (stored.title, stored.status) == ("Original", state)

    def test_other_author_gets_403_not_409_on_published_post(self, env):
        # Ownership is checked before the transition, so a non-owner learns
        # nothing about which transitions the post would allow.
        client, _ = env
        owner = _login(client, "owner")
        intruder = _login(client, "intruder")
        post = _create(client, owner, "published")

        assert _put(client, intruder, post["id"], publish_option="save_draft").status_code == 403

    def test_unauthenticated_gets_401(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        resp = client.put(f"/posts/{post['id']}", json={"publish_option": "publish_now"})

        assert resp.status_code == 401

    def test_missing_post_gets_404(self, env):
        client, _ = env
        headers = _login(client)

        assert _put(client, headers, 999999, publish_option="publish_now").status_code == 404

    def test_blank_title_still_rejected(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        assert _put(client, headers, post["id"], title="   ", publish_option="publish_now").status_code == 422

    def test_image_upload_after_transition_keeps_new_state(self, env):
        client, _ = env
        headers = _login(client)
        post = _create(client, headers, "draft")
        _put(client, headers, post["id"], publish_option="schedule", scheduled_at=_future().isoformat())

        resp = client.post(
            f"/posts/{post['id']}/image", files={"image": ("c.png", PNG_1X1, "image/png")}, headers=headers
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "scheduled"
        assert resp.json()["image"].startswith("/media/")

    def test_edit_does_not_consume_post_quota(self, env):
        client, sessions = env
        headers = _login(client)
        post = _create(client, headers, "draft")

        _put(client, headers, post["id"], publish_option="publish_now")

        db = sessions()
        assert db.query(models.Post).count() == 1
        db.close()
