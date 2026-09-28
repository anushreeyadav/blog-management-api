"""
Scheduled publishing validation review: the 15 cases, one test class each
(numbered to match), through the real endpoints / job with an in-memory
database. Several are also covered in more depth by the other
test_post_publishing_* / test_scheduled_publishing files; this file is the
single place that walks the whole list.
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
from app.schemas import SCHEDULED_AT_ERRORS as MSG
from app.services import post_publishing, scheduled_publishing

IST = timezone(timedelta(hours=5, minutes=30))
NEW_YORK_WINTER = timezone(timedelta(hours=-5))


@pytest.fixture()
def env():
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
        client.post("/auth/register", json={"username": "w", "email": "w@example.com", "password": "Password123"})
        token = client.post("/auth/login", json={"username": "w", "password": "Password123"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        pro = next(p["id"] for p in client.get("/subscriptions/plans").json()["plans"] if p["slug"] == "pro")
        assert client.post("/subscriptions/subscribe", json={"plan_id": pro}, headers=headers).status_code == 201
        yield client, sessions, headers
    main_app.dependency_overrides.clear()
    engine.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(delta: timedelta, tz=timezone.utc) -> str:
    return (_now() + delta).astimezone(tz).isoformat()


def _create(client, headers, **extra):
    return client.post("/posts", json={"title": "t", "content": "c", **extra}, headers=headers)


def _single_error(resp) -> tuple[list, str, str]:
    assert resp.status_code == 422, resp.text
    [error] = resp.json()["detail"]
    return error["loc"], error["msg"], error["type"]


def _count(sessions) -> int:
    db = sessions()
    try:
        return db.query(models.Post).count()
    finally:
        db.close()


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Test01MissingScheduledAt:
    def test_create(self, env):
        client, sessions, h = env
        loc, msg, typ = _single_error(_create(client, h, publish_option="schedule"))
        assert (loc, msg, typ) == (["body", "scheduled_at"], MSG["scheduled_at_required"], "scheduled_at_required")
        assert _count(sessions) == 0

    def test_explicit_null(self, env):
        client, _, h = env
        assert _single_error(_create(client, h, publish_option="schedule", scheduled_at=None))[1] == MSG[
            "scheduled_at_required"
        ]


class Test02PastScheduledAt:
    @pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(hours=-3), timedelta(days=-400)])
    def test_rejected(self, env, delta):
        client, sessions, h = env
        loc, msg, typ = _single_error(_create(client, h, publish_option="schedule", scheduled_at=_iso(delta)))
        assert (loc, msg, typ) == (["body", "scheduled_at"], MSG["scheduled_at_not_in_future"], "scheduled_at_not_in_future")
        assert _count(sessions) == 0


class Test03ScheduledAtEqualToNow:
    def test_now_is_not_the_future(self, env):
        client, _, h = env
        # By the time the request is validated, "now" from the client is at
        # best equal to, and in practice just before, the server's now.
        resp = _create(client, h, publish_option="schedule", scheduled_at=_now().isoformat())
        assert _single_error(resp)[1] == MSG["scheduled_at_not_in_future"]


class Test04FutureScheduledAt:
    @pytest.mark.parametrize("delta", [timedelta(minutes=2), timedelta(days=1), timedelta(days=365 * 3)])
    def test_accepted_as_scheduled(self, env, delta):
        client, _, h = env
        when = (_now() + delta).replace(microsecond=0)
        resp = _create(client, h, publish_option="schedule", scheduled_at=when.isoformat())
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "scheduled"
        assert _utc(body["scheduled_at"]) == when
        assert body["published_at"] is None


class Test05DraftWithScheduledAt:
    def test_create_rejected(self, env):
        client, sessions, h = env
        resp = _create(client, h, publish_option="save_draft", scheduled_at=_iso(timedelta(days=1)))
        assert _single_error(resp) == (["body", "scheduled_at"], MSG["scheduled_at_not_allowed"], "scheduled_at_not_allowed")
        assert _count(sessions) == 0

    def test_update_rejected(self, env):
        client, _, h = env
        pid = _create(client, h, publish_option="save_draft").json()["id"]
        resp = client.put(f"/posts/{pid}", json={"publish_option": "save_draft", "scheduled_at": _iso(timedelta(days=1))}, headers=h)
        assert _single_error(resp)[1] == MSG["scheduled_at_not_allowed"]


class Test06PublishNowWithScheduledAt:
    @pytest.mark.parametrize("extra", [{"publish_option": "publish_now"}, {}])  # {} = default publish_now
    def test_rejected(self, env, extra):
        client, sessions, h = env
        resp = _create(client, h, scheduled_at=_iso(timedelta(days=1)), **extra)
        assert _single_error(resp)[1] == MSG["scheduled_at_not_allowed"]
        assert _count(sessions) == 0


class Test07ScheduleWithPublishedAt:
    def test_create_rejected(self, env):
        client, sessions, h = env
        resp = _create(
            client, h, publish_option="schedule", scheduled_at=_iso(timedelta(days=1)), published_at=_now().isoformat()
        )
        assert _single_error(resp) == (["body", "published_at"], MSG["published_at_read_only"], "published_at_read_only")
        assert _count(sessions) == 0

    def test_update_rejected_and_post_unchanged(self, env):
        client, sessions, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(days=1))).json()
        resp = client.put(f"/posts/{post['id']}", json={"title": "x", "published_at": _now().isoformat()}, headers=h)
        assert _single_error(resp)[1] == MSG["published_at_read_only"]
        db = sessions()
        stored = db.get(models.Post, post["id"])
        assert (stored.title, stored.status, stored.published_at) == ("t", "scheduled", None)
        db.close()

    def test_status_field_rejected_too(self, env):
        client, _, h = env
        resp = _create(client, h, status="scheduled")
        assert _single_error(resp) == (["body", "status"], MSG["status_read_only"], "status_read_only")


class Test08InvalidDatetimeFormat:
    @pytest.mark.parametrize(
        "value",
        ["next tuesday", "", "2026-13-01T10:00:00Z", "2026-02-30T10:00:00Z", "01/10/2026 09:00", "2026-10-01T25:00:00Z",
         [2026, 10, 1], {"date": "2026-10-01"}, True],
    )
    def test_friendly_format_message(self, env, value):
        client, sessions, h = env
        resp = _create(client, h, publish_option="schedule", scheduled_at=value)
        assert _single_error(resp) == (
            ["body", "scheduled_at"], MSG["scheduled_at_invalid_format"], "scheduled_at_invalid_format"
        )
        assert _count(sessions) == 0

    @pytest.mark.parametrize("value", ["2026-10-01", "2099-10-01T09:00:00"])
    def test_date_only_or_no_timezone_asks_for_full_datetime(self, env, value):
        client, _, h = env
        assert _single_error(_create(client, h, publish_option="schedule", scheduled_at=value))[1] == MSG[
            "scheduled_at_missing_timezone"
        ]


class Test09DifferentTimezones:
    @pytest.mark.parametrize("tz", [timezone.utc, IST, NEW_YORK_WINTER, timezone(timedelta(hours=14))])
    def test_same_instant_stored_and_returned_in_utc(self, env, tz):
        client, sessions, h = env
        instant = (_now() + timedelta(days=1)).replace(microsecond=0)
        resp = _create(client, h, publish_option="schedule", scheduled_at=instant.astimezone(tz).isoformat())
        assert resp.status_code == 201
        returned = resp.json()["scheduled_at"]
        assert returned.endswith("Z")  # always explicit UTC, never a bare local time
        assert _utc(returned) == instant

    def test_z_suffix_accepted(self, env):
        client, _, h = env
        when = (_now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _create(client, h, publish_option="schedule", scheduled_at=when).status_code == 201

    def test_future_check_uses_the_instant_not_the_wall_clock(self, env):
        client, _, h = env
        # 30 minutes in the past, but written in a zone whose wall clock reads
        # hours "later" -- must still be rejected.
        past_in_plus14 = (_now() - timedelta(minutes=30)).astimezone(timezone(timedelta(hours=14))).isoformat()
        assert _single_error(_create(client, h, publish_option="schedule", scheduled_at=past_in_plus14))[1] == MSG[
            "scheduled_at_not_in_future"
        ]
        # ...and a future instant written in a zone whose wall clock reads
        # "earlier" than UTC now must be accepted.
        future_in_minus5 = (_now() + timedelta(hours=1)).astimezone(NEW_YORK_WINTER).isoformat()
        assert _create(client, h, publish_option="schedule", scheduled_at=future_in_minus5).status_code == 201

    def test_service_rejects_naive_now_and_normalizes_offsets(self, env):
        _, sessions, _ = env
        db = sessions()
        with pytest.raises(ValueError):
            post_publishing.publish_due_posts(db, now=datetime.now())
        with pytest.raises(ValueError):
            post_publishing.publicly_visible_clause(datetime.now())
        db.close()


class Test10UpdatingAScheduledPost:
    def test_text_edit_keeps_schedule(self, env):
        client, _, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(days=2))).json()
        body = client.put(f"/posts/{post['id']}", json={"title": "Edited", "content": "New"}, headers=h).json()
        assert (body["title"], body["status"], body["scheduled_at"]) == ("Edited", "scheduled", post["scheduled_at"])

    def test_resending_a_now_past_time_is_rejected(self, env):
        client, sessions, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(days=2))).json()
        resp = client.put(
            f"/posts/{post['id']}",
            json={"title": "x", "publish_option": "schedule", "scheduled_at": _iso(timedelta(minutes=-1))},
            headers=h,
        )
        assert _single_error(resp)[1] == MSG["scheduled_at_not_in_future"]
        db = sessions()
        assert db.get(models.Post, post["id"]).title == "t"
        db.close()


class Test11Rescheduling:
    @pytest.mark.parametrize("delta", [timedelta(hours=1), timedelta(days=30)])  # earlier and later than before
    def test_new_future_time(self, env, delta):
        client, _, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(days=2))).json()
        new_time = (_now() + delta).replace(microsecond=0)
        body = client.put(
            f"/posts/{post['id']}", json={"publish_option": "schedule", "scheduled_at": new_time.astimezone(IST).isoformat()}, headers=h
        ).json()
        assert body["status"] == "scheduled"
        assert _utc(body["scheduled_at"]) == new_time
        assert body["published_at"] is None

    def test_reschedule_without_time_rejected(self, env):
        client, _, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(days=2))).json()
        resp = client.put(f"/posts/{post['id']}", json={"publish_option": "schedule"}, headers=h)
        assert _single_error(resp)[1] == MSG["scheduled_at_required"]


class Test12CancelToDraft:
    def test_scheduled_to_draft_clears_schedule_and_job_ignores_it(self, env):
        client, sessions, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(minutes=5))).json()
        body = client.put(f"/posts/{post['id']}", json={"publish_option": "save_draft"}, headers=h).json()
        assert (body["status"], body["scheduled_at"], body["published_at"]) == ("draft", None, None)
        # Even well past the original time, the job leaves the draft alone.
        db = sessions()
        assert post_publishing.publish_due_posts(db, now=_now() + timedelta(days=1)) == []
        db.close()
        assert client.get(f"/posts/{post['id']}").status_code == 404


class Test13ManyPostsSameTime:
    def test_all_published_in_one_pass_exactly_once(self, env):
        client, sessions, h = env
        when = (_now() + timedelta(hours=1)).replace(microsecond=0)
        ids = [
            _create(client, h, publish_option="schedule", scheduled_at=when.isoformat()).json()["id"] for _ in range(5)
        ]
        future = _create(client, h, publish_option="schedule", scheduled_at=(when + timedelta(seconds=1)).isoformat()).json()["id"]

        db = sessions()
        assert post_publishing.publish_due_posts(db, now=when) == sorted(ids)  # boundary: == scheduled_at
        db.commit()
        assert post_publishing.publish_due_posts(db, now=when) == []
        db.commit()
        statuses = {p.id: p.status for p in db.query(models.Post)}
        published_at = {p.published_at for p in db.query(models.Post).filter(models.Post.id.in_(ids))}
        db.close()
        assert all(statuses[i] == "published" for i in ids)
        assert statuses[future] == "scheduled"
        assert len(published_at) == 1  # one pass, one timestamp


class Test14SchedulerRestart:
    def test_each_new_process_catches_up_without_republishing(self, env):
        client, sessions, _ = env
        db = sessions()
        author = db.query(models.User).first()
        for minutes in (10, 60, 60 * 24):
            db.add(models.Post(title="t", content="c", author_id=author.id, status="scheduled",
                               scheduled_at=_now() - timedelta(minutes=minutes)))
        db.commit()
        db.close()

        first = scheduled_publishing.run_once(sessions)       # "process 1"
        assert len(first) == 3
        assert scheduled_publishing.run_once(sessions) == []  # "process 2" after a restart
        assert scheduled_publishing.run_once(sessions) == []


class Test15AlreadyPublishedPost:
    def test_job_never_touches_published_posts(self, env):
        client, sessions, h = env
        published = _create(client, h).json()
        # Scheduled then published early by the author, with its schedule
        # time now past: must not be "published again".
        early = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(minutes=10))).json()
        client.put(f"/posts/{early['id']}", json={"publish_option": "publish_now"}, headers=h)
        before = {p["id"]: p["published_at"] for p in client.get("/posts/mine", headers=h).json()}

        db = sessions()
        assert post_publishing.publish_due_posts(db, now=_now() + timedelta(days=1)) == []
        db.commit()
        db.close()

        after = {p["id"]: p["published_at"] for p in client.get("/posts/mine", headers=h).json()}
        assert after == before
        assert after[published["id"]] is not None

    def test_edit_after_job_published_gets_clear_409(self, env):
        client, sessions, h = env
        post = _create(client, h, publish_option="schedule", scheduled_at=_iso(timedelta(minutes=10))).json()
        db = sessions()
        post_publishing.publish_due_posts(db, now=_now() + timedelta(hours=1))
        db.commit()
        db.close()
        # The author's editor still thought it was scheduled.
        resp = client.put(f"/posts/{post['id']}", json={"publish_option": "save_draft"}, headers=h)
        assert resp.status_code == 409
        assert resp.json()["detail"] == post_publishing.UNPUBLISH_NOT_ALLOWED_MESSAGE
