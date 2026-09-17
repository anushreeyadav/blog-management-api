from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.schemas import (
    BillingHistoryResponse,
    CurrentSubscriptionResponse,
    SubscribeRequest,
    SubscribeResponse,
    SubscriptionPlanResponse,
    SubscriptionResponse,
    SubscriptionUsageResponse,
)
from app.services import billing as billing_service
from app.services import subscription as subscription_service

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/plans", response_model=list[SubscriptionPlanResponse])
def list_plans(db: Session = Depends(get_db)):
    return (
        db.query(models.SubscriptionPlan)
        .filter(models.SubscriptionPlan.is_active.is_(True))
        .order_by(models.SubscriptionPlan.price)
        .all()
    )


@router.post("/subscribe", response_model=SubscribeResponse, status_code=status.HTTP_201_CREATED)
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
    plan = db.query(models.SubscriptionPlan).filter(models.SubscriptionPlan.id == request.plan_id).first()
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription plan not found")

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
        return SubscribeResponse(subscription=existing, invoice=latest_invoice)

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

    invoice = billing_service.create_invoice(db, new_subscription)

    return SubscribeResponse(subscription=new_subscription, invoice=invoice)


@router.get("/me", response_model=CurrentSubscriptionResponse)
def read_my_subscription(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    plan = subscription_service.get_effective_plan(current_user)
    subscription = subscription_service.get_active_subscription(db, current_user)

    return CurrentSubscriptionResponse(
        plan_id=plan.id,
        plan_name=plan.name,
        price=plan.price,
        max_posts=plan.max_posts,
        max_images=plan.max_images,
        max_likes=plan.max_likes,
        max_comments=plan.max_comments,
        subscription_status=subscription.status if subscription is not None else "default",
        current_period_start=subscription.current_period_start if subscription is not None else None,
        current_period_end=subscription.current_period_end if subscription is not None else None,
    )


@router.get("/usage", response_model=SubscriptionUsageResponse)
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


@router.post("/cancel", response_model=SubscriptionResponse)
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


@router.get("/billing-history", response_model=list[BillingHistoryResponse])
def read_my_billing_history(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return (
        db.query(models.BillingHistory)
        .filter(models.BillingHistory.user_id == current_user.id)
        .order_by(models.BillingHistory.created_at.desc())
        .all()
    )
