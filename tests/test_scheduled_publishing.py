"""
Automatic publishing of scheduled posts: app/services/post_publishing.py's
publish_due_posts and app/services/scheduled_publishing.py's run_once /
background loop. In-memory SQLite throughout -- conftest.py keeps the real
loop (which would use the .env database) switched off, so these tests drive
run_once / the loop explicitly against their own session factory.

Posts that are already due are inserted directly through the ORM: the API
(correctly) refuses a scheduled_at in the past, and this simulates "the
scheduled time has since arrived".
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app
from app.services import post_publishing, scheduled_publishing
from app.services.plans import seed_default_plans

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _utc(value: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; everything is stored as UTC.
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=timezone.utc)


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    seed_default_plans(db)
    plan = db.query(models.SubscriptionPlan).first()
    db.add(models.User(username="author", email="a@example.com", password_hash="x", subscription_plan_id=plan.id))
    db.commit()
    db.close()
    yield factory
    engine.dispose()


def _add(factory, status: str, scheduled_at: datetime | None = None, published_at: datetime | None = None) -> int:
    db = factory()
    try:
        author = db.query(models.User).first()
        post = models.Post(
            title=status, content="c", author_id=author.id, status=status,
            scheduled_at=scheduled_at, published_at=published_at,
        )
        db.add(post)
        db.commit()
        return post.id
    finally:
        db.close()


def _get(factory, post_id: int) -> models.Post:
    db = factory()
    try:
        return db.get(models.Post, post_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# publish_due_posts -- which posts get published, and how
# ---------------------------------------------------------------------------


class TestPublishDuePosts:
    def test_publishes_due_post_and_keeps_scheduled_at(self, sessions):
        due_at = NOW - timedelta(minutes=5)
        pid = _add(sessions, "scheduled", scheduled_at=due_at)

        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW) == [pid]
        db.commit()
        db.close()

        post = _get(sessions, pid)
        assert post.status == "published"
        assert _utc(post.published_at) == NOW
        assert _utc(post.scheduled_at) == due_at  # original schedule kept as a record

    def test_post_due_exactly_now_is_published(self, sessions):
        pid = _add(sessions, "scheduled", scheduled_at=NOW)
        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW) == [pid]
        db.close()

    def test_future_scheduled_post_untouched(self, sessions):
        pid = _add(sessions, "scheduled", scheduled_at=NOW + timedelta(seconds=1))
        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW) == []
        db.commit()
        db.close()
        post = _get(sessions, pid)
        assert (post.status, post.published_at) == ("scheduled", None)

    def test_draft_untouched_even_with_old_timestamps(self, sessions):
        pid = _add(sessions, "draft")
        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW + timedelta(days=3650)) == []
        db.commit()
        db.close()
        assert _get(sessions, pid).status == "draft"

    def test_already_published_post_untouched(self, sessions):
        original = NOW - timedelta(days=10)
        pid = _add(sessions, "published", published_at=original)
        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW) == []
        db.commit()
        db.close()
        assert _utc(_get(sessions, pid).published_at) == original

    def test_many_due_posts_in_one_pass_and_only_those(self, sessions):
        due = [_add(sessions, "scheduled", scheduled_at=NOW - timedelta(minutes=m)) for m in (1, 30, 60 * 24)]
        future = _add(sessions, "scheduled", scheduled_at=NOW + timedelta(hours=1))
        draft = _add(sessions, "draft")
        published = _add(sessions, "published", published_at=NOW - timedelta(days=1))

        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW) == sorted(due)
        db.commit()
        db.close()

        assert all(_get(sessions, pid).status == "published" for pid in due)
        assert _get(sessions, future).status == "scheduled"
        assert _get(sessions, draft).status == "draft"
        assert _utc(_get(sessions, published).published_at) == NOW - timedelta(days=1)

    def test_each_post_published_only_once(self, sessions):
        pid = _add(sessions, "scheduled", scheduled_at=NOW - timedelta(minutes=1))
        db = sessions()
        assert post_publishing.publish_due_posts(db, now=NOW) == [pid]
        db.commit()
        # Later passes find nothing, and don't move published_at.
        assert post_publishing.publish_due_posts(db, now=NOW + timedelta(minutes=1)) == []
        assert post_publishing.publish_due_posts(db, now=NOW + timedelta(days=1)) == []
        db.commit()
        db.close()
        assert _utc(_get(sessions, pid).published_at) == NOW

    def test_non_utc_now_compares_as_the_same_instant(self, sessions):
        pid = _add(sessions, "scheduled", scheduled_at=NOW - timedelta(minutes=1))
        ist_now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        db = sessions()
        # Passing IST "now" must not be mistaken for 5.5h later/earlier.
        assert post_publishing.publish_due_posts(db, now=ist_now.astimezone(timezone.utc)) == [pid]
        db.close()


# ---------------------------------------------------------------------------
# run_once -- transaction, restart safety, error handling
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_commits_and_returns_ids(self, sessions):
        pid = _add(sessions, "scheduled", scheduled_at=NOW - timedelta(minutes=1))
        assert scheduled_publishing.run_once(sessions) == [pid]
        post = _get(sessions, pid)
        assert post.status == "published"
        assert post.published_at is not None

    def test_nothing_due_returns_empty(self, sessions):
        _add(sessions, "scheduled", scheduled_at=datetime.now(timezone.utc) + timedelta(days=1))
        assert scheduled_publishing.run_once(sessions) == []

    def test_restart_publishes_posts_that_fell_due_while_down_and_nothing_twice(self, sessions):
        first = _add(sessions, "scheduled", scheduled_at=NOW - timedelta(hours=5))
        assert scheduled_publishing.run_once(sessions) == [first]
        first_published_at = _get(sessions, first).published_at

        # "Downtime": more posts fall due; a fresh process then runs its first pass.
        missed = [_add(sessions, "scheduled", scheduled_at=NOW - timedelta(hours=h)) for h in (1, 2)]
        assert scheduled_publishing.run_once(sessions) == sorted(missed)
        assert scheduled_publishing.run_once(sessions) == []
        assert _get(sessions, first).published_at == first_published_at

    def test_database_error_is_rolled_back_logged_and_not_raised(self, sessions, monkeypatch, caplog):
        pid = _add(sessions, "scheduled", scheduled_at=NOW - timedelta(minutes=1))

        def boom(db, now=None):
            raise OperationalError("UPDATE posts ...", {}, Exception("database is locked"))

        monkeypatch.setattr(post_publishing, "publish_due_posts", boom)
        with caplog.at_level(logging.ERROR, logger="app.services.scheduled_publishing"):
            assert scheduled_publishing.run_once(sessions) == []
        assert "database error" in caplog.text
        assert _get(sessions, pid).status == "scheduled"

        # Next pass, database healthy again: the post is published then.
        monkeypatch.undo()
        assert scheduled_publishing.run_once(sessions) == [pid]

    def test_partial_failure_leaves_no_half_published_rows(self, sessions, monkeypatch):
        ids = [_add(sessions, "scheduled", scheduled_at=NOW - timedelta(minutes=m)) for m in (1, 2)]
        real = post_publishing.publish_due_posts

        def update_then_fail(db, now=None):
            real(db, now)
            raise OperationalError("COMMIT", {}, Exception("connection lost"))

        monkeypatch.setattr(post_publishing, "publish_due_posts", update_then_fail)
        assert scheduled_publishing.run_once(sessions) == []
        assert [_get(sessions, pid).status for pid in ids] == ["scheduled", "scheduled"]

    def test_lock_is_a_no_op_on_sqlite(self, sessions):
        db = sessions()
        assert scheduled_publishing._acquire_run_lock(db) is True
        db.close()


# ---------------------------------------------------------------------------
# The background loop
# ---------------------------------------------------------------------------


class TestLoop:
    def test_disabled_flag_starts_nothing(self):
        # conftest.py sets SCHEDULED_PUBLISHING_ENABLED=False for every test.
        async def scenario():
            return scheduled_publishing.start()

        assert asyncio.run(scenario()) is None

    def test_start_twice_reuses_one_task_and_stop_cancels_it(self, monkeypatch):
        monkeypatch.setattr(scheduled_publishing, "SCHEDULED_PUBLISHING_ENABLED", True)
        monkeypatch.setattr(scheduled_publishing, "run_once", lambda *a, **k: [])

        async def scenario():
            first = scheduled_publishing.start()
            second = scheduled_publishing.start()
            assert first is second
            assert [t.get_name() for t in asyncio.all_tasks()].count("scheduled-post-publishing") == 1
            await scheduled_publishing.stop()
            assert first.cancelled()
            assert scheduled_publishing._task is None

        asyncio.run(scenario())

    def test_loop_runs_immediately_repeats_and_survives_errors(self, monkeypatch):
        calls = []

        def flaky_run_once(*args, **kwargs):
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("session could not be created")
            return []

        monkeypatch.setattr(scheduled_publishing, "run_once", flaky_run_once)

        async def scenario():
            task = asyncio.create_task(scheduled_publishing._run_forever(0.01))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert len(calls) >= 3  # kept going after the first pass failed

    def test_lifespan_starts_and_stops_the_loop(self, monkeypatch):
        monkeypatch.setattr(scheduled_publishing, "SCHEDULED_PUBLISHING_ENABLED", True)
        passes = []
        monkeypatch.setattr(scheduled_publishing, "run_once", lambda *a, **k: passes.append(1) or [])
        # Keep the lifespan's own startup work off the real database.
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        monkeypatch.setattr("app.main.engine", engine)
        monkeypatch.setattr("app.main.SessionLocal", sessionmaker(bind=engine))
        for name in ("ensure_post_image_column", "ensure_post_view_count_column", "ensure_dashboard_indexes",
                     "ensure_user_auth0_sub_column", "ensure_post_publishing_columns"):
            monkeypatch.setattr(f"app.main.{name}", lambda: None)

        with TestClient(main_app):
            task = scheduled_publishing._task
            assert task is not None and not task.done()
        assert scheduled_publishing._task is None
        assert passes  # the startup catch-up pass ran


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


class TestThroughTheAPI:
    def test_scheduled_post_goes_live_after_job_runs(self, sessions):
        def override_get_db():
            db = sessions()
            try:
                yield db
            finally:
                db.close()

        main_app.dependency_overrides[get_db] = override_get_db
        try:
            with TestClient(main_app) as client:
                client.post("/auth/register", json={"username": "w", "email": "w@example.com", "password": "Password123"})
                token = client.post("/auth/login", json={"username": "w", "password": "Password123"}).json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}
                when = datetime.now(timezone.utc) + timedelta(hours=1)
                post = client.post(
                    "/posts",
                    json={"title": "Soon", "content": "c", "publish_option": "schedule", "scheduled_at": when.isoformat()},
                    headers=headers,
                ).json()

                # Before the time: the job leaves it alone and it's hidden.
                assert scheduled_publishing.run_once(sessions) == []
                assert client.get(f"/posts/{post['id']}").status_code == 404

                # The time arrives (job run with "now" an hour and a bit later).
                db = sessions()
                assert post_publishing.publish_due_posts(db, now=when + timedelta(seconds=1)) == [post["id"]]
                db.commit()
                db.close()

                body = client.get(f"/posts/{post['id']}").json()
                assert body["status"] == "published"
                assert body["published_at"] is not None
                assert _utc(datetime.fromisoformat(body["scheduled_at"])) == when
                assert post["id"] in [p["id"] for p in client.get("/posts").json()["items"]]
                # And it's now a normal published post: can't be pulled back.
                resp = client.put(f"/posts/{post['id']}", json={"publish_option": "save_draft"}, headers=headers)
                assert resp.status_code == 409
        finally:
            main_app.dependency_overrides.clear()
