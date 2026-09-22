import math

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.routers.common import get_post_or_404
from app.schemas import PaginatedPosts, PostCreate, PostResponse, PostUpdate
from app.services import media as media_service
from app.services import subscription as subscription_service

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post(
    "",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a post",
    description="Creates a post for the authenticated user, gated by their subscription plan's post limit "
    "(see GET /subscriptions/usage and GET /subscriptions/plans -- Basic/Premium cap the number of posts, "
    "Pro is unlimited).",
    responses={
        201: {"description": "The created post."},
        401: {"description": "Missing or invalid access token."},
        403: {
            "description": "The user's subscription plan's post limit has been reached.",
            "content": {
                "application/json": {
                    "example": {"detail": subscription_service.LIMIT_EXCEEDED_MESSAGE}
                }
            },
        },
    },
)
def create_post(
    post_data: PostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    subscription_service.enforce_action_limit(db, current_user, subscription_service.ACTION_CREATE_POST)

    post = models.Post(
        title=post_data.title,
        content=post_data.content,
        author_id=current_user.id,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.get("", response_model=PaginatedPosts)
def list_posts(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(10, ge=1, le=100, description="Posts per page (max 100)"),
    search: str | None = Query(None, description="Case-insensitive match against post title or content"),
    db: Session = Depends(get_db),
):
    query = db.query(models.Post)
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(models.Post.title.ilike(pattern), models.Post.content.ilike(pattern)))

    total = query.count()
    offset = (page - 1) * limit
    items = query.order_by(models.Post.id).offset(offset).limit(limit).all()
    total_pages = math.ceil(total / limit) if total else 0

    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/mine", response_model=list[PostResponse])
def list_my_posts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return (
        db.query(models.Post)
        .filter(models.Post.author_id == current_user.id)
        .order_by(models.Post.id)
        .all()
    )


@router.get("/{post_id}", response_model=PostResponse)
def get_post(post_id: int, db: Session = Depends(get_db)):
    post = get_post_or_404(db, post_id)
    # The only place view_count changes (see app/models.py's Post.view_count
    # for the full policy: no dedup by visitor/session/IP, refreshes count
    # again). update_post/upload_post_image/delete_post below all use
    # get_post_or_404 directly rather than this handler, so none of them
    # count as a view of the post they're acting on.
    post.view_count += 1
    db.commit()
    db.refresh(post)
    return post


@router.put("/{post_id}", response_model=PostResponse)
def update_post(
    post_id: int,
    post_data: PostUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this post")

    if post_data.title is not None:
        post.title = post_data.title
    if post_data.content is not None:
        post.content = post_data.content

    db.commit()
    db.refresh(post)
    return post


@router.post(
    "/{post_id}/image",
    response_model=PostResponse,
    summary="Upload a post image",
    description="Adds an image to the post's gallery, gated by the owner's subscription plan's per-post "
    "image limit (Basic=1, Premium=2, Pro=unlimited -- see GET /subscriptions/usage).",
    responses={
        200: {"description": "The post, with the new image added to its gallery."},
        401: {"description": "Missing or invalid access token."},
        403: {
            "description": "Either the caller doesn't own this post, or its owner's subscription plan's "
            "image limit for this post has been reached.",
            "content": {
                "application/json": {
                    "example": {"detail": subscription_service.LIMIT_EXCEEDED_MESSAGE}
                }
            },
        },
        404: {"description": "No post exists with this id."},
        413: {"description": "The uploaded image exceeds the maximum allowed size."},
    },
)
async def upload_post_image(
    post_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this post")

    # Below the plan's per-post image limit: add a new image to the post's
    # gallery (Basic=1, Premium=2, Pro=unlimited -- see
    # app/services/subscription.py). Post.image (singular) is kept in sync
    # to the most recent upload so existing single-image consumers see the
    # same field, behaving the same way, as before.
    subscription_service.enforce_action_limit(db, current_user, subscription_service.ACTION_UPLOAD_IMAGE, post=post)

    new_image_path = await media_service.save_post_image(image)

    db.add(models.PostImage(post_id=post.id, image=new_image_path))
    post.image = new_image_path
    db.commit()
    db.refresh(post)
    return post


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this post")

    db.delete(post)
    db.commit()
    return None
