"""
Scheduled publishing -- feature test suite, organised by the feature's
checklist (numbers match it):

  Creation              1-5
  Visibility            6-10
  Updating              11-15
  Automatic publishing  16-20
  Security              21-22

Everything goes through the real app (endpoints, visibility rule, the
scheduler job's run_once) against an isolated in-memory database. "Time
passing" is simulated by moving the clock the publishing service reads
(post_publishing._utc_now) forward -- never by writing past timestamps
through the API, which correctly refuses them. conftest.py keeps the
background loop off, so the job only runs when a test calls it.
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
from app.services import post_publishing, scheduled_publishing
from app.services.post_publishing import UNPUBLISH_NOT_ALLOWED_MESSAGE

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine)

    def override_get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    main_app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_app) as client:
        yield client, sessions
    main_app.dependency_overrides.clear()
    engine.dispose()


def _author(client: TestClient, username: str) -> dict:
    """A signed-in Pro user (Pro: no post limit, so tests can create freely)."""
    creds = {"username": username, "email": f"{username}@example.com", "password": "Password123"}
    assert client.post("/auth/register", json=creds).status_code == 201
    token = client.post("/auth/login", json={"username": username, "password": creds["password"]}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    plans = client.get("/subscriptions/plans").json()["plans"]
    pro = next(p["id"] for p in plans if p["slug"] == "pro")
    assert client.post("/subscriptions/subscribe", json={"plan_id": pro}, headers=headers).status_code == 201
    return headers


@pytest.fixture()
def author(app_env):
    client, sessions = app_env
    return client, sessions, _author(client, "author")


def _in(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(**delta)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _create(client, headers, title="A story", content="Story body", **publishing):
    return client.post("/posts", json={"title": title, "content": content, **publishing}, headers=headers)


def _create_scheduled(client, headers, when: datetime, title="Scheduled story", content="Story body") -> dict:
    resp = _create(client, headers, title, content, publish_option="schedule", scheduled_at=when.isoformat())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_draft(client, headers, title="Draft story", content="Story body") -> dict:
    resp = _create(client, headers, title, content, publish_option="save_draft")
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stored(sessions, post_id: int) -> models.Post:
    db = sessions()
    try:
        return db.get(models.Post, post_id)
    finally:
        db.close()


def _feed_ids(client, **params) -> list[int]:
    return [p["id"] for p in client.get("/posts", params={"limit": 100, **params}).json()["items"]]


def _set_clock(monkeypatch, when: datetime) -> None:
    """Move the publishing service's notion of "now" (visibility + job)."""
    monkeypatch.setattr(post_publishing, "_utc_now", lambda: when)


def _run_job(sessions) -> list[int]:
    return scheduled_publishing.run_once(sessions)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


class TestCreation:
    def test_01_create_post_with_publish_now(self, author):
        client, sessions, h = author
        before = datetime.now(timezone.utc) - timedelta(seconds=5)

        resp = _create(client, h, publish_option="publish_now")

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert before <= _utc(body["published_at"]) <= datetime.now(timezone.utc) + timedelta(seconds=5)
        stored = _stored(sessions, body["id"])
        assert (stored.status, stored.scheduled_at) == ("published", None)
        assert stored.published_at is not None

    def test_01b_default_body_still_publishes_now(self, author):
        client, _, h = author
        body = _create(client, h).json()
        assert body["status"] == "published"
        assert body["published_at"] is not None

    def test_02_create_post_as_draft(self, author):
        client, sessions, h = author

        resp = _create(client, h, publish_option="save_draft")

        assert resp.status_code == 201
        body = resp.json()
        assert (body["status"], body["scheduled_at"], body["published_at"]) == ("draft", None, None)
        stored = _stored(sessions, body["id"])
        assert (stored.status, stored.scheduled_at, stored.published_at) == ("draft", None, None)

    def test_03_create_post_with_future_scheduled_at(self, author):
        client, sessions, h = author
        when = _in(days=2)

        body = _create_scheduled(client, h, when)

        assert body["status"] == "scheduled"
        assert _utc(body["scheduled_at"]) == when
        assert body["published_at"] is None
        stored = _stored(sessions, body["id"])
        assert stored.status == "scheduled"
        assert stored.published_at is None

    @pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(hours=-5), timedelta(0)])
    def test_04_reject_past_scheduled_at(self, author, delta):
        client, sessions, h = author

        resp = _create(
            client, h, publish_option="schedule", scheduled_at=(datetime.now(timezone.utc) + delta).isoformat()
        )

        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "scheduled_at"]
        assert error["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_not_in_future"]
        db = sessions()
        assert db.query(models.Post).count() == 0
        db.close()

    @pytest.mark.parametrize("scheduled_at", ["__missing__", None])
    def test_05_reject_missing_scheduled_at(self, author, scheduled_at):
        client, sessions, h = author
        extra = {} if scheduled_at == "__missing__" else {"scheduled_at": scheduled_at}

        resp = _create(client, h, publish_option="schedule", **extra)

        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "scheduled_at"]
        assert error["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_required"]
        db = sessions()
        assert db.query(models.Post).count() == 0
        db.close()


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


class TestVisibility:
    def test_06_draft_not_publicly_visible(self, app_env):
        client, _ = app_env
        h = _author(client, "author")
        other = _author(client, "reader")
        draft = _create_draft(client, h)

        assert draft["id"] not in _feed_ids(client)
        assert client.get(f"/posts/{draft['id']}").status_code == 404
        assert client.get(f"/posts/{draft['id']}", headers=other).status_code == 404
        assert client.get(f"/posts/{draft['id']}/comments").status_code == 404
        assert client.get("/posts").json()["total"] == 0
        # ...while its author can still see it.
        assert client.get(f"/posts/{draft['id']}", headers=h).status_code == 200

    def test_07_scheduled_post_hidden_before_scheduled_time(self, author, monkeypatch):
        client, _, h = author
        when = _in(hours=6)
        post = _create_scheduled(client, h, when)

        for moment in (datetime.now(timezone.utc), when - timedelta(minutes=1), when - timedelta(seconds=1)):
            _set_clock(monkeypatch, moment)
            assert post["id"] not in _feed_ids(client)
            assert client.get(f"/posts/{post['id']}").status_code == 404

        _set_clock(monkeypatch, when)
        assert post["id"] in _feed_ids(client)
        assert client.get(f"/posts/{post['id']}").status_code == 200

    def test_08_published_post_publicly_visible(self, app_env):
        client, _ = app_env
        h = _author(client, "author")
        other = _author(client, "reader")
        post = _create(client, h, publish_option="publish_now").json()

        assert post["id"] in _feed_ids(client)
        for headers in (None, other, h):
            resp = client.get(f"/posts/{post['id']}", headers=headers)
            assert resp.status_code == 200
            assert resp.json()["status"] == "published"

    def test_09_search_does_not_expose_drafts(self, author):
        client, _, h = author
        _create_draft(client, h, title="Secret draft zebra", content="zebra notes")
        visible = _create(client, h, title="Public zebra", content="zebra facts").json()

        by_word = client.get("/posts", params={"search": "zebra"}).json()
        by_title = client.get("/posts", params={"search": "Secret draft"}).json()

        assert [p["id"] for p in by_word["items"]] == [visible["id"]]
        assert by_word["total"] == 1
        assert by_title["items"] == [] and by_title["total"] == 0

    def test_10_search_does_not_expose_future_scheduled_posts(self, author, monkeypatch):
        client, _, h = author
        when = _in(days=1)
        scheduled = _create_scheduled(client, h, when, title="Upcoming giraffe", content="giraffe launch")
        visible = _create(client, h, title="Current giraffe", content="giraffe now").json()

        body = client.get("/posts", params={"search": "giraffe"}).json()
        assert [p["id"] for p in body["items"]] == [visible["id"]]
        assert body["total"] == 1
        assert client.get("/posts", params={"search": "Upcoming"}).json()["total"] == 0

        _set_clock(monkeypatch, when + timedelta(seconds=1))
        assert scheduled["id"] in _feed_ids(client, search="Upcoming giraffe")


# ---------------------------------------------------------------------------
# Updating
# ---------------------------------------------------------------------------


class TestUpdating:
    def test_11_draft_to_scheduled(self, author):
        client, sessions, h = author
        draft = _create_draft(client, h)
        when = _in(days=1)

        resp = client.put(
            f"/posts/{draft['id']}", json={"publish_option": "schedule", "scheduled_at": when.isoformat()}, headers=h
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "scheduled"
        assert _utc(body["scheduled_at"]) == when
        assert body["published_at"] is None
        assert _stored(sessions, draft["id"]).status == "scheduled"
        assert draft["id"] not in _feed_ids(client)

    def test_12_draft_to_published(self, author):
        client, sessions, h = author
        draft = _create_draft(client, h)

        resp = client.put(f"/posts/{draft['id']}", json={"publish_option": "publish_now"}, headers=h)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert body["published_at"] is not None
        assert _stored(sessions, draft["id"]).published_at is not None
        assert draft["id"] in _feed_ids(client)

    def test_13_scheduled_to_draft(self, author):
        client, sessions, h = author
        post = _create_scheduled(client, h, _in(days=1))

        resp = client.put(f"/posts/{post['id']}", json={"publish_option": "save_draft"}, headers=h)

        assert resp.status_code == 200
        body = resp.json()
        assert (body["status"], body["scheduled_at"], body["published_at"]) == ("draft", None, None)
        stored = _stored(sessions, post["id"])
        assert (stored.status, stored.scheduled_at, stored.published_at) == ("draft", None, None)

    def test_14_scheduled_to_published(self, author):
        client, sessions, h = author
        post = _create_scheduled(client, h, _in(days=1))

        resp = client.put(f"/posts/{post['id']}", json={"publish_option": "publish_now"}, headers=h)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert body["published_at"] is not None
        assert post["id"] in _feed_ids(client)
        # Nothing left for the job to do with it.
        assert _run_job(sessions) == []

    @pytest.mark.parametrize("new_delta", [timedelta(hours=2), timedelta(days=10)])  # earlier / later
    def test_15_reschedule_existing_scheduled_post(self, author, new_delta):
        client, sessions, h = author
        post = _create_scheduled(client, h, _in(days=1))
        new_time = _in() + new_delta

        resp = client.put(
            f"/posts/{post['id']}",
            json={"publish_option": "schedule", "scheduled_at": new_time.isoformat()},
            headers=h,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "scheduled"
        assert _utc(body["scheduled_at"]) == new_time
        assert body["published_at"] is None
        assert _stored(sessions, post["id"]).status == "scheduled"

    def test_15b_reschedule_into_the_past_rejected_and_unchanged(self, author):
        client, sessions, h = author
        post = _create_scheduled(client, h, _in(days=1))

        resp = client.put(
            f"/posts/{post['id']}",
            json={"publish_option": "schedule", "scheduled_at": _in(minutes=-5).isoformat()},
            headers=h,
        )

        assert resp.status_code == 422
        assert resp.json()["detail"][0]["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_not_in_future"]
        assert _utc(client.get(f"/posts/{post['id']}", headers=h).json()["scheduled_at"]) == _utc(post["scheduled_at"])


# ---------------------------------------------------------------------------
# Automatic publishing
# ---------------------------------------------------------------------------


class TestAutomaticPublishing:
    def test_16_scheduled_post_published_when_time_reached(self, author, monkeypatch):
        client, sessions, h = author
        when = _in(hours=3)
        post = _create_scheduled(client, h, when)

        _set_clock(monkeypatch, when)  # the scheduled moment arrives
        assert _run_job(sessions) == [post["id"]]

        stored = _stored(sessions, post["id"])
        assert stored.status == "published"
        assert stored.published_at is not None
        assert _utc(client.get(f"/posts/{post['id']}").json()["published_at"]) == when
        # scheduled_at is kept as the record of the original schedule.
        assert _utc(client.get(f"/posts/{post['id']}").json()["scheduled_at"]) == when
        assert post["id"] in _feed_ids(client)

    def test_17_future_scheduled_post_remains_scheduled(self, author, monkeypatch):
        client, sessions, h = author
        when = _in(hours=3)
        post = _create_scheduled(client, h, when)

        for moment in (datetime.now(timezone.utc), when - timedelta(seconds=1)):
            _set_clock(monkeypatch, moment)
            assert _run_job(sessions) == []

        stored = _stored(sessions, post["id"])
        assert (stored.status, stored.published_at) == ("scheduled", None)
        assert post["id"] not in _feed_ids(client)

    def test_18_draft_remains_draft(self, author, monkeypatch):
        client, sessions, h = author
        draft = _create_draft(client, h)
        # A draft that used to be scheduled, too: the old time mustn't matter.
        was_scheduled = _create_scheduled(client, h, _in(minutes=30), title="Was scheduled")
        client.put(f"/posts/{was_scheduled['id']}", json={"publish_option": "save_draft"}, headers=h)

        _set_clock(monkeypatch, _in(days=365))
        assert _run_job(sessions) == []

        for pid in (draft["id"], was_scheduled["id"]):
            stored = _stored(sessions, pid)
            assert (stored.status, stored.published_at) == ("draft", None)

    def test_19_already_published_post_not_processed_again(self, author, monkeypatch):
        client, sessions, h = author
        published = _create(client, h, title="Always live").json()
        when = _in(hours=1)
        scheduled = _create_scheduled(client, h, when)

        _set_clock(monkeypatch, when)
        assert _run_job(sessions) == [scheduled["id"]]
        first_published_at = _stored(sessions, scheduled["id"]).published_at
        original_published_at = _stored(sessions, published["id"]).published_at

        # Later passes -- including after a "restart" -- find nothing to do
        # and don't move either post's published_at.
        for later in (when + timedelta(minutes=1), when + timedelta(days=1)):
            _set_clock(monkeypatch, later)
            assert _run_job(sessions) == []
        assert _stored(sessions, scheduled["id"]).published_at == first_published_at
        assert _stored(sessions, published["id"]).published_at == original_published_at

    def test_20_multiple_scheduled_posts_published_correctly(self, author, monkeypatch):
        client, sessions, h = author
        base = _in(hours=1)
        same_time = [_create_scheduled(client, h, base, title=f"Same {i}")["id"] for i in range(3)]
        later = _create_scheduled(client, h, base + timedelta(hours=1), title="Later")["id"]
        much_later = _create_scheduled(client, h, base + timedelta(days=5), title="Much later")["id"]
        draft = _create_draft(client, h)["id"]

        _set_clock(monkeypatch, base)
        assert _run_job(sessions) == sorted(same_time)

        _set_clock(monkeypatch, base + timedelta(hours=2))
        assert _run_job(sessions) == [later]

        statuses = {pid: _stored(sessions, pid).status for pid in same_time + [later, much_later, draft]}
        assert statuses == {
            **{pid: "published" for pid in same_time},
            later: "published",
            much_later: "scheduled",
            draft: "draft",
        }
        assert sorted(_feed_ids(client)) == sorted(same_time + [later])
        assert client.get("/posts").json()["total"] == 4


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestSecurity:
    @pytest.mark.parametrize(
        "body",
        [
            {"title": "Hijacked"},
            {"publish_option": "publish_now"},
            {"publish_option": "save_draft"},
            {"publish_option": "schedule", "scheduled_at": _in(days=30).isoformat()},
        ],
    )
    def test_21_author_cannot_modify_another_authors_scheduled_post(self, app_env, body, monkeypatch):
        client, sessions = app_env
        owner = _author(client, "owner")
        intruder = _author(client, "intruder")
        when = _in(days=1)
        post = _create_scheduled(client, owner, when, title="Owner's story")

        resp = client.put(f"/posts/{post['id']}", json=body, headers=intruder)

        assert resp.status_code == 403
        assert resp.json() == {"detail": "Not authorized to modify this post"}
        stored = _stored(sessions, post["id"])
        assert (stored.title, stored.status, stored.published_at) == ("Owner's story", "scheduled", None)
        # ...and it still goes live at the owner's time.
        _set_clock(monkeypatch, when)
        assert _run_job(sessions) == [post["id"]]

    def test_21b_other_author_cannot_delete_upload_to_or_see_it(self, app_env):
        client, sessions = app_env
        owner = _author(client, "owner")
        intruder = _author(client, "intruder")
        post = _create_scheduled(client, owner, _in(days=1))

        assert client.delete(f"/posts/{post['id']}", headers=intruder).status_code == 403
        upload = client.post(
            f"/posts/{post['id']}/image", files={"image": ("c.png", PNG_1X1, "image/png")}, headers=intruder
        )
        assert upload.status_code == 403
        assert client.get(f"/posts/{post['id']}", headers=intruder).status_code == 404
        assert client.get("/posts/mine", headers=intruder).json() == []
        assert client.post(f"/posts/{post['id']}/like", headers=intruder).status_code == 404
        assert _stored(sessions, post["id"]) is not None

    def test_21c_owner_cannot_unpublish_after_job_published(self, author, monkeypatch):
        client, sessions, h = author
        when = _in(hours=1)
        post = _create_scheduled(client, h, when)
        _set_clock(monkeypatch, when)
        _run_job(sessions)

        resp = client.put(f"/posts/{post['id']}", json={"publish_option": "save_draft"}, headers=h)

        assert resp.status_code == 409
        assert resp.json() == {"detail": UNPUBLISH_NOT_ALLOWED_MESSAGE}

    def test_22_unauthenticated_users_cannot_use_author_functionality(self, author):
        client, _, h = author
        draft = _create_draft(client, h)
        scheduled = _create_scheduled(client, h, _in(days=1))
        future = _in(days=2).isoformat()

        protected = [
            ("POST", "/posts", {"json": {"title": "t", "content": "c", "publish_option": "save_draft"}}),
            ("POST", "/posts", {"json": {"title": "t", "content": "c", "publish_option": "schedule", "scheduled_at": future}}),
            ("GET", "/posts/mine", {}),
            ("PUT", f"/posts/{draft['id']}", {"json": {"publish_option": "publish_now"}}),
            ("PUT", f"/posts/{scheduled['id']}", {"json": {"publish_option": "schedule", "scheduled_at": future}}),
            ("PUT", f"/posts/{scheduled['id']}", {"json": {"publish_option": "save_draft"}}),
            ("DELETE", f"/posts/{draft['id']}", {}),
            ("POST", f"/posts/{scheduled['id']}/image", {"files": {"image": ("c.png", PNG_1X1, "image/png")}}),
        ]
        for token in (None, "not-a-real-token"):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            for method, url, kwargs in protected:
                resp = client.request(method, url, headers=headers, **kwargs)
                assert resp.status_code == 401, (token, method, url, resp.status_code)

        # Author-only previews of unpublished posts aren't available anonymously either.
        for pid in (draft["id"], scheduled["id"]):
            assert client.get(f"/posts/{pid}").status_code == 404
            assert client.get(f"/posts/{pid}", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 404

        # Nothing changed.
        mine = {p["id"]: p["status"] for p in client.get("/posts/mine", headers=h).json()}
        assert mine == {draft["id"]: "draft", scheduled["id"]: "scheduled"}
