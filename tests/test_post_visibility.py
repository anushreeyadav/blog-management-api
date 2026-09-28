"""
Public visibility of drafts / scheduled posts (app/services/post_publishing.py's
publicly_visible_clause / can_view, applied by GET /posts, GET /posts/{id},
the comments endpoints and POST /posts/{id}/like). Real endpoints, real
seeded plans, in-memory database -- same setup as the other
test_post_publishing_* files.

"Time passing" for scheduled posts is simulated by moving the visibility
check's clock (post_publishing._utc_now) forward, not by writing past
timestamps into the database.
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
from app.services import post_publishing


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


def _login(client: TestClient, username: str) -> dict:
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


def _create(client, headers, state: str, title: str, content: str = "body", hours_ahead: int = 24) -> dict:
    body = {"title": title, "content": content}
    if state == "draft":
        body["publish_option"] = "save_draft"
    elif state == "scheduled":
        when = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        body.update(publish_option="schedule", scheduled_at=when.isoformat())
    resp = client.post("/posts", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _feed_ids(client, **params) -> list[int]:
    return [p["id"] for p in client.get("/posts", params={"limit": 100, **params}).json()["items"]]


def _advance_clock(monkeypatch, hours: float) -> None:
    later = datetime.now(timezone.utc) + timedelta(hours=hours)
    monkeypatch.setattr(post_publishing, "_utc_now", lambda: later)


@pytest.fixture()
def world(env):
    """One author with a published post, a draft and a scheduled post (due
    in 24h), plus a second signed-in user who isn't the author."""
    client, sessions = env
    author = _login(client, "author")
    other = _login(client, "reader")
    posts = {
        "published": _create(client, author, "published", "Published story", "shared keyword"),
        "draft": _create(client, author, "draft", "Draft story", "shared keyword"),
        "scheduled": _create(client, author, "scheduled", "Scheduled story", "shared keyword"),
    }
    return client, sessions, author, other, posts


# ---------------------------------------------------------------------------
# 1-3. Draft hidden, scheduled hidden until due, published visible
# ---------------------------------------------------------------------------


class TestFeedVisibility:
    def test_only_published_post_in_public_feed(self, world):
        client, _, _, _, posts = world
        assert _feed_ids(client) == [posts["published"]["id"]]

    def test_signed_in_non_author_sees_the_same_feed(self, world):
        client, _, _, other, posts = world
        resp = client.get("/posts", params={"limit": 100}, headers=other)
        assert [p["id"] for p in resp.json()["items"]] == [posts["published"]["id"]]

    def test_author_feed_is_also_public_only(self, world):
        # GET /posts is the public feed for everyone; authors see their own
        # drafts/scheduled posts via GET /posts/mine instead.
        client, _, author, _, posts = world
        resp = client.get("/posts", params={"limit": 100}, headers=author)
        assert [p["id"] for p in resp.json()["items"]] == [posts["published"]["id"]]

    def test_scheduled_post_hidden_until_due_then_visible(self, world, monkeypatch):
        client, _, _, _, posts = world
        sid = posts["scheduled"]["id"]

        _advance_clock(monkeypatch, 23)
        assert sid not in _feed_ids(client)
        assert client.get(f"/posts/{sid}").status_code == 404

        _advance_clock(monkeypatch, 25)
        assert sid in _feed_ids(client)
        assert client.get(f"/posts/{sid}").status_code == 200

    def test_draft_never_becomes_visible(self, world, monkeypatch):
        client, _, _, _, posts = world
        _advance_clock(monkeypatch, 24 * 365)
        assert posts["draft"]["id"] not in _feed_ids(client)
        assert client.get(f"/posts/{posts['draft']['id']}").status_code == 404

    def test_publishing_a_draft_makes_it_visible(self, world):
        client, _, author, _, posts = world
        did = posts["draft"]["id"]
        assert client.put(f"/posts/{did}", json={"publish_option": "publish_now"}, headers=author).status_code == 200
        assert did in _feed_ids(client)
        assert client.get(f"/posts/{did}").status_code == 200

    def test_publishing_a_scheduled_post_early_makes_it_visible(self, world):
        client, _, author, _, posts = world
        sid = posts["scheduled"]["id"]
        client.put(f"/posts/{sid}", json={"publish_option": "publish_now"}, headers=author)
        assert sid in _feed_ids(client)

    def test_existing_default_created_posts_appear_normally(self, env):
        client, _ = env
        author = _login(client, "author")
        ids = [client.post("/posts", json={"title": f"P{i}", "content": "c"}, headers=author).json()["id"] for i in range(3)]
        body = client.get("/posts").json()
        assert [p["id"] for p in body["items"]] == ids  # same id ordering as before
        assert body["total"] == 3


# ---------------------------------------------------------------------------
# 4. Search
# ---------------------------------------------------------------------------


class TestSearchVisibility:
    def test_search_matching_all_three_returns_only_published(self, world):
        client, _, _, _, posts = world
        body = client.get("/posts", params={"search": "shared keyword"}).json()
        assert [p["id"] for p in body["items"]] == [posts["published"]["id"]]
        assert body["total"] == 1

    @pytest.mark.parametrize("term", ["Draft story", "Scheduled story"])
    def test_search_by_unpublished_title_finds_nothing(self, world, term):
        client, _, _, _, _ = world
        body = client.get("/posts", params={"search": term}).json()
        assert body == {"items": [], "page": 1, "limit": 10, "total": 0, "total_pages": 0}

    def test_search_finds_scheduled_post_once_due(self, world, monkeypatch):
        client, _, _, _, posts = world
        _advance_clock(monkeypatch, 25)
        assert _feed_ids(client, search="Scheduled story") == [posts["scheduled"]["id"]]


# ---------------------------------------------------------------------------
# 5. Pagination counts
# ---------------------------------------------------------------------------


class TestPaginationCounts:
    def test_counts_and_pages_only_include_visible_posts(self, env):
        client, _ = env
        author = _login(client, "author")
        published = [_create(client, author, "published", f"Pub {i}")["id"] for i in range(5)]
        for i in range(4):
            _create(client, author, "draft", f"Draft {i}")
            _create(client, author, "scheduled", f"Sched {i}")

        page1 = client.get("/posts", params={"page": 1, "limit": 2}).json()
        page2 = client.get("/posts", params={"page": 2, "limit": 2}).json()
        page3 = client.get("/posts", params={"page": 3, "limit": 2}).json()
        page4 = client.get("/posts", params={"page": 4, "limit": 2}).json()

        assert page1["total"] == 5
        assert page1["total_pages"] == 3
        assert [len(p["items"]) for p in (page1, page2, page3, page4)] == [2, 2, 1, 0]
        assert [p["id"] for pg in (page1, page2, page3) for p in pg["items"]] == published

    def test_counts_grow_when_a_scheduled_post_becomes_due(self, world, monkeypatch):
        client, _, _, _, _ = world
        assert client.get("/posts").json()["total"] == 1
        _advance_clock(monkeypatch, 25)
        assert client.get("/posts").json()["total"] == 2


# ---------------------------------------------------------------------------
# 6. Detail endpoint
# ---------------------------------------------------------------------------


class TestDetailVisibility:
    @pytest.mark.parametrize("state", ["draft", "scheduled"])
    def test_anonymous_gets_same_404_as_missing_post(self, world, state):
        client, _, _, _, posts = world
        resp = client.get(f"/posts/{posts[state]['id']}")
        missing = client.get("/posts/999999")
        assert resp.status_code == missing.status_code == 404
        assert resp.json() == missing.json() == {"detail": "Post not found"}

    @pytest.mark.parametrize("state", ["draft", "scheduled"])
    def test_other_signed_in_user_gets_404(self, world, state):
        client, _, _, other, posts = world
        assert client.get(f"/posts/{posts[state]['id']}", headers=other).status_code == 404

    @pytest.mark.parametrize("state", ["draft", "scheduled"])
    def test_author_can_preview_own_unpublished_post(self, world, state):
        client, _, author, _, posts = world
        resp = client.get(f"/posts/{posts[state]['id']}", headers=author)
        assert resp.status_code == 200
        assert resp.json()["status"] == state
        assert resp.json()["content"] == "shared keyword"

    def test_invalid_token_is_treated_as_anonymous(self, world):
        client, _, _, _, posts = world
        bad = {"Authorization": "Bearer not-a-real-token"}
        assert client.get(f"/posts/{posts['draft']['id']}", headers=bad).status_code == 404
        assert client.get(f"/posts/{posts['published']['id']}", headers=bad).status_code == 200

    def test_published_post_visible_to_everyone(self, world):
        client, _, author, other, posts = world
        pid = posts["published"]["id"]
        for headers in (None, other, author):
            assert client.get(f"/posts/{pid}", headers=headers).status_code == 200

    def test_view_count_unchanged_by_author_preview_but_counted_for_published(self, world):
        client, sessions, author, _, posts = world
        client.get(f"/posts/{posts['draft']['id']}", headers=author)
        client.get(f"/posts/{posts['published']['id']}")
        db = sessions()
        try:
            assert db.get(models.Post, posts["draft"]["id"]).view_count == 0
            assert db.get(models.Post, posts["published"]["id"]).view_count == 1
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Author-only views are preserved
# ---------------------------------------------------------------------------


class TestAuthorViewsPreserved:
    def test_posts_mine_still_lists_every_status(self, world):
        client, _, author, _, posts = world
        mine = client.get("/posts/mine", headers=author).json()
        assert {p["id"]: p["status"] for p in mine} == {p["id"]: s for s, p in posts.items()}

    def test_author_can_still_edit_and_delete_unpublished_posts(self, world):
        client, _, author, _, posts = world
        assert client.put(f"/posts/{posts['draft']['id']}", json={"title": "x"}, headers=author).status_code == 200
        assert client.delete(f"/posts/{posts['scheduled']['id']}", headers=author).status_code == 204

    def test_non_author_edit_of_draft_still_403(self, world):
        # Unchanged authorization: PUT/DELETE keep their existing 403 for
        # non-owners rather than switching to 404.
        client, _, _, other, posts = world
        assert client.put(f"/posts/{posts['draft']['id']}", json={"title": "x"}, headers=other).status_code == 403


# ---------------------------------------------------------------------------
# Comments and likes don't expose unpublished posts either
# ---------------------------------------------------------------------------


class TestCommentsAndLikesOnUnpublishedPosts:
    @pytest.mark.parametrize("state", ["draft", "scheduled"])
    def test_public_comment_list_404(self, world, state):
        client, _, _, other, posts = world
        assert client.get(f"/posts/{posts[state]['id']}/comments").status_code == 404
        assert client.get(f"/posts/{posts[state]['id']}/comments", headers=other).status_code == 404

    @pytest.mark.parametrize("state", ["draft", "scheduled"])
    def test_non_author_cannot_comment_or_like(self, world, state):
        client, sessions, _, other, posts = world
        pid = posts[state]["id"]
        assert client.post(f"/posts/{pid}/comments", json={"text": "hi"}, headers=other).status_code == 404
        assert client.post(f"/posts/{pid}/like", headers=other).status_code == 404
        db = sessions()
        try:
            assert db.query(models.Comment).count() == 0
            assert db.query(models.Like).count() == 0
            # (The fixture's Pro subscriptions create subscription notifications.)
            assert (
                db.query(models.Notification)
                .filter(models.Notification.notification_type.in_(["like", "comment"]))
                .count()
                == 0
            )
        finally:
            db.close()

    def test_author_can_still_use_comments_on_own_draft(self, world):
        client, _, author, _, posts = world
        pid = posts["draft"]["id"]
        assert client.post(f"/posts/{pid}/comments", json={"text": "note to self"}, headers=author).status_code == 201
        assert len(client.get(f"/posts/{pid}/comments", headers=author).json()) == 1

    def test_published_post_comments_and_likes_unchanged(self, world):
        client, _, _, other, posts = world
        pid = posts["published"]["id"]
        assert client.post(f"/posts/{pid}/comments", json={"text": "nice"}, headers=other).status_code == 201
        assert client.post(f"/posts/{pid}/like", headers=other).status_code == 201
        assert len(client.get(f"/posts/{pid}/comments").json()) == 1

    def test_comments_work_once_scheduled_post_is_due(self, world, monkeypatch):
        client, _, _, other, posts = world
        _advance_clock(monkeypatch, 25)
        pid = posts["scheduled"]["id"]
        assert client.post(f"/posts/{pid}/like", headers=other).status_code == 201
        assert client.get(f"/posts/{pid}/comments").status_code == 200
