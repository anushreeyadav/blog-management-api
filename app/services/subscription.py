"""
Centralized subscription access-control service.

This is the single place that decides whether a user's plan lets them
perform a given action -- app/routers/posts.py, comments.py, and likes.py
all call enforce_action_limit() instead of checking plan fields themselves,
so the rule for "what counts as usage" and "what the limit means" lives in
exactly one place, not once per router. Because the check runs inside the
route handler itself (server-side, on every request, regardless of who or
what calls the API), it applies identically whether the caller is the
project's own UI, Swagger, or a raw HTTP request -- there is no client-side
gate to route around.

Every user has an "effective plan" at all times: User.subscription_plan_id
(see app/models.py), which defaults to Basic at registration and is kept in
sync whenever the user subscribes/cancels (app/routers/subscriptions.py).
Reading it is a single relationship lookup already resolved on the current
user object -- no separate query into the historical Subscription table is
needed just to find "the" plan.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app import models
from app.services.plans import seed_default_plans

BASIC_PLAN_SLUG = "basic"

LIMIT_EXCEEDED_MESSAGE = "You’ve reached your plan limit. Kindly upgrade your plan to continue."

ACTION_CREATE_POST = "create_post"
ACTION_UPLOAD_IMAGE = "upload_image"
ACTION_LIKE_POST = "like_post"
ACTION_COMMENT_ON_POST = "comment_on_post"

# Which SubscriptionPlan column holds the limit for each action. A limit of
# None means unlimited (Pro) -- the same convention every max_* column uses.
_LIMIT_FIELD_BY_ACTION = {
    ACTION_CREATE_POST: "max_posts",
    ACTION_UPLOAD_IMAGE: "max_images",
    ACTION_LIKE_POST: "max_likes",
    ACTION_COMMENT_ON_POST: "max_comments",
}


def get_or_create_basic_plan(db: Session) -> models.SubscriptionPlan:
    """
    Registration (and any other caller needing "the" default plan) goes
    through the same idempotent seeder app startup uses (see
    app/services/plans.py), so a database that was created via
    Base.metadata.create_all() without running the real migrations -- e.g.
    the isolated in-memory SQLite databases the test suite uses -- still
    ends up with the exact same Basic/Premium/Pro definitions.
    """
    return seed_default_plans(db)[BASIC_PLAN_SLUG]


def get_active_subscription(db: Session, user: models.User) -> models.Subscription | None:
    now = datetime.now(timezone.utc)
    return (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status == "active",
            models.Subscription.current_period_end > now,
        )
        .order_by(models.Subscription.current_period_end.desc())
        .first()
    )


def get_effective_plan(user: models.User) -> models.SubscriptionPlan | None:
    """
    The plan that governs `user` right now. Every user is assigned Basic at
    registration and this is never expected to be None in practice, but a
    user record that somehow has no resolvable plan (e.g. constructed
    directly, bypassing registration, as some tests deliberately do) is
    treated as having no valid subscription rather than raising -- callers
    fail closed instead of crashing or silently allowing the action.
    """
    return user.subscription_plan


def _current_usage(db: Session, user: models.User, action: str, *, post: models.Post | None = None) -> int:
    if action == ACTION_CREATE_POST:
        return db.query(models.Post).filter(models.Post.author_id == user.id).count()

    if action == ACTION_UPLOAD_IMAGE:
        # Per-post, not per-user: "Premium allows 2 images per post" caps
        # how many images a single post may hold, not a lifetime total
        # across all of the user's posts -- enforcement (post given) always
        # counts just that one post's images.
        if post is not None:
            return db.query(models.PostImage).filter(models.PostImage.post_id == post.id).count()
        # No specific post: used only by the read-only usage summary
        # (GET /subscriptions/usage), where there's no single "the" post to
        # check against the per-post limit -- reports the total across all
        # of the user's posts instead, so there's still a meaningful number
        # to show alongside the (per-post) limit.
        return (
            db.query(models.PostImage)
            .join(models.Post, models.PostImage.post_id == models.Post.id)
            .filter(models.Post.author_id == user.id)
            .count()
        )

    if action == ACTION_LIKE_POST:
        # Chosen meaning of max_likes -- "current active likes", not a
        # lifetime running total and not reset on a billing-period boundary:
        # this counts the user's Like rows that exist *right now*. Unliking
        # a post (DELETE /posts/{id}/like) deletes its row, which frees that
        # slot for a new like immediately -- there is no cooldown or
        # per-period reset. This was chosen for two reasons: (1) it matches
        # how max_posts/max_comments already work in this app -- a live
        # count against the current table, not a period-windowed one, since
        # there is no scheduled job or renewal hook anywhere that resets
        # usage counters when a Subscription's billing period rolls over;
        # and (2) "likes per subscription period" would mean an unliked
        # post still permanently consumes quota, which has no clear
        # product benefit here and would be surprising given likes/unlikes
        # are otherwise fully reversible actions in this app.
        return db.query(models.Like).filter(models.Like.user_id == user.id).count()

    if action == ACTION_COMMENT_ON_POST:
        return db.query(models.Comment).filter(models.Comment.user_id == user.id).count()

    raise ValueError(f"Unknown subscription action: {action!r}")


def can_perform_action(db: Session, user: models.User, action: str, *, post: models.Post | None = None) -> bool:
    """
    Retrieves the user's active plan, looks up that action's configured
    limit, determines current usage, and compares the two. A single
    COUNT(*) query is issued -- and only if the plan doesn't already grant
    unlimited access, in which case no usage query is needed at all.
    """
    plan = get_effective_plan(user)
    if plan is None:
        return False

    limit = getattr(plan, _LIMIT_FIELD_BY_ACTION[action])
    if limit is None:
        return True

    usage = _current_usage(db, user, action, post=post)
    return usage < limit


def enforce_action_limit(db: Session, user: models.User, action: str, *, post: models.Post | None = None) -> None:
    """
    Raises if `user`'s plan doesn't have room for `action` right now.

    This is a check, then (in the caller) an insert -- two concurrent
    requests from the same user (e.g. firing several "create post"
    requests back to back, faster than either can commit) could otherwise
    both pass the usage check before either is counted, letting a user end
    up over their limit. The SELECT ... FOR UPDATE below locks the user's
    own row for the rest of this transaction, so a second concurrent
    request for the *same* user blocks until the first one commits or
    rolls back and then sees the up-to-date count -- it never affects
    other users' requests, which take out their own, independent lock.
    Postgres (production) enforces this; SQLite (the test suite) has no
    FOR UPDATE support and silently ignores it, which is harmless there
    since the test suite issues requests sequentially, never concurrently.
    """
    db.query(models.User).filter(models.User.id == user.id).with_for_update().first()

    if not can_perform_action(db, user, action, post=post):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=LIMIT_EXCEEDED_MESSAGE)


def get_usage_summary(db: Session, user: models.User) -> dict:
    """
    GET /subscriptions/usage: current usage vs. limit for every action, for
    `user` only -- there is no user_id parameter anywhere in this path, so
    it is not possible to request another user's usage through this
    function or its caller. A limit of None means unlimited (Pro): shown
    as JSON null, the same convention already used for max_posts/etc.
    everywhere else in this API (plan listings, /subscriptions/me), rather
    than a separate ad hoc "unlimited" sentinel.
    """
    plan = get_effective_plan(user)
    def metric(used: int, limit: int | None) -> dict:
        return {
            "used": used,
            "limit": limit,
            "remaining": None if limit is None else max(limit - used, 0),
        }

    return {
        "plan": plan.name if plan is not None else None,
        "usage": {
            "posts": metric(_current_usage(db, user, ACTION_CREATE_POST), plan.max_posts if plan else 0),
            "images": metric(_current_usage(db, user, ACTION_UPLOAD_IMAGE), plan.max_images if plan else 0),
            "likes": metric(_current_usage(db, user, ACTION_LIKE_POST), plan.max_likes if plan else 0),
            "comments": metric(_current_usage(db, user, ACTION_COMMENT_ON_POST), plan.max_comments if plan else 0),
        },
    }
