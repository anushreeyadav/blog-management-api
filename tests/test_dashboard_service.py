"""
Unit tests for the dashboard metrics service (app/services/dashboard_service.py).

Calls the service directly rather than going through the HTTP API, the same
direct-model, isolated-in-memory-SQLite style as test_subscription_service.py.
Each metric is checked both for correctness and for isolation -- another
user's posts/comments/likes must never contribute to the count.
"""

from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from app import models
from app.auth import hash_password
from app.database import Base
from app.services import dashboard_service


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    session = sessionmaker(bind=db_engine)()
    yield session
    session.close()


def _make_plan(db_session, **overrides) -> models.SubscriptionPlan:
    defaults = dict(
        name="Pro",
        slug="pro",
        price=19.99,
        billing_interval="month",
        max_posts=None,
        max_images=None,
        max_likes=None,
        max_comments=None,
        is_active=True,
    )
    defaults.update(overrides)
    plan = models.SubscriptionPlan(**defaults)
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _make_user(db_session, plan_id: int, username: str = "someone") -> models.User:
    user = models.User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("Password123"),
        subscription_plan_id=plan_id,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_post(
    db_session,
    author: models.User,
    *,
    title: str = "post",
    view_count: int = 0,
    created_at: datetime | None = None,
) -> models.Post:
    post = models.Post(title=title, content="body", author_id=author.id, view_count=view_count)
    if created_at is not None:
        post.created_at = created_at
    db_session.add(post)
    db_session.commit()
    db_session.refresh(post)
    return post


class TestTotalPosts:
    def test_counts_only_this_users_posts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")
        _make_post(db_session, alice)
        _make_post(db_session, alice)
        _make_post(db_session, bob)

        assert dashboard_service.get_total_posts(db_session, alice) == 2
        assert dashboard_service.get_total_posts(db_session, bob) == 1

    def test_zero_when_no_posts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        assert dashboard_service.get_total_posts(db_session, alice) == 0


class TestTotalCommentsReceived:
    def test_counts_comments_on_this_users_posts_from_any_commenter(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")
        alices_post = _make_post(db_session, alice)
        bobs_post = _make_post(db_session, bob)

        # Bob and a third user comment on Alice's post -- both count as
        # comments *received* by Alice.
        carol = _make_user(db_session, plan.id, "carol")
        db_session.add(models.Comment(post_id=alices_post.id, user_id=bob.id, text="nice"))
        db_session.add(models.Comment(post_id=alices_post.id, user_id=carol.id, text="great"))
        # Alice comments on Bob's post -- a comment she *wrote*, must not
        # count as "received" for her.
        db_session.add(models.Comment(post_id=bobs_post.id, user_id=alice.id, text="thanks"))
        db_session.commit()

        assert dashboard_service.get_total_comments_received(db_session, alice) == 2
        assert dashboard_service.get_total_comments_received(db_session, bob) == 1

    def test_zero_when_no_posts_or_no_comments(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        assert dashboard_service.get_total_comments_received(db_session, alice) == 0

        _make_post(db_session, alice)
        assert dashboard_service.get_total_comments_received(db_session, alice) == 0


class TestTotalLikesReceived:
    def test_counts_likes_on_this_users_posts_from_any_liker(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")
        alices_post = _make_post(db_session, alice)
        bobs_post = _make_post(db_session, bob)

        # Bob and a third user like Alice's post -- both count as likes
        # *received* by Alice.
        carol = _make_user(db_session, plan.id, "carol")
        db_session.add(models.Like(post_id=alices_post.id, user_id=bob.id))
        db_session.add(models.Like(post_id=alices_post.id, user_id=carol.id))
        # Alice likes Bob's post -- a like she *gave*, must not count as
        # "received" for her.
        db_session.add(models.Like(post_id=bobs_post.id, user_id=alice.id))
        db_session.commit()

        assert dashboard_service.get_total_likes_received(db_session, alice) == 2
        assert dashboard_service.get_total_likes_received(db_session, bob) == 1

    def test_zero_when_no_posts_or_no_likes(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        assert dashboard_service.get_total_likes_received(db_session, alice) == 0

        _make_post(db_session, alice)
        assert dashboard_service.get_total_likes_received(db_session, alice) == 0


class TestTotalViews:
    def test_sums_view_count_across_this_users_posts_only(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")
        _make_post(db_session, alice, title="a1", view_count=5)
        _make_post(db_session, alice, title="a2", view_count=7)
        _make_post(db_session, bob, title="b1", view_count=100)

        assert dashboard_service.get_total_views(db_session, alice) == 12
        assert dashboard_service.get_total_views(db_session, bob) == 100

    def test_zero_when_no_posts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        assert dashboard_service.get_total_views(db_session, alice) == 0

    def test_zero_when_posts_have_no_views(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        _make_post(db_session, alice)

        assert dashboard_service.get_total_views(db_session, alice) == 0


class TestPostAnalytics:
    def test_returns_one_entry_per_owned_post_with_correct_counts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")

        post_1 = _make_post(db_session, alice, title="FastAPI Best Practices", view_count=80)
        post_2 = _make_post(db_session, alice, title="Learning SQLAlchemy", view_count=50)

        db_session.add(models.Like(post_id=post_1.id, user_id=bob.id))
        db_session.add(models.Comment(post_id=post_1.id, user_id=bob.id, text="a"))
        db_session.commit()

        analytics = dashboard_service.get_post_analytics(db_session, alice)

        by_id = {item["post_id"]: item for item in analytics}
        assert by_id[post_1.id] == {
            "post_id": post_1.id,
            "title": "FastAPI Best Practices",
            "likes": 1,
            "comments": 1,
            "views": 80,
        }
        assert by_id[post_2.id] == {
            "post_id": post_2.id,
            "title": "Learning SQLAlchemy",
            "likes": 0,
            "comments": 0,
            "views": 50,
        }

    def test_never_includes_another_users_posts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")
        _make_post(db_session, bob, title="Bob's post")

        assert dashboard_service.get_post_analytics(db_session, alice) == []

    def test_empty_list_when_user_has_no_posts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        assert dashboard_service.get_post_analytics(db_session, alice) == []

    def test_query_count_does_not_grow_with_number_of_posts(self, db_session, db_engine):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        def _add_post_with_activity(i: int) -> None:
            post = _make_post(db_session, alice, title=f"post {i}", view_count=i)
            db_session.add(models.Comment(post_id=post.id, user_id=alice.id, text="c"))
            db_session.add(models.Like(post_id=post.id, user_id=alice.id))
            db_session.commit()
            _ = alice.id  # see the equivalent note in TestQueryEfficiency below

        _add_post_with_activity(0)
        queries_with_one_post = TestQueryEfficiency._count_statements(
            db_engine, lambda: dashboard_service.get_post_analytics(db_session, alice)
        )

        for i in range(1, 21):
            _add_post_with_activity(i)
        queries_with_twenty_one_posts = TestQueryEfficiency._count_statements(
            db_engine, lambda: dashboard_service.get_post_analytics(db_session, alice)
        )

        # Same statement count at 1 post and at 21 posts -- if this issued
        # one query per post (N+1), the second measurement would grow with
        # the post count instead of staying identical.
        assert queries_with_one_post == queries_with_twenty_one_posts
        # The post list, plus one GROUP BY each for likes and comments --
        # three queries, not one per post.
        assert queries_with_one_post == 3


class TestPostActivity:
    def test_groups_post_creation_counts_by_calendar_day_ascending(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        # Two posts on the 18th (different times), one on the 19th, three
        # on the 20th -- created out of chronological order, to confirm
        # the result is sorted by date, not insertion order.
        _make_post(db_session, alice, created_at=datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        _make_post(db_session, alice, created_at=datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc))
        _make_post(db_session, alice, created_at=datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc))
        _make_post(db_session, alice, created_at=datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc))
        _make_post(db_session, alice, created_at=datetime(2026, 9, 18, 18, 30, tzinfo=timezone.utc))
        _make_post(db_session, alice, created_at=datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc))

        activity = dashboard_service.get_post_activity(db_session, alice)

        assert [(str(point["date"]), point["posts"]) for point in activity] == [
            ("2026-09-18", 2),
            ("2026-09-19", 1),
            ("2026-09-20", 3),
        ]

    def test_never_includes_another_users_post_activity(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")
        _make_post(db_session, bob, created_at=datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc))

        assert dashboard_service.get_post_activity(db_session, alice) == []

    def test_empty_list_when_user_has_no_posts(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        assert dashboard_service.get_post_activity(db_session, alice) == []

    def test_query_count_does_not_grow_with_number_of_posts(self, db_session, db_engine):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        def _add_post(i: int) -> None:
            _make_post(db_session, alice, title=f"post {i}", created_at=datetime(2026, 9, 1 + i % 28, tzinfo=timezone.utc))
            _ = alice.id  # see the note in TestQueryEfficiency below

        _add_post(0)
        queries_with_one_post = TestQueryEfficiency._count_statements(
            db_engine, lambda: dashboard_service.get_post_activity(db_session, alice)
        )

        for i in range(1, 21):
            _add_post(i)
        queries_with_twenty_one_posts = TestQueryEfficiency._count_statements(
            db_engine, lambda: dashboard_service.get_post_activity(db_session, alice)
        )

        assert queries_with_one_post == queries_with_twenty_one_posts == 1


class TestGetDashboardMetrics:
    def test_combines_all_four_metrics_for_the_given_user_only(self, db_session):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")
        bob = _make_user(db_session, plan.id, "bob")

        alices_post = _make_post(db_session, alice, view_count=10)
        _make_post(db_session, bob, view_count=999)

        # A comment on Alice's own post, by Alice herself -- still counts as
        # "received" (there's no author-of-the-comment exclusion; get_post_
        # analytics' comment counts don't exclude self-comments either).
        db_session.add(models.Comment(post_id=alices_post.id, user_id=alice.id, text="self comment"))
        db_session.add(models.Like(post_id=alices_post.id, user_id=bob.id))
        db_session.commit()

        metrics = dashboard_service.get_dashboard_metrics(db_session, alice)

        assert metrics == {
            "total_posts": 1,
            "total_comments_received": 1,
            "total_likes_received": 1,
            "total_views": 10,
        }


class TestQueryEfficiency:
    """
    Proves get_dashboard_metrics doesn't degrade into one query per post
    (or per comment/like) -- the number of SQL statements it issues must
    stay constant as the amount of data grows, not scale with it.
    """

    @staticmethod
    def _count_statements(db_engine, fn) -> int:
        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db_engine, "before_cursor_execute", _record)
        try:
            fn()
        finally:
            event.remove(db_engine, "before_cursor_execute", _record)
        return len(statements)

    def test_query_count_does_not_grow_with_number_of_posts(self, db_session, db_engine):
        plan = _make_plan(db_session)
        alice = _make_user(db_session, plan.id, "alice")

        def _add_post_with_activity(i: int) -> None:
            post = _make_post(db_session, alice, title=f"post {i}", view_count=i)
            db_session.add(models.Comment(post_id=post.id, user_id=alice.id, text="c"))
            db_session.add(models.Like(post_id=post.id, user_id=alice.id))
            db_session.commit()
            # db_session.commit() expires `alice` by default (expire_on_commit),
            # so the next attribute access re-fetches her row. That refresh is
            # a side effect of this test committing mid-measurement, not of
            # get_dashboard_metrics -- in the real request path (see
            # app/routers/dashboard.py), current_user is loaded fresh by
            # get_current_user with no commit in between, so it's never
            # expired when the service reads user.id. Touching it here, before
            # the measurement window opens, reproduces that same precondition.
            _ = alice.id

        _add_post_with_activity(0)
        queries_with_one_post = self._count_statements(
            db_engine, lambda: dashboard_service.get_dashboard_metrics(db_session, alice)
        )

        for i in range(1, 21):
            _add_post_with_activity(i)
        queries_with_twenty_one_posts = self._count_statements(
            db_engine, lambda: dashboard_service.get_dashboard_metrics(db_session, alice)
        )

        # Same statement count regardless of data volume -- if this scaled
        # with the number of posts (N+1), the second measurement would be
        # far larger than the first instead of identical.
        assert queries_with_one_post == queries_with_twenty_one_posts
        # A single combined SELECT (see get_dashboard_metrics), not one
        # query per metric and nowhere near one per post/comment/like.
        assert queries_with_one_post == 1
