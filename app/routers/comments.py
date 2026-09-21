from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.routers.common import get_post_or_404
from app.schemas import CommentCreate, CommentResponse
from app.services import subscription as subscription_service
from app.services.notifications import send_comment_notification

router = APIRouter(prefix="/posts", tags=["comments"])


@router.post(
    "/{post_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a post",
    description="Adds a comment for the authenticated user, gated by their subscription plan's comment "
    "limit (Basic=5, Premium=25, Pro=unlimited; see GET /subscriptions/usage).",
    responses={
        201: {"description": "The created comment."},
        401: {"description": "Missing or invalid access token."},
        403: {
            "description": "The user's subscription plan's comment limit has been reached.",
            "content": {
                "application/json": {
                    "example": {"detail": subscription_service.LIMIT_EXCEEDED_MESSAGE}
                }
            },
        },
        404: {"description": "No post exists with this id."},
    },
)
def create_comment(
    post_id: int,
    comment_data: CommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = get_post_or_404(db, post_id)
    subscription_service.enforce_action_limit(db, current_user, subscription_service.ACTION_COMMENT_ON_POST)

    comment = models.Comment(
        post_id=post.id,
        user_id=current_user.id,
        text=comment_data.text,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    # Don't notify a user about their own comment on their own post.
    if post.author_id != current_user.id:
        send_comment_notification(
            post_owner_email=post.author.email,
            post_title=post.title,
            actor_username=current_user.username,
        )

    return comment


@router.get("/{post_id}/comments", response_model=list[CommentResponse])
def list_comments(post_id: int, db: Session = Depends(get_db)):
    get_post_or_404(db, post_id)
    return (
        db.query(models.Comment)
        .filter(models.Comment.post_id == post_id)
        .order_by(models.Comment.created_at, models.Comment.id)
        .all()
    )
