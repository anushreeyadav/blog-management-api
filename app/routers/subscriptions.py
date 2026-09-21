import math
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import (
    BillingHistoryListResponse,
    CurrentSubscriptionResponse,
    SubscribeRequest,
    SubscribeResponse,
    SubscriptionChangeRequest,
    SubscriptionChangeResponse,
    SubscriptionPlanResponse,
    SubscriptionPlansResponse,
    SubscriptionResponse,
    SubscriptionUsageResponse,
)
from app.services import billing as billing_service
from app.services import invoices as invoice_pdf_service
from app.services import subscription as subscription_service

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def _get_requested_plan(db: Session, plan_id: int) -> models.SubscriptionPlan:
    plan = db.query(models.SubscriptionPlan).filter(models.SubscriptionPlan.id == plan_id).first()
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription plan not found")
    return plan


def _change_subscription(
    db: Session, current_user: models.User, plan: models.SubscriptionPlan
) -> tuple[models.Subscription, models.BillingHistory]:
    existing = subscription_service.get_active_subscription(db, current_user)
    if existing is not None and existing.plan_id == plan.id:
        latest_invoice = (
            db.query(models.BillingHistory)
            .filter(
                models.BillingHistory.user_id == current_user.id,
                models.BillingHistory.subscription_plan_id == plan.id,
            )
            .order_by(models.BillingHistory.created_at.desc())
            .first()
        )
        return existing, latest_invoice

    if existing is not None:
        existing.status = "canceled"
        existing.canceled_at = datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    new_subscription = models.Subscription(
        user_id=current_user.id,
        plan_id=plan.id,
        status="active",
        current_period_start=now,
        current_period_end=now + billing_service.subscription_duration(plan.billing_interval),
        auto_renew=True,
    )
    db.add(new_subscription)
    current_user.subscription_plan_id = plan.id
    db.commit()
    db.refresh(new_subscription)

    return new_subscription, billing_service.create_invoice(db, new_subscription)


@router.get(
    "/plans",
    response_model=SubscriptionPlansResponse,
    summary="List available subscription plans",
    description="Public endpoint -- no authentication required. Returns every currently active plan "
    "(Basic, Premium, Pro), cheapest first, so a client can look up a plan's `id` before calling "
    "POST /subscriptions/subscribe.",
    responses={200: {"description": "The list of active plans."}},
)
def list_plans(db: Session = Depends(get_db)):
    plans = (
        db.query(models.SubscriptionPlan)
        .filter(models.SubscriptionPlan.is_active.is_(True))
        .order_by(models.SubscriptionPlan.price)
        .all()
    )
    return {"plans": plans}


@router.post(
    "/subscribe",
    response_model=SubscribeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe to a plan",
    responses={
        201: {
            "description": "Now subscribed -- the new (or, if re-subscribing to the plan already active, "
            "the unchanged existing) subscription, plus its billing invoice."
        },
        401: {"description": "Missing or invalid access token."},
        404: {"description": "No active plan exists with the given plan_id."},
        422: {"description": "plan_id is missing/the wrong type, or the body contains any field other than plan_id."},
    },
)
def subscribe(
    request: SubscribeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Validates the requested plan, replaces any existing active Subscription
    with a new one, and immediately generates a paid invoice for it -- there
    is no real payment gateway here (this is a fake/simulated billing flow
    by design), so "subscribing" and "paying" happen in the same step.

    Safe against accidental duplicate requests: re-subscribing to the exact
    plan the user is already actively on (e.g. a double-click, or a client
    retrying after a dropped response) is a no-op that returns the existing
    subscription and its invoice, rather than canceling a subscription that
    was never really replaced and billing a second, redundant invoice for
    it. Subscribing to a *different* plan always proceeds normally --
    upgrades and plan changes still cancel the old Subscription and create
    a fresh billing record, per Sub-Task 12's requirement that plan changes
    preserve old billing history rather than delete or alter it.
    """
    plan = _get_requested_plan(db, request.plan_id)
    new_subscription, invoice = _change_subscription(db, current_user, plan)

    return SubscribeResponse(subscription=new_subscription, invoice=invoice)


@router.post(
    "/change",
    response_model=SubscriptionChangeResponse,
    summary="Change subscription plan",
    description="Switches the authenticated user to a different plan -- an upgrade or a downgrade. The "
    "previous Subscription is marked canceled (never deleted), its BillingHistory rows are preserved "
    "exactly as they were, and a fresh invoice is generated for the new plan.",
    responses={
        200: {"description": "The change succeeded -- a summary of the new subscription and its invoice."},
        401: {"description": "Missing or invalid access token."},
        404: {"description": "No active plan exists with the given plan_id."},
        422: {"description": "plan_id is missing/the wrong type, or the body contains any field other than plan_id."},
    },
)
def change_subscription(
    request: SubscriptionChangeRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    plan = _get_requested_plan(db, request.plan_id)
    subscription, invoice = _change_subscription(db, current_user, plan)

    return SubscriptionChangeResponse(
        message="Subscription changed successfully.",
        subscription={
            "plan": subscription.plan.name,
            "start_date": subscription.current_period_start,
            "end_date": subscription.current_period_end,
            "status": subscription.status,
        },
        billing={
            "transaction_id": invoice.transaction_id,
            "amount": invoice.amount,
            "invoice_url": invoice.invoice_path,
        },
    )


@router.get(
    "/me",
    response_model=CurrentSubscriptionResponse,
    summary="View current subscription",
    description="Returns the authenticated user's own current plan and subscription status -- there is no "
    "id parameter anywhere in this path, so another user's subscription can never be requested through "
    "this endpoint. Always resolvable: a user who has never called /subscribe is still governed by the "
    "default Basic plan and gets that back here, not a 404.",
    responses={
        200: {"description": "The caller's current plan and subscription status."},
        401: {"description": "Missing or invalid access token."},
    },
)
def read_my_subscription(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    plan = subscription_service.get_effective_plan(current_user)
    subscription = subscription_service.get_active_subscription(db, current_user)

    return CurrentSubscriptionResponse(
        user_id=current_user.id,
        plan={
            "id": plan.id,
            "name": plan.name,
            "price": plan.price,
            "max_posts": plan.max_posts,
            "max_images": plan.max_images,
            "max_likes": plan.max_likes,
            "max_comments": plan.max_comments,
        },
        start_date=subscription.current_period_start if subscription is not None else None,
        end_date=subscription.current_period_end if subscription is not None else None,
        status=subscription.status if subscription is not None else "default",
    )


@router.get(
    "/usage",
    response_model=SubscriptionUsageResponse,
    summary="View current plan usage",
    responses={
        200: {
            "description": "Current used/limit/remaining counts for posts, images, likes, and comments. "
            "limit and remaining are null for a plan (Pro) with no cap on that action."
        },
        401: {"description": "Missing or invalid access token."},
    },
)
def read_my_usage(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Current plan-limit usage for the authenticated user only -- there is no
    way to pass another user's id here, so this can never expose anyone
    else's usage. Reuses the same centralized counting logic
    (app/services/subscription.py's get_usage_summary) that actually
    enforces these limits on post/image/like/comment creation, so this is
    guaranteed to agree with what would or wouldn't be allowed right now.
    """
    return subscription_service.get_usage_summary(db, current_user)


@router.post(
    "/cancel",
    response_model=SubscriptionResponse,
    summary="Cancel current subscription",
    description="Cancels the authenticated user's active subscription and reverts them to the default Basic "
    "plan. The canceled Subscription row, and all of its billing history, is kept -- never deleted.",
    responses={
        200: {"description": "The now-canceled subscription."},
        401: {"description": "Missing or invalid access token."},
        404: {"description": "The user has no active subscription to cancel."},
    },
)
def cancel_subscription(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    subscription = subscription_service.get_active_subscription(db, current_user)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription")

    subscription.status = "canceled"
    subscription.auto_renew = False
    subscription.canceled_at = datetime.now(timezone.utc)
    current_user.subscription_plan_id = subscription_service.get_or_create_basic_plan(db).id
    db.commit()
    db.refresh(subscription)
    return subscription


@router.get(
    "/billing-history",
    response_model=BillingHistoryListResponse,
    summary="View billing history",
    responses={
        200: {"description": "A page of the caller's own billing records, newest first."},
        401: {"description": "Missing or invalid access token."},
    },
)
def read_my_billing_history(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(10, ge=1, le=100, description="Records per page (max 100)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Billing records belonging to the authenticated user only -- always
    filtered by current_user.id, so there is no way for a client to
    request (or leak into) another user's billing history through this
    endpoint. The admin-only, cross-user equivalent lives separately at
    GET /admin/billing-history (app/routers/admin.py).

    Paginated the same way GET /posts already is (page/limit/total/
    total_pages), per this app's one established pagination convention.
    """
    query = (
        db.query(models.BillingHistory)
        .filter(models.BillingHistory.user_id == current_user.id)
        .order_by(models.BillingHistory.created_at.desc())
    )
    total = query.count()
    offset = (page - 1) * limit
    records = query.offset(offset).limit(limit).all()
    total_pages = math.ceil(total / limit) if total else 0

    return {
        "billing_history": [
            {
                "id": record.id,
                "plan": record.plan.name,
                "transaction_id": record.transaction_id,
                "amount": record.amount,
                "start_date": record.start_date,
                "end_date": record.end_date,
                "status": record.status,
                "invoice_url": record.invoice_path,
                "created_at": record.created_at,
            }
            for record in records
        ],
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
    }


@router.get(
    "/billing/{billing_id}/invoice",
    summary="Download an invoice",
    responses={
        200: {
            "description": "The invoice PDF.",
            "content": {"application/pdf": {}},
        },
        401: {"description": "Missing or invalid access token."},
        404: {
            "description": "No billing record with this id belongs to the caller -- returned identically "
            "whether the id doesn't exist at all or belongs to another user, so this endpoint can never be "
            "used to enumerate other users' billing records by id.",
        },
    },
)
def download_invoice(
    billing_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Lets a user download their own invoice PDF. Steps run in the order the
    spec requires:

      1. Authenticate -- the get_current_user dependency (401 if missing
         or invalid).
      2. Retrieve the billing record by id.
      3. Verify it belongs to current_user.
      4. Verify the invoice file actually exists on disk.
      5. Return the PDF.

    Steps 2 and 3 are deliberately collapsed into one query+check that
    returns the *same* 404 whether the record doesn't exist at all or
    belongs to someone else -- a distinct "403 forbidden" would itself leak
    that a given billing_id belongs to another user, letting one user probe
    for the existence of another's billing records by id. This is the same
    reasoning already applied elsewhere in this API's subscription
    endpoints (see Sub-Task 18 / test_subscription_security.py) to avoid
    turning an ownership check into an enumeration oracle.

    Reuses the existing media/file-serving architecture rather than a new
    one: invoice_path is the same "/media/invoices/<file>" convention
    app/services/media.py established for post images, and INVOICES_DIR
    (app/services/invoices.py) is the same on-disk directory that
    convention resolves to. The public /media static mount (app/main.py)
    serves that directory unauthenticated, which is fine for post images
    but wrong for invoices, so this route serves the same files through an
    authenticated, ownership-checked path instead.
    """
    record = db.query(models.BillingHistory).filter(models.BillingHistory.id == billing_id).first()
    if record is None or record.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")

    if not record.invoice_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")

    # Only the basename is trusted, so a stored path can never escape
    # INVOICES_DIR via traversal segments (same rule app/services/media.py's
    # delete_post_image already applies to stored image paths).
    # invoice_pdf_service.INVOICES_DIR is read fresh here, not imported as a
    # bare name, so it honors the same tests/conftest.py monkeypatch that
    # redirects invoice generation to an isolated tmp directory.
    filename = Path(record.invoice_path).name
    file_path = invoice_pdf_service.INVOICES_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")

    return FileResponse(path=file_path, media_type="application/pdf", filename=filename)
