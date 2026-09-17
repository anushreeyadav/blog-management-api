"""
seed_default_plans() tests: creation, idempotency, and that existing rows
(including ones an admin has since edited) are never overwritten.

Mirrors the direct-model, isolated-in-memory-SQLite style of
test_subscription_plan_model.py.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from app import models
from app.database import Base
from app.services.plans import DEFAULT_PLAN_SLUGS, DEFAULT_PLANS, seed_default_plans


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    session = sessionmaker(bind=db_engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Running it once creates the plans
# ---------------------------------------------------------------------------


class TestFirstRunCreatesPlans:
    def test_creates_exactly_basic_premium_pro(self, db_session):
        result = seed_default_plans(db_session)

        assert set(result.keys()) == {"basic", "premium", "pro"}
        for slug, plan in result.items():
            assert plan.id is not None
            assert plan.slug == slug

    def test_plan_limits_match_the_spec(self, db_session):
        result = seed_default_plans(db_session)

        basic, premium, pro = result["basic"], result["premium"], result["pro"]

        assert basic.max_posts == 1 and basic.max_images == 1
        assert premium.max_posts == 2 and premium.max_images == 2
        assert premium.max_likes > basic.max_likes
        assert premium.max_comments > basic.max_comments
        assert pro.max_posts is None
        assert pro.max_images is None
        assert pro.max_likes is None
        assert pro.max_comments is None


# ---------------------------------------------------------------------------
# Verify the database contains exactly the required default plans
# ---------------------------------------------------------------------------


class TestDatabaseContainsExactlyTheDefaultPlans:
    def test_no_stray_plans_exist_after_seeding_an_empty_database(self, db_session):
        seed_default_plans(db_session)

        all_plans = db_session.query(models.SubscriptionPlan).all()
        assert {p.slug for p in all_plans} == set(DEFAULT_PLAN_SLUGS)
        assert len(all_plans) == len(DEFAULT_PLANS)


# ---------------------------------------------------------------------------
# Running it again does not create duplicates
# ---------------------------------------------------------------------------


class TestRerunningIsIdempotent:
    def test_second_run_does_not_duplicate_rows(self, db_session):
        seed_default_plans(db_session)
        seed_default_plans(db_session)

        count = db_session.query(models.SubscriptionPlan).count()
        assert count == len(DEFAULT_PLANS)

    def test_second_run_returns_the_same_row_ids(self, db_session):
        first_run = seed_default_plans(db_session)
        second_run = seed_default_plans(db_session)

        for slug in DEFAULT_PLAN_SLUGS:
            assert first_run[slug].id == second_run[slug].id

    def test_many_reruns_still_leave_exactly_three_plans(self, db_session):
        for _ in range(5):
            seed_default_plans(db_session)

        count = db_session.query(models.SubscriptionPlan).count()
        assert count == len(DEFAULT_PLANS)

    def test_unique_slug_constraint_would_reject_a_true_duplicate_insert(self, db_session):
        """
        Belt-and-suspenders: even if seed_default_plans' existence check were
        ever bypassed, the schema-level unique constraint on slug (added in
        Sub-Task 2) backstops it.
        """
        from sqlalchemy.exc import IntegrityError

        seed_default_plans(db_session)
        db_session.add(models.SubscriptionPlan(**DEFAULT_PLANS[0]))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ---------------------------------------------------------------------------
# Existing plan records must not be unnecessarily overwritten
# ---------------------------------------------------------------------------


class TestExistingPlansAreNeverOverwritten:
    def test_admin_edited_price_survives_a_reseed(self, db_session):
        seed_default_plans(db_session)

        basic = db_session.query(models.SubscriptionPlan).filter_by(slug="basic").one()
        basic.price = 3.49
        basic.max_posts = 10
        db_session.commit()

        seed_default_plans(db_session)

        db_session.refresh(basic)
        assert float(basic.price) == 3.49
        assert basic.max_posts == 10

    def test_deactivated_plan_stays_deactivated_after_reseed(self, db_session):
        seed_default_plans(db_session)

        pro = db_session.query(models.SubscriptionPlan).filter_by(slug="pro").one()
        pro.is_active = False
        db_session.commit()

        seed_default_plans(db_session)

        db_session.refresh(pro)
        assert pro.is_active is False

    def test_manually_pre_created_plan_is_left_alone(self, db_session):
        """
        A plan created some other way (e.g. by hand, or by a future admin
        endpoint) before the seeder ever runs should be adopted as-is, not
        replaced with the seeder's own row.
        """
        manual = models.SubscriptionPlan(
            name="Basic",
            slug="basic",
            price=0,
            billing_interval="month",
            max_posts=999,
            max_images=999,
            max_likes=999,
            max_comments=999,
            is_active=True,
        )
        db_session.add(manual)
        db_session.commit()
        manual_id = manual.id

        result = seed_default_plans(db_session)

        assert result["basic"].id == manual_id
        assert result["basic"].max_posts == 999
        assert db_session.query(models.SubscriptionPlan).filter_by(slug="basic").count() == 1
