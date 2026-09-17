"""
Canonical definitions for the three default subscription plans, and an
idempotent seed function that ensures they exist in the database.

This is the single source of truth for Basic/Premium/Pro's field values.
app/services/subscription.py's registration-time plan assignment and this
seeder both read from DEFAULT_PLANS, so there is exactly one place that can
drift -- unlike duplicating the same literal values in two files. The
Alembic migration that originally seeded these rows
(alembic/versions/6c0e04dc7cba_...py) intentionally keeps its own inline
copy instead of importing this module: a migration is a frozen historical
snapshot and must keep working even if these defaults change later.

Run standalone with `python -m app.services.plans` to seed a database
outside the app's own startup (see seed_default_plans below for how it's
wired into that startup).
"""

from sqlalchemy.orm import Session

from app import models

DEFAULT_PLANS: list[dict] = [
    dict(
        name="Basic",
        slug="basic",
        price=4.99,
        billing_interval="month",
        max_posts=1,
        max_images=1,
        max_likes=5,
        max_comments=5,
        is_active=True,
    ),
    dict(
        name="Premium",
        slug="premium",
        price=9.99,
        billing_interval="month",
        max_posts=2,
        max_images=2,
        max_likes=25,
        max_comments=25,
        is_active=True,
    ),
    dict(
        name="Pro",
        slug="pro",
        price=19.99,
        billing_interval="month",
        max_posts=None,
        max_images=None,
        max_likes=None,
        max_comments=None,
        is_active=True,
    ),
]

DEFAULT_PLAN_SLUGS = [plan["slug"] for plan in DEFAULT_PLANS]


def seed_default_plans(db: Session) -> dict[str, models.SubscriptionPlan]:
    """
    Ensures Basic, Premium, and Pro all exist, matched by slug.

    Idempotent: a plan that already exists -- including one an admin has
    since edited -- is returned as-is and never overwritten back to these
    defaults; only genuinely missing plans are inserted. Safe to call on
    every app startup, or any number of times in a row.

    Returns {slug: SubscriptionPlan} for all three, whether they already
    existed or were just created.
    """
    existing = (
        db.query(models.SubscriptionPlan)
        .filter(models.SubscriptionPlan.slug.in_(DEFAULT_PLAN_SLUGS))
        .all()
    )
    plans_by_slug = {plan.slug: plan for plan in existing}

    created_any = False
    for plan_data in DEFAULT_PLANS:
        if plan_data["slug"] in plans_by_slug:
            continue
        plan = models.SubscriptionPlan(**plan_data)
        db.add(plan)
        plans_by_slug[plan_data["slug"]] = plan
        created_any = True

    if created_any:
        db.commit()
        for plan in plans_by_slug.values():
            db.refresh(plan)

    return plans_by_slug


if __name__ == "__main__":
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        seeded = seed_default_plans(session)
        for slug, plan in sorted(seeded.items()):
            print(f"{plan.name} ({slug}): id={plan.id} price={plan.price}")
    finally:
        session.close()
