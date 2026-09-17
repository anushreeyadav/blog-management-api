"""
Fake billing/transaction generation for subscription purchases.

There is no real payment gateway integration here (no Stripe or similar) --
subscribing "pays" immediately, and every subscribe/renew produces exactly
one BillingHistory row as the invoice/receipt record, backed by a real
generated PDF (see app/services/invoices.py). transaction_id uses a uuid
suffix (the same approach app/services/media.py uses for stored filenames)
so concurrent invoice creation can never collide, unlike a counted sequence
would.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import models
from app.services import invoices as invoice_pdf_service

load_dotenv()

# Subscription duration is configurable (see .env) rather than hard-coded,
# with a sensible 30-day default -- matches how auth.py already reads
# ACCESS_TOKEN_EXPIRE_MINUTES the same way.
DEFAULT_SUBSCRIPTION_DURATION_DAYS = int(os.getenv("SUBSCRIPTION_DURATION_DAYS", "30"))
ANNUAL_SUBSCRIPTION_DURATION_DAYS = int(os.getenv("ANNUAL_SUBSCRIPTION_DURATION_DAYS", "365"))


def subscription_duration(billing_interval: str) -> timedelta:
    days = ANNUAL_SUBSCRIPTION_DURATION_DAYS if billing_interval == "year" else DEFAULT_SUBSCRIPTION_DURATION_DAYS
    return timedelta(days=days)


def _generate_transaction_id() -> str:
    year = datetime.now(timezone.utc).year
    return f"TXN-{year}-{uuid.uuid4().hex[:10].upper()}"


def create_invoice(db: Session, subscription: models.Subscription) -> models.BillingHistory:
    transaction_id = _generate_transaction_id()
    amount = subscription.plan.price
    status = "paid"

    invoice_path = invoice_pdf_service.generate_invoice_pdf(
        transaction_id=transaction_id,
        user=subscription.user,
        plan=subscription.plan,
        amount=amount,
        status=status,
        start_date=subscription.current_period_start,
        end_date=subscription.current_period_end,
    )

    invoice = models.BillingHistory(
        user_id=subscription.user_id,
        subscription_plan_id=subscription.plan_id,
        transaction_id=transaction_id,
        amount=amount,
        start_date=subscription.current_period_start,
        end_date=subscription.current_period_end,
        status=status,
        invoice_path=invoice_path,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice
