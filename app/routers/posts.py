import math

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user, get_optional_current_user
from app.database import get_db
from app.routers.common import get_post_or_404, get_visible_post_or_404
from app.schemas import PaginatedPosts, PostCreate, PostResponse, PostUpdate
from app.services import media as media_service
from app.services import post_publishing as post_publishing_service
from app.services import subscription as subscription_service

router = APIRouter(prefix="/posts", tags=["posts"])


@router.post(
    "",
    response_model=PostResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a post",
    description="Creates a post for the authenticated user, gated by their subscription plan's post limit "
    "(see GET /subscriptions/usage and GET /subscriptions/plans -- Basic/Premium cap the number of posts, "
    "Pro is unlimited).\n\n"
    "publish_option chooses how the post is published: \"publish_now\" (the default -- status=published, "
    "published_at recorded now), \"save_draft\" (status=draft) or \"schedule\" (status=scheduled; requires "
    "a future, timezone-aware scheduled_at). Drafts and scheduled posts count toward the post limit too.",
    responses={
        201: {"description": "The created post, including its status, scheduled_at and published_at."},
        401: {"description": "Missing or invalid access token."},
        422: {"description": "Invalid title/content, unknown publish_option, or a missing/past scheduled_at."},
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

    # publish_option (app/schemas.py's PostCreate) has already been validated:
    # scheduled_at is set only for "schedule", and is then in the future.
    # published_at stays NULL here -- for a published post it's recorded at
    # insert time (app/models.py's _set_published_at_on_insert); drafts and
    # scheduled posts keep it NULL until they go live.
    post = models.Post(
        title=post_data.title,
        content=post_data.content,
        author_id=current_user.id,
        status=post_data.target_status,
        scheduled_at=post_data.scheduled_at,
        published_at=None,
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
    # Public feed: drafts and not-yet-due scheduled posts are excluded here,
    # before search/count/pagination, so total/total_pages only ever count
    # what a reader can actually see.
    query = db.query(models.Post).filter(post_publishing_service.publicly_visible_clause())
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


@router.get(
    "/{post_id}",
    response_model=PostResponse,
    description="Public. A draft, or a scheduled post before its scheduled_at, is returned only to its author "
    "(send the author's bearer token); anyone else gets the same 404 as for a post that doesn't exist.",
    responses={404: {"description": "No post exists with this id, or it isn't published yet."}},
)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    viewer: models.User | None = Depends(get_optional_current_user),
):
    post = get_visible_post_or_404(db, post_id, viewer)
    # An author previewing their own unpublished post isn't a reader view.
    if not post_publishing_service.is_publicly_visible(post):
        return post
    # The only place view_count changes (see app/models.py's Post.view_count
    # for the full policy: no dedup by visitor/session/IP, refreshes count
    # again). update_post/upload_post_image/delete_post below all use
    # get_post_or_404 directly rather than this handler, so none of them
    # count as a view of the post they're acting on.
    post.view_count += 1
    db.commit()
    db.refresh(post)
    return post


@router.put(
    "/{post_id}",
    response_model=PostResponse,
    description="Updates the caller's own post. title/content change only when sent. Optional publish_option "
    "changes the publishing state: \"publish_now\" (draft/scheduled -> published, published_at recorded now), "
    "\"save_draft\" (draft/scheduled -> draft) or \"schedule\" (draft/scheduled -> scheduled, or a new time for "
    "an already scheduled post; requires a future, timezone-aware scheduled_at). Without publish_option the "
    "status is left unchanged. A published post can't be moved back to draft or scheduled.",
    responses={
        401: {"description": "Missing or invalid access token."},
        403: {"description": "The caller doesn't own this post."},
        404: {"description": "No post exists with this id."},
        409: {
            "description": "The requested publish_option isn't allowed from the post's current status.",
            "content": {
                "application/json": {"example": {"detail": post_publishing_service.UNPUBLISH_NOT_ALLOWED_MESSAGE}}
            },
        },
        422: {"description": "Invalid title/content, unknown publish_option, or a missing/past scheduled_at."},
    },
)
def update_post(
    post_id: int,
    post_data: PostUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this post")
    # Checked before any field changes, so a rejected transition leaves the
    # post exactly as it was. No publish_option: status is left untouched.
    if post_data.publish_option is not None:
        # Re-read the row under a lock (SELECT ... FOR UPDATE on PostgreSQL)
        # so the scheduler can't publish it between this check and the
        # commit below -- otherwise e.g. "save_draft" on a post that had
        # just gone live would silently take it down again. The scheduler's
        # own UPDATE waits for this lock, then re-checks status='scheduled'.
        db.refresh(post, with_for_update=True)
        post_publishing_service.ensure_publish_option_allowed(post, post_data.publish_option)

    if post_data.title is not None:
        post.title = post_data.title
    if post_data.content is not None:
        post.content = post_data.content
    if post_data.publish_option is not None:
        post_publishing_service.apply_publish_option(post, post_data.publish_option, post_data.scheduled_at)

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
