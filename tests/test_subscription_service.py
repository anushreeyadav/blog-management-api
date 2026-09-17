"""
Unit tests for the centralized subscription access-control service
(app/services/subscription.py's can_perform_action / enforce_action_limit).

These call the service directly rather than going through the HTTP API --
the router-level wiring (that a real request actually gets gated) is
covered by tests/test_subscriptions.py and test_user_subscription_plan.py.
Mirrors the direct-model, isolated-in-memory-SQLite style of
test_subscription_plan_model.py.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest
from fastapi import HTTPException

from app import models
from app.auth import hash_password
from app.database import Base
from app.services import subscription as subscription_service
from app.services.subscription import (
    ACTION_COMMENT_ON_POST,
    ACTION_CREATE_POST,
    ACTION_LIKE_POST,
    ACTION_UPLOAD_IMAGE,
    LIMIT_EXCEEDED_MESSAGE,
    can_perform_action,
    enforce_action_limit,
)

ALL_ACTIONS = [ACTION_CREATE_POST, ACTION_UPLOAD_IMAGE, ACTION_LIKE_POST, ACTION_COMMENT_ON_POST]


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
        name="Basic",
        slug="basic",
        price=4.99,
        billing_interval="month",
        max_posts=1,
        max_images=1,
        max_likes=5,
        max_comments=5,
        is_active=True,
    )
    defaults.update(overrides)
    plan = models.SubscriptionPlan(**defaults)
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _make_user(db_session, plan_id: int | None, username: str = "someone") -> models.User:
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


def _target_post(db_session, user: models.User, action: str) -> models.Post | None:
    """
    Image usage is counted per-post (Premium allows 2 images *per post*,
    not a lifetime total), so image-action tests need a specific post to
    check/populate usage against. The other three actions are per-user
    totals and don't need one.
    """
    if action != ACTION_UPLOAD_IMAGE:
        return None
    post = models.Post(title="gallery target", content="body", author_id=user.id)
    db_session.add(post)
    db_session.commit()
    db_session.refresh(post)
    return post


def _add_usage(db_session, user: models.User, action: str, count: int, *, post: models.Post | None = None) -> None:
    """Creates `count` existing records of the kind `action` counts as usage."""
    for i in range(count):
        if action == ACTION_CREATE_POST:
            db_session.add(models.Post(title=f"post {i}", content="body", author_id=user.id))
        elif action == ACTION_UPLOAD_IMAGE:
            db_session.add(models.PostImage(post_id=post.id, image=f"/media/posts/{i}.jpg"))
        elif action == ACTION_LIKE_POST:
            target = models.Post(title=f"target {i}", content="body", author_id=user.id)
            db_session.add(target)
            db_session.flush()
            db_session.add(models.Like(post_id=target.id, user_id=user.id))
        elif action == ACTION_COMMENT_ON_POST:
            target = models.Post(title=f"target {i}", content="body", author_id=user.id)
            db_session.add(target)
            db_session.flush()
            db_session.add(models.Comment(post_id=target.id, user_id=user.id, text="a comment"))
        else:
            raise ValueError(action)
    db_session.commit()


# ---------------------------------------------------------------------------
# Basic within limit / exceeding limit -- for every supported action
# ---------------------------------------------------------------------------


class TestBasicPlan:
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_within_limit_is_allowed(self, db_session, action):
        plan = _make_plan(db_session, name="Basic", slug="basic", max_posts=1, max_images=1, max_likes=5, max_comments=5)
        user = _make_user(db_session, plan.id)
        post = _target_post(db_session, user, action)

        assert can_perform_action(db_session, user, action, post=post) is True
        enforce_action_limit(db_session, user, action, post=post)  # must not raise

    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_exceeding_limit_is_rejected(self, db_session, action):
        plan = _make_plan(db_session, name="Basic", slug="basic", max_posts=1, max_images=1, max_likes=5, max_comments=5)
        user = _make_user(db_session, plan.id)
        post = _target_post(db_session, user, action)
        limit = getattr(plan, subscription_service._LIMIT_FIELD_BY_ACTION[action])
        _add_usage(db_session, user, action, limit, post=post)

        assert can_perform_action(db_session, user, action, post=post) is False
        with pytest.raises(HTTPException) as exc_info:
            enforce_action_limit(db_session, user, action, post=post)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == LIMIT_EXCEEDED_MESSAGE


# ---------------------------------------------------------------------------
# Premium within limit / exceeding limit -- for every supported action
# ---------------------------------------------------------------------------


class TestPremiumPlan:
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_within_limit_is_allowed(self, db_session, action):
        plan = _make_plan(
            db_session, name="Premium", slug="premium", max_posts=2, max_images=2, max_likes=25, max_comments=25
        )
        user = _make_user(db_session, plan.id)
        post = _target_post(db_session, user, action)
        limit = getattr(plan, subscription_service._LIMIT_FIELD_BY_ACTION[action])
        _add_usage(db_session, user, action, limit - 1, post=post)

        assert can_perform_action(db_session, user, action, post=post) is True
        enforce_action_limit(db_session, user, action, post=post)  # must not raise

    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_exceeding_limit_is_rejected(self, db_session, action):
        plan = _make_plan(
            db_session, name="Premium", slug="premium", max_posts=2, max_images=2, max_likes=25, max_comments=25
        )
        user = _make_user(db_session, plan.id)
        post = _target_post(db_session, user, action)
        limit = getattr(plan, subscription_service._LIMIT_FIELD_BY_ACTION[action])
        _add_usage(db_session, user, action, limit, post=post)

        assert can_perform_action(db_session, user, action, post=post) is False
        with pytest.raises(HTTPException) as exc_info:
            enforce_action_limit(db_session, user, action, post=post)
        assert exc_info.value.detail == LIMIT_EXCEEDED_MESSAGE


# ---------------------------------------------------------------------------
# Pro: unlimited access regardless of usage
# ---------------------------------------------------------------------------


class TestProPlanUnlimited:
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_pro_is_never_blocked_however_much_is_used(self, db_session, action):
        plan = _make_plan(
            db_session, name="Pro", slug="pro", price=19.99,
            max_posts=None, max_images=None, max_likes=None, max_comments=None,
        )
        user = _make_user(db_session, plan.id)
        post = _target_post(db_session, user, action)
        _add_usage(db_session, user, action, 500, post=post)

        assert can_perform_action(db_session, user, action, post=post) is True
        enforce_action_limit(db_session, user, action, post=post)  # must not raise

    def test_pro_skips_the_usage_query_entirely(self, db_session, monkeypatch):
        """Avoid duplicate/unnecessary queries: an unlimited plan should
        never even issue the usage COUNT(*) query."""
        plan = _make_plan(db_session, name="Pro", slug="pro", max_posts=None, max_images=None, max_likes=None, max_comments=None)
        user = _make_user(db_session, plan.id)

        def _boom(*args, **kwargs):
            raise AssertionError("usage query should not run for an unlimited plan")

        monkeypatch.setattr(subscription_service, "_current_usage", _boom)
        assert can_perform_action(db_session, user, ACTION_CREATE_POST) is True


# ---------------------------------------------------------------------------
# User without a valid subscription
# ---------------------------------------------------------------------------


class TestUserWithoutValidSubscription:
    def test_user_with_no_resolvable_plan_is_denied(self, db_session):
        """
        Every real user always has subscription_plan_id set (NOT NULL --
        see Sub-Task 4), but the service must still fail closed rather than
        crash or silently allow the action if that ever isn't the case
        (e.g. a user object built directly, bypassing registration).
        """
        user = models.User(
            username="planless",
            email="planless@example.com",
            password_hash=hash_password("Password123"),
        )
        user.subscription_plan = None  # simulate an unresolvable plan without touching the DB

        assert can_perform_action(db_session, user, ACTION_CREATE_POST) is False
        with pytest.raises(HTTPException) as exc_info:
            enforce_action_limit(db_session, user, ACTION_CREATE_POST)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == LIMIT_EXCEEDED_MESSAGE

    def test_newly_registered_user_with_no_formal_subscription_row_still_governed_by_basic(self, db_session):
        """
        A user who has never called /subscriptions/subscribe has no
        Subscription row at all, yet must still be correctly evaluated
        against their (Basic) plan -- not treated as unrestricted.
        """
        basic = _make_plan(db_session, name="Basic", slug="basic", max_posts=1)
        user = _make_user(db_session, basic.id)
        assert subscription_service.get_active_subscription(db_session, user) is None

        assert can_perform_action(db_session, user, ACTION_CREATE_POST) is True
        _add_usage(db_session, user, ACTION_CREATE_POST, 1)
        assert can_perform_action(db_session, user, ACTION_CREATE_POST) is False


# ---------------------------------------------------------------------------
# Image uploads are counted per-post, not per-user: each post has its own
# independent gallery limit.
# ---------------------------------------------------------------------------


class TestImageLimitIsPerPostNotPerUser:
    def test_a_fresh_post_is_unaffected_by_another_post_being_at_its_limit(self, db_session):
        plan = _make_plan(db_session, name="Basic", slug="basic", max_images=1)
        user = _make_user(db_session, plan.id)
        maxed_out = models.Post(title="t1", content="c", author_id=user.id)
        fresh = models.Post(title="t2", content="c", author_id=user.id)
        db_session.add_all([maxed_out, fresh])
        db_session.commit()
        db_session.refresh(maxed_out)
        db_session.refresh(fresh)
        db_session.add(models.PostImage(post_id=maxed_out.id, image="/media/posts/old.jpg"))
        db_session.commit()

        # maxed_out already has its one allowed image; fresh has none yet --
        # each post's usage is independent of the user's other posts.
        assert can_perform_action(db_session, user, ACTION_UPLOAD_IMAGE, post=maxed_out) is False
        assert can_perform_action(db_session, user, ACTION_UPLOAD_IMAGE, post=fresh) is True

    def test_second_image_on_the_same_post_is_rejected_at_the_basic_limit(self, db_session):
        plan = _make_plan(db_session, name="Basic", slug="basic", max_images=1)
        user = _make_user(db_session, plan.id)
        post = models.Post(title="t", content="c", author_id=user.id)
        db_session.add(post)
        db_session.commit()
        db_session.refresh(post)

        assert can_perform_action(db_session, user, ACTION_UPLOAD_IMAGE, post=post) is True
        db_session.add(models.PostImage(post_id=post.id, image="/media/posts/first.jpg"))
        db_session.commit()

        assert can_perform_action(db_session, user, ACTION_UPLOAD_IMAGE, post=post) is False

    def test_premium_post_holds_a_second_image_but_not_a_third(self, db_session):
        plan = _make_plan(db_session, name="Premium", slug="premium", max_images=2)
        user = _make_user(db_session, plan.id)
        post = models.Post(title="t", content="c", author_id=user.id)
        db_session.add(post)
        db_session.commit()
        db_session.refresh(post)

        db_session.add(models.PostImage(post_id=post.id, image="/media/posts/1.jpg"))
        db_session.commit()
        assert can_perform_action(db_session, user, ACTION_UPLOAD_IMAGE, post=post) is True  # room for a 2nd

        db_session.add(models.PostImage(post_id=post.id, image="/media/posts/2.jpg"))
        db_session.commit()
        assert can_perform_action(db_session, user, ACTION_UPLOAD_IMAGE, post=post) is False  # no room for a 3rd
