from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import PostCreate, PostResponse, PostUpdate
from app.services import media as media_service

router = APIRouter(prefix="/posts", tags=["posts"])


def _get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    post_data: PostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = models.Post(
        title=post_data.title,
        content=post_data.content,
        author_id=current_user.id,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.get("", response_model=list[PostResponse])
def list_posts(db: Session = Depends(get_db)):
    return db.query(models.Post).order_by(models.Post.id).all()


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
    return _get_post_or_404(db, post_id)


@router.put("/{post_id}", response_model=PostResponse)
def update_post(
    post_id: int,
    post_data: PostUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this post")

    if post_data.title is not None:
        post.title = post_data.title
    if post_data.content is not None:
        post.content = post_data.content

    db.commit()
    db.refresh(post)
    return post


@router.post("/{post_id}/image", response_model=PostResponse)
async def upload_post_image(
    post_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this post")

    new_image_path = await media_service.save_post_image(image)
    old_image_path = post.image

    post.image = new_image_path
    db.commit()
    db.refresh(post)

    # Only remove the old file after the new path is safely committed, so a
    # failed commit never leaves the post pointing at a deleted image.
    media_service.delete_post_image(old_image_path)
    return post


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    if post.author_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this post")

    db.delete(post)
    db.commit()
    return None
