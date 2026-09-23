from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import NotificationListResponse, NotificationMarkAllReadResponse, NotificationResponse

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _get_owned_notification_or_404(db: Session, notification_id: int, user_id: int) -> models.Notification:
    """Shared by the mark-as-read and mark-as-unread endpoints below: the
    same 404 whether the id doesn't exist at all or belongs to another
    user, so ownership can never be probed by id."""
    notification = (
        db.query(models.Notification)
        .filter(models.Notification.id == notification_id, models.Notification.user_id == user_id)
        .first()
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return notification


@router.get(
    "/",
    response_model=NotificationListResponse,
    summary="List your notifications",
    description="Returns the authenticated user's own notifications, newest first, plus unread_count. There "
    "is no user_id parameter anywhere in this path -- the caller is always resolved from the access token "
    "via get_current_user, so another user's notifications can never be requested by changing an id in the "
    "URL. Mirrors the same self-scoping already used by GET /dashboard/me and GET /subscriptions/me.",
    responses={
        200: {"description": "The caller's own notifications and their total unread count."},
        401: {"description": "Missing or invalid access token."},
    },
)
def list_my_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notifications = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == current_user.id)
        .order_by(models.Notification.created_at.desc(), models.Notification.id.desc())
        .all()
    )
    unread_count = sum(1 for n in notifications if not n.is_read)

    return {"notifications": notifications, "unread_count": unread_count}


@router.patch(
    "/{notification_id}/read",
    response_model=NotificationResponse,
    summary="Mark a notification as read",
    description="Marks one of the authenticated user's own notifications as read. Returns the same 404 "
    "whether no notification exists with this id or it belongs to another user, so this endpoint can never "
    "be used to enumerate other users' notifications by id -- the same reasoning already applied to "
    "GET /subscriptions/billing/{billing_id}/invoice.",
    responses={
        200: {"description": "The notification, now marked as read."},
        401: {"description": "Missing or invalid access token."},
        404: {
            "description": "No notification with this id belongs to the caller -- returned identically "
            "whether the id doesn't exist at all or belongs to another user."
        },
    },
)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notification = _get_owned_notification_or_404(db, notification_id, current_user.id)

    notification.is_read = True
    db.commit()
    db.refresh(notification)
    return notification


@router.patch(
    "/{notification_id}/unread",
    response_model=NotificationResponse,
    summary="Mark a notification as unread",
    description="Marks one of the authenticated user's own notifications as unread -- the inverse of "
    "PATCH /notifications/{notification_id}/read. Returns the same 404 whether no notification exists with "
    "this id or it belongs to another user, so this endpoint can never be used to enumerate other users' "
    "notifications by id.",
    responses={
        200: {"description": "The notification, now marked as unread."},
        401: {"description": "Missing or invalid access token."},
        404: {
            "description": "No notification with this id belongs to the caller -- returned identically "
            "whether the id doesn't exist at all or belongs to another user."
        },
    },
)
def mark_notification_unread(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    notification = _get_owned_notification_or_404(db, notification_id, current_user.id)

    notification.is_read = False
    db.commit()
    db.refresh(notification)
    return notification


@router.patch(
    "/read-all",
    response_model=NotificationMarkAllReadResponse,
    summary="Mark all your notifications as read",
    description="Marks every currently-unread notification belonging to the authenticated user as read. "
    "Scoped to the caller only via get_current_user, the same as every other endpoint on this router -- "
    "another user's notifications are never touched, regardless of how many of the caller's own are "
    "unread (including zero).",
    responses={
        200: {"description": "How many notifications were updated (0 if none were unread)."},
        401: {"description": "Missing or invalid access token."},
    },
)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    unread = (
        db.query(models.Notification)
        .filter(models.Notification.user_id == current_user.id, models.Notification.is_read.is_(False))
        .all()
    )
    for notification in unread:
        notification.is_read = True
    db.commit()

    return {"message": "All notifications marked as read", "updated_count": len(unread)}
