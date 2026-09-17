from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_admin_user
from app.database import get_db
from app.schemas import BillingHistoryResponse, SubscriptionPlanResponse, SubscriptionResponse

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_current_admin_user)])


@router.get("/plans", response_model=list[SubscriptionPlanResponse])
def list_all_plans(db: Session = Depends(get_db)):
    return db.query(models.SubscriptionPlan).order_by(models.SubscriptionPlan.price).all()


@router.get("/subscriptions", response_model=list[SubscriptionResponse])
def list_all_subscriptions(db: Session = Depends(get_db)):
    return db.query(models.Subscription).order_by(models.Subscription.id).all()


@router.get("/billing-history", response_model=list[BillingHistoryResponse])
def list_all_billing_history(db: Session = Depends(get_db)):
    return db.query(models.BillingHistory).order_by(models.BillingHistory.created_at.desc()).all()
