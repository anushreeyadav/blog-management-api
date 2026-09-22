"""
Phase 13 -- controlled test data, hand-verified expected numbers, and
independent database cross-checks for every GET /dashboard/me metric.

This is deliberately not a repeat of test_dashboard_service.py /
test_dashboard_security.py's isolation-focused assertions ("only the
right rows are counted"). Here the question is arithmetic correctness:
build a scenario with a known, worked-out answer, then check the API's
numbers against it two ways --

  1. a hand-calculated literal (matching the phase's own worked example:
     3 posts, 4 comments received, 5+3+7=15 likes received), and
  2. an independent raw query against the same test database (via direct
     session access, not through dashboard_service.py) --

so a bug that happened to satisfy one check couldn't silently pass the
other. Nothing here inspects rendered chart output -- every assertion
reads the JSON response body and/or queries the database directly.

NOTE: total_comments_received counts comments *received* on the user's
own posts (mirroring total_likes_received), not comments the user wrote
elsewhere -- this was total_comments_made until a manual testing session
(post-Phase-18) surfaced that "Total Comments" reading as "comments I
wrote" was inconsistent with "Total Posts"/"Total Likes Received"/"Total
Views" all being about the user's own content, and confusingly different
from what the per-post analytics' own "comments" field already meant.
"""

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}


@pytest.fixture()
def db_client():
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


def _register_and_login(client: TestClient, username: str, email: str, password: str = "Password123") -> dict:
    client.post("/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upgrade_to_pro(client: TestClient, headers: dict) -> None:
    """Basic caps a user at 1 post -- User A's scenario needs three."""
    plans = client.get("/subscriptions/plans").json()["plans"]
    pro_id = next(p["id"] for p in plans if p["slug"] == "pro")
    resp = client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers)
    assert resp.status_code == 201


@pytest.fixture()
def scenario(db_client):
    """
    Builds exactly the worked example from the phase prompt:

      User A creates Post 1, Post 2, Post 3.
      Another user leaves 4 comments on User A's posts (2 on Post 1, 1 on
        Post 2, 1 on Post 3) -- comments *received*, not comments User A
        wrote herself.
      Other users like User A's posts: Post 1 -> 5, Post 2 -> 3, Post 3 -> 7
        (15 total), each like from a distinct user (Like enforces one like
        per (post, user) pair, so reaching 5/7 likes on a single post needs
        that many distinct likers).

    Every "other" liker stays on the default Basic plan (max_likes=5): the
    heaviest-liking users (u1-u3, who each like all three of A's posts)
    only ever place 3 likes total, safely under that cap -- no plan
    upgrades needed for any of them.
    """
    client, TestingSessionLocal = db_client
    headers_a = _register_and_login(client, "user_a", "usera@example.com")
    _upgrade_to_pro(client, headers_a)

    post_1 = client.post("/posts", json={"title": "Post 1", "content": "body"}, headers=headers_a).json()
    post_2 = client.post("/posts", json={"title": "Post 2", "content": "body"}, headers=headers_a).json()
    post_3 = client.post("/posts", json={"title": "Post 3", "content": "body"}, headers=headers_a).json()

    # Another user comments on User A's posts -- 2 on Post 1, 1 on Post 2,
    # 1 on Post 3 (4 total) -- comments *received* by User A, from someone
    # else.
    headers_other = _register_and_login(client, "someone_else", "someone_else@example.com")
    comment_counts = {post_1["id"]: 2, post_2["id"]: 1, post_3["id"]: 1}
    for post_id, count in comment_counts.items():
        for i in range(count):
            resp = client.post(
                f"/posts/{post_id}/comments", json={"text": f"comment {i}"}, headers=headers_other
            )
            assert resp.status_code == 201

    # Likers: u1-u3 like all three posts (3 likes each); u4-u5 like Post 1
    # and Post 3; u6-u7 like Post 3 only. Tally per post: Post 1 = 5
    # (u1-u5), Post 2 = 3 (u1-u3), Post 3 = 7 (u1-u7).
    likers = {}
    for i in range(1, 8):
        username = f"liker_{i}"
        likers[i] = _register_and_login(client, username, f"{username}@example.com")

    like_matrix = {
        post_1["id"]: [1, 2, 3, 4, 5],
        post_2["id"]: [1, 2, 3],
        post_3["id"]: [1, 2, 3, 4, 5, 6, 7],
    }
    for post_id, liker_ids in like_matrix.items():
        for liker_id in liker_ids:
            resp = client.post(f"/posts/{post_id}/like", headers=likers[liker_id])
            assert resp.status_code == 201

    return {
        "headers_a": headers_a,
        "post_1": post_1,
        "post_2": post_2,
        "post_3": post_3,
        "TestingSessionLocal": TestingSessionLocal,
    }


class TestSummaryStatisticsMatchTheWorkedExample:
    def test_total_posts_is_3(self, db_client, scenario):
        client, _ = db_client
        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        assert body["statistics"]["total_posts"] == 3

    def test_total_comments_received_is_4(self, db_client, scenario):
        client, _ = db_client
        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        assert body["statistics"]["total_comments_received"] == 4  # 2 + 1 + 1

    def test_total_likes_received_is_15(self, db_client, scenario):
        client, _ = db_client
        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        assert body["statistics"]["total_likes_received"] == 15  # 5 + 3 + 7


class TestSummaryStatisticsMatchTheRawDatabase:
    """
    Independent of the hand-calculated expectations above: queries the
    same test database directly (bypassing app/services/dashboard_service.py
    entirely) and checks the API's numbers agree with what's actually
    stored. A bug in the dashboard's own aggregation logic that happened to
    still add up to 3/4/15 by coincidence would be caught here, since this
    class derives its expectation a completely different way.
    """

    def test_total_posts_matches_a_raw_count_query(self, db_client, scenario):
        client, _ = db_client
        db = scenario["TestingSessionLocal"]()
        try:
            user_id = client.get("/auth/me", headers=scenario["headers_a"]).json()["id"]
            expected = db.query(models.Post).filter(models.Post.author_id == user_id).count()
        finally:
            db.close()

        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        assert body["statistics"]["total_posts"] == expected == 3

    def test_total_comments_received_matches_a_raw_join_query(self, db_client, scenario):
        client, _ = db_client
        db = scenario["TestingSessionLocal"]()
        try:
            user_id = client.get("/auth/me", headers=scenario["headers_a"]).json()["id"]
            expected = (
                db.query(models.Comment)
                .join(models.Post, models.Comment.post_id == models.Post.id)
                .filter(models.Post.author_id == user_id)
                .count()
            )
        finally:
            db.close()

        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        assert body["statistics"]["total_comments_received"] == expected == 4

    def test_total_likes_received_matches_a_raw_join_query(self, db_client, scenario):
        client, _ = db_client
        db = scenario["TestingSessionLocal"]()
        try:
            user_id = client.get("/auth/me", headers=scenario["headers_a"]).json()["id"]
            expected = (
                db.query(models.Like)
                .join(models.Post, models.Like.post_id == models.Post.id)
                .filter(models.Post.author_id == user_id)
                .count()
            )
        finally:
            db.close()

        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        assert body["statistics"]["total_likes_received"] == expected == 15

    def test_total_views_matches_a_raw_sum_query(self, db_client, scenario):
        client, _ = db_client
        headers_a = scenario["headers_a"]

        # Generate a known, uneven number of views per post so this isn't
        # trivially zero=zero.
        client.get(f"/posts/{scenario['post_1']['id']}")
        client.get(f"/posts/{scenario['post_1']['id']}")
        client.get(f"/posts/{scenario['post_2']['id']}")
        client.get(f"/posts/{scenario['post_3']['id']}")
        client.get(f"/posts/{scenario['post_3']['id']}")
        client.get(f"/posts/{scenario['post_3']['id']}")

        db = scenario["TestingSessionLocal"]()
        try:
            user_id = client.get("/auth/me", headers=headers_a).json()["id"]
            expected = (
                db.query(func.coalesce(func.sum(models.Post.view_count), 0))
                .filter(models.Post.author_id == user_id)
                .scalar()
            )
        finally:
            db.close()

        body = client.get("/dashboard/me", headers=headers_a).json()
        assert body["statistics"]["total_views"] == expected == 6  # 2 + 1 + 3


class TestPerPostAnalyticsMatchTheWorkedExample:
    def test_each_posts_like_count_matches_the_example(self, db_client, scenario):
        client, _ = db_client
        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        by_id = {item["post_id"]: item for item in body["post_analytics"]}

        assert by_id[scenario["post_1"]["id"]]["likes"] == 5
        assert by_id[scenario["post_2"]["id"]]["likes"] == 3
        assert by_id[scenario["post_3"]["id"]]["likes"] == 7

    def test_each_posts_like_count_matches_a_raw_count_query_per_post(self, db_client, scenario):
        client, _ = db_client
        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()
        by_id = {item["post_id"]: item for item in body["post_analytics"]}

        db = scenario["TestingSessionLocal"]()
        try:
            for post_key, expected_likes in (("post_1", 5), ("post_2", 3), ("post_3", 7)):
                post_id = scenario[post_key]["id"]
                actual_db_count = db.query(models.Like).filter(models.Like.post_id == post_id).count()
                assert actual_db_count == expected_likes
                assert by_id[post_id]["likes"] == actual_db_count
        finally:
            db.close()

    def test_sum_of_per_post_likes_equals_the_summary_total(self, db_client, scenario):
        client, _ = db_client
        body = client.get("/dashboard/me", headers=scenario["headers_a"]).json()

        assert sum(item["likes"] for item in body["post_analytics"]) == body["statistics"]["total_likes_received"]
