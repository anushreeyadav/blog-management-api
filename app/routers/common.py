from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app import models
from app.services import post_publishing


def get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


def get_visible_post_or_404(db: Session, post_id: int, viewer: models.User | None) -> models.Post:
    """
    Like get_post_or_404, but a draft or not-yet-due scheduled post is
    reported as 404 to anyone but its author -- the same response as a post
    that doesn't exist, so its existence isn't revealed either. See
    app/services/post_publishing.py for the visibility rule.
    """
    post = get_post_or_404(db, post_id)
    if not post_publishing.can_view(post, viewer):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post
