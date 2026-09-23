from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.routers.common import get_post_or_404
from app.schemas import LikeResponse
from app.services import subscription as subscription_service
from app.services.notifications import create_notification, send_like_notification

router = APIRouter(prefix="/posts", tags=["likes"])


def _get_like(db: Session, post_id: int, user_id: int) -> models.Like | None:
    return (
        db.query(models.Like)
        .filter(models.Like.post_id == post_id, models.Like.user_id == user_id)
        .first()
    )


@router.post(
    "/{post_id}/like",
    response_model=LikeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Like a post",
    description="Likes a post for the authenticated user, gated by their subscription plan's like limit "
    "(current active likes -- Basic=5, Premium=25, Pro=unlimited; see GET /subscriptions/usage).",
    responses={
        201: {"description": "The created like."},
        401: {"description": "Missing or invalid access token."},
        403: {
            "description": "The user's subscription plan's like limit has been reached.",
            "content": {
                "application/json": {
                    "example": {"detail": subscription_service.LIMIT_EXCEEDED_MESSAGE}
                }
            },
        },
        404: {"description": "No post exists with this id."},
        409: {"description": "The user has already liked this post."},
    },
)
def like_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = get_post_or_404(db, post_id)

    if _get_like(db, post_id, current_user.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Post already liked")

    subscription_service.enforce_action_limit(db, current_user, subscription_service.ACTION_LIKE_POST)

    like = models.Like(post_id=post_id, user_id=current_user.id)
    db.add(like)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request won the race and inserted the same
        # (post_id, user_id) pair first; the unique constraint caught it.
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Post already liked")

    db.refresh(like)

    # Don't notify a user about their own like on their own post.
    if post.author_id != current_user.id:
        send_like_notification(
            post_owner_email=post.author.email,
            post_title=post.title,
            actor_username=current_user.username,
        )
        create_notification(
            db,
            user_id=post.author_id,
            message=f"{current_user.username} liked your post '{post.title}'.",
            notification_type="like",
        )
        db.commit()

    return like


@router.delete("/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
def unlike_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    get_post_or_404(db, post_id)

    like = _get_like(db, post_id, current_user.id)
    if like is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Like not found")

    db.delete(like)
    db.commit()
    return None
