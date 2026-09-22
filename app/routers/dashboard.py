from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import DashboardResponse
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "/me",
    response_model=DashboardResponse,
    summary="View your dashboard",
    description="Returns the authenticated user's own post/comment/like/view statistics, a per-post "
    "breakdown, and post-creation activity by day, all for charting. There is no user_id parameter "
    "anywhere in this path -- the caller is always resolved from the access token via get_current_user, "
    "so another user's dashboard (or their posts' analytics/activity) can never be requested by changing "
    "an id in the URL. Mirrors the same self-scoping already used by GET /subscriptions/me and GET /auth/me.",
    responses={
        200: {
            "description": "The caller's own dashboard: their id/username, their statistics, per-post "
            "analytics, and daily post-creation activity, all for their own posts only."
        },
        401: {"description": "Missing or invalid access token."},
    },
)
def read_my_dashboard(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return {
        "user": {"id": current_user.id, "username": current_user.username},
        "statistics": dashboard_service.get_dashboard_metrics(db, current_user),
        "post_analytics": dashboard_service.get_post_analytics(db, current_user),
        "post_activity": dashboard_service.get_post_activity(db, current_user),
    }
