"""
Publishing-state changes for existing posts (PUT /posts/{id} with a
publish_option -- see app/schemas.py's PostUpdate). Creating a post sets
its initial state directly in app/routers/posts.py's create_post; this
module covers moving an existing post between draft / scheduled /
published.
"""
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app import models
from app.schemas import PUBLISH_NOW, SAVE_DRAFT, SCHEDULE

# Which publish_option each current status may be edited with.
#
# A published post can't be moved back to draft or scheduled: before
# scheduled publishing existed every post was public from creation, and the
# app has never had an "unpublish" (deleting is the only way to take a post
# down). Adding SAVE_DRAFT/SCHEDULE to the published entry is all it would
# take to allow that later. publish_now on an already-published post is
# accepted as a no-op so clients can resend it harmlessly.
ALLOWED_PUBLISH_OPTIONS: dict[str, frozenset[str]] = {
    models.POST_STATUS_DRAFT: frozenset({PUBLISH_NOW, SAVE_DRAFT, SCHEDULE}),
    models.POST_STATUS_SCHEDULED: frozenset({PUBLISH_NOW, SAVE_DRAFT, SCHEDULE}),
    models.POST_STATUS_PUBLISHED: frozenset({PUBLISH_NOW}),
}

UNPUBLISH_NOT_ALLOWED_MESSAGE = (
    "This post is already published and can't be moved back to draft or scheduled."
)


def ensure_publish_option_allowed(post: models.Post, publish_option: str) -> None:
    """Raises 409 before anything on the post is changed, so a rejected
    request never half-applies its title/content edits."""
    if publish_option not in ALLOWED_PUBLISH_OPTIONS.get(post.status, frozenset()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=UNPUBLISH_NOT_ALLOWED_MESSAGE)


def apply_publish_option(post: models.Post, publish_option: str, scheduled_at: datetime | None) -> None:
    """
    Sets status/scheduled_at/published_at for an already-allowed option.
    scheduled_at has been validated by PostUpdate: present, UTC and in the
    future for "schedule", None otherwise.
    """
    if publish_option == PUBLISH_NOW:
        if post.status != models.POST_STATUS_PUBLISHED:
            post.status = models.POST_STATUS_PUBLISHED
            # Database clock, same as created_at and a new post's published_at.
            post.published_at = func.now()
        # Already published: keep the original published_at.
        post.scheduled_at = None
    elif publish_option == SAVE_DRAFT:
        post.status = models.POST_STATUS_DRAFT
        post.scheduled_at = None
        post.published_at = None
    elif publish_option == SCHEDULE:
        post.status = models.POST_STATUS_SCHEDULED
        post.scheduled_at = scheduled_at
        post.published_at = None


# ---------------------------------------------------------------------------
# Public visibility
# ---------------------------------------------------------------------------
#
# The one rule for who may see a post, used by every public read
# (app/routers/posts.py's list/detail, app/routers/common.py's
# get_visible_post_or_404 for comments and likes):
#   - published: visible to everyone
#   - scheduled: visible to everyone once scheduled_at has passed, hidden
#     before that -- so visibility never depends on a background job having
#     flipped the status on time
#   - draft: never publicly visible
# The author can always see their own post (GET /posts/mine lists all of
# them regardless of status).


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(now: datetime | None) -> datetime:
    """Every comparison against scheduled_at uses an aware UTC instant: a
    naive value would be ambiguous, and SQLite compares the stored UTC text
    literally, so a "+05:30" now would be off by 5.5 hours there."""
    if now is None:
        return _utc_now()
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def publicly_visible_clause(now: datetime | None = None) -> ColumnElement[bool]:
    """SQL filter for posts anyone may see -- for list/search queries."""
    now = _as_utc(now)
    return or_(
        models.Post.status == models.POST_STATUS_PUBLISHED,
        and_(
            models.Post.status == models.POST_STATUS_SCHEDULED,
            models.Post.scheduled_at.is_not(None),
            models.Post.scheduled_at <= now,
        ),
    )


def is_publicly_visible(post: models.Post, now: datetime | None = None) -> bool:
    """Same rule as publicly_visible_clause, for a single loaded post."""
    if post.status == models.POST_STATUS_PUBLISHED:
        return True
    if post.status == models.POST_STATUS_SCHEDULED and post.scheduled_at is not None:
        scheduled_at = post.scheduled_at
        # SQLite returns naive datetimes; scheduled_at is always stored as UTC.
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        return scheduled_at <= _as_utc(now)
    return False


def can_view(post: models.Post, viewer: models.User | None) -> bool:
    return is_publicly_visible(post) or (viewer is not None and viewer.id == post.author_id)


# ---------------------------------------------------------------------------
# Automatic publishing of due scheduled posts
# ---------------------------------------------------------------------------


def publish_due_posts(db: Session, now: datetime | None = None) -> list[int]:
    """
    Publishes every scheduled post whose scheduled_at has passed, in one
    conditional UPDATE, and returns their ids. The caller commits (see
    app/services/scheduled_publishing.py's run_once).

    - Only rows still status='scheduled' AND scheduled_at <= now match, so
      drafts, future posts and already-published posts are never touched,
      and a post can be published only once: a second run (or a concurrent
      one, whose UPDATE re-checks the WHERE clause after the first commits)
      finds nothing left to change.
    - published_at is the time of this run; scheduled_at keeps the original
      scheduled time as a record of when it was meant to go live.
    - now is UTC, the same convention scheduled_at is stored in.
    """
    now = _as_utc(now)
    stmt = (
        update(models.Post)
        .where(
            models.Post.status == models.POST_STATUS_SCHEDULED,
            models.Post.scheduled_at.is_not(None),
            models.Post.scheduled_at <= now,
        )
        .values(status=models.POST_STATUS_PUBLISHED, published_at=now)
        .returning(models.Post.id)
        # Rows are changed in the database directly; any Post objects already
        # loaded in this session are expired on commit as usual.
        .execution_options(synchronize_session=False)
    )
    return sorted(row[0] for row in db.execute(stmt))
