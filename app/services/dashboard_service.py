"""
User-specific dashboard metrics.

Every function here takes `user` (the authenticated current_user resolved by
app/auth.py's get_current_user) and scopes its query to that user's own
data -- there is no user_id parameter anywhere in this module, so it is not
possible to compute, and therefore not possible to leak, another user's
statistics through it. This mirrors the same rule already applied to
GET /subscriptions/usage (see app/services/subscription.py).
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models


def get_total_posts(db: Session, user: models.User) -> int:
    """Posts authored by `user`."""
    return db.query(models.Post).filter(models.Post.author_id == user.id).count()


def get_total_comments_received(db: Session, user: models.User) -> int:
    """
    Comments left by anyone on posts `user` owns -- not comments `user` has
    written elsewhere. Mirrors get_total_likes_received's join shape:
    Comment.post_id -> Post.id, filtered to Post.author_id == user.id.
    """
    return (
        db.query(models.Comment)
        .join(models.Post, models.Comment.post_id == models.Post.id)
        .filter(models.Post.author_id == user.id)
        .count()
    )


def get_total_likes_received(db: Session, user: models.User) -> int:
    """
    Likes left by anyone on posts `user` owns -- not likes `user` has given
    out. Like.post_id -> Post.id, filtered to Post.author_id == user.id.
    """
    return (
        db.query(models.Like)
        .join(models.Post, models.Like.post_id == models.Post.id)
        .filter(models.Post.author_id == user.id)
        .count()
    )


def get_total_views(db: Session, user: models.User) -> int:
    """
    Sum of Post.view_count across every post `user` owns. Each view is
    counted once per GET /posts/{id} request, by anyone (see
    app/routers/posts.py) -- a running total, not a deduplicated
    unique-visitor count.
    """
    total = (
        db.query(func.coalesce(func.sum(models.Post.view_count), 0))
        .filter(models.Post.author_id == user.id)
        .scalar()
    )
    return int(total)


def get_dashboard_metrics(db: Session, user: models.User) -> dict:
    """
    All four dashboard metrics for `user`, in a single round trip.

    Each metric aggregates a different table (Post, Comment, Like) with a
    different join shape, so combining them into one query with real JOINs
    would multiply rows across tables and need DISTINCT/subquery correction
    anyway. Instead, each metric is built as its own independent scalar
    aggregate subquery (COUNT/SUM, same filtering as the standalone helpers
    above) and all four are selected together in one top-level statement --
    conceptually:

        SELECT (SELECT COUNT(*) FROM posts WHERE author_id = :uid),
               (SELECT COUNT(*) FROM comments JOIN posts ... WHERE author_id = :uid),
               (SELECT COUNT(*) FROM likes JOIN posts ... WHERE author_id = :uid),
               (SELECT COALESCE(SUM(view_count), 0) FROM posts WHERE author_id = :uid)

    One query, not four -- and never one per post/comment/like. No
    Post/Comment/Like row is loaded into Python at any point; every count
    and sum is computed by the database.
    """
    posts_subq = (
        db.query(func.count(models.Post.id)).filter(models.Post.author_id == user.id).scalar_subquery()
    )
    comments_subq = (
        db.query(func.count(models.Comment.id))
        .join(models.Post, models.Comment.post_id == models.Post.id)
        .filter(models.Post.author_id == user.id)
        .scalar_subquery()
    )
    likes_subq = (
        db.query(func.count(models.Like.id))
        .join(models.Post, models.Like.post_id == models.Post.id)
        .filter(models.Post.author_id == user.id)
        .scalar_subquery()
    )
    views_subq = (
        db.query(func.coalesce(func.sum(models.Post.view_count), 0))
        .filter(models.Post.author_id == user.id)
        .scalar_subquery()
    )

    total_posts, total_comments_received, total_likes_received, total_views = db.query(
        posts_subq, comments_subq, likes_subq, views_subq
    ).one()

    return {
        "total_posts": total_posts,
        "total_comments_received": total_comments_received,
        "total_likes_received": total_likes_received,
        "total_views": int(total_views),
    }


def get_post_analytics(db: Session, user: models.User) -> list[dict]:
    """
    Per-post like/comment/view counts for every post `user` owns, for
    frontend charts (see GET /dashboard/me). Post.author_id == user.id is
    applied before anything else, so a post belonging to another user can
    never appear in the result -- there is no way to request another
    user's per-post breakdown through this function.

    Three queries total (the post list, then one GROUP BY each for likes
    and comments), independent of how many posts/likes/comments exist --
    never one query per post. A single query joining Like and Comment onto
    Post directly was deliberately avoided: joining both at once would fan
    out each post's row once per (like, comment) pair, needing
    COUNT(DISTINCT ...) to correct back to the true counts -- two grouped
    subqueries merged in Python by post id are both simpler and cheaper.
    """
    posts = (
        db.query(models.Post.id, models.Post.title, models.Post.view_count)
        .filter(models.Post.author_id == user.id)
        .order_by(models.Post.id)
        .all()
    )
    if not posts:
        return []

    post_ids = [post_id for post_id, _title, _views in posts]

    likes_by_post = dict(
        db.query(models.Like.post_id, func.count(models.Like.id))
        .filter(models.Like.post_id.in_(post_ids))
        .group_by(models.Like.post_id)
        .all()
    )
    comments_by_post = dict(
        db.query(models.Comment.post_id, func.count(models.Comment.id))
        .filter(models.Comment.post_id.in_(post_ids))
        .group_by(models.Comment.post_id)
        .all()
    )

    return [
        {
            "post_id": post_id,
            "title": title,
            "likes": likes_by_post.get(post_id, 0),
            "comments": comments_by_post.get(post_id, 0),
            "views": view_count,
        }
        for post_id, title, view_count in posts
    ]


def get_post_activity(db: Session, user: models.User) -> list[dict]:
    """
    `user`'s post-creation count per calendar day, oldest first, for a
    frontend line chart (see GET /dashboard/me). Built from Post.created_at
    -- the existing timestamp every post already has -- and Post.author_id
    == user.id, so another user's post activity is never included.

    Grouped by func.date(Post.created_at) rather than a database-specific
    truncation function (e.g. Postgres's date_trunc): both this project's
    dev/test database (SQLite) and its production option (PostgreSQL, via
    DATABASE_URL -- see app/database.py) support a plain date(...) function
    call that reduces a timestamp to its calendar day, so one query works
    unchanged on either engine without a dialect branch. One query total,
    independent of how many posts exist -- never one per post.
    """
    day = func.date(models.Post.created_at)
    rows = (
        db.query(day.label("date"), func.count(models.Post.id))
        .filter(models.Post.author_id == user.id)
        .group_by(day)
        .order_by(day)
        .all()
    )
    return [{"date": day_value, "posts": count} for day_value, count in rows]
