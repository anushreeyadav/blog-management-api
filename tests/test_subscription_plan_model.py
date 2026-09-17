"""
SubscriptionPlan model tests.

Mirrors the direct-model, isolated-in-memory-SQLite style of
test_database.py: these exercise the ORM model and the real schema-level
constraints (via SQLite's own enforcement, not just application checks),
rather than going through the HTTP API.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from app import models
from app.database import Base


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


def _make_plan(**overrides) -> models.SubscriptionPlan:
    defaults = dict(
        name="Basic",
        slug="basic",
        price=4.99,
        billing_interval="month",
        max_posts=1,
        max_images=1,
        max_likes=5,
        max_comments=5,
        is_active=True,
    )
    defaults.update(overrides)
    return models.SubscriptionPlan(**defaults)


# ---------------------------------------------------------------------------
# All three plans can be stored
# ---------------------------------------------------------------------------


class TestAllThreePlansCanBeStored:
    def test_basic_premium_pro_all_persist(self, db_session):
        basic = _make_plan(name="Basic", slug="basic", price=4.99, max_posts=1, max_images=1, max_likes=5, max_comments=5)
        premium = _make_plan(
            name="Premium", slug="premium", price=9.99, max_posts=2, max_images=2, max_likes=25, max_comments=25
        )
        pro = _make_plan(
            name="Pro", slug="pro", price=19.99, max_posts=None, max_images=None, max_likes=None, max_comments=None
        )
        db_session.add_all([basic, premium, pro])
        db_session.commit()

        names = {row.name for row in db_session.query(models.SubscriptionPlan).all()}
        assert names == {"Basic", "Premium", "Pro"}
        assert basic.id is not None
        assert premium.id is not None
        assert pro.id is not None


# ---------------------------------------------------------------------------
# Plan names are unique
# ---------------------------------------------------------------------------


class TestPlanNameUniqueness:
    def test_duplicate_plan_name_rejected(self, db_session):
        db_session.add(_make_plan(name="Basic", slug="basic-1"))
        db_session.commit()

        db_session.add(_make_plan(name="Basic", slug="basic-2"))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        count = db_session.query(models.SubscriptionPlan).filter_by(name="Basic").count()
        assert count == 1

    def test_duplicate_plan_slug_rejected(self, db_session):
        db_session.add(_make_plan(name="Basic A", slug="basic"))
        db_session.commit()

        db_session.add(_make_plan(name="Basic B", slug="basic"))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_unique_index_exists_on_name(self, db_engine):
        cur = db_engine.raw_connection().cursor()
        cur.execute("PRAGMA index_list(subscription_plans)")
        found = False
        for row in cur.fetchall():
            index_name, is_unique = row[1], row[2]
            if not is_unique:
                continue
            cur.execute(f"PRAGMA index_info({index_name})")
            cols = {r[2] for r in cur.fetchall()}
            if cols == {"name"}:
                found = True
        assert found


# ---------------------------------------------------------------------------
# Limits are stored correctly
# ---------------------------------------------------------------------------


class TestLimitsStoredCorrectly:
    def test_basic_plan_limits(self, db_session):
        db_session.add(_make_plan(name="Basic", slug="basic", max_posts=1, max_images=1, max_likes=5, max_comments=5))
        db_session.commit()

        plan = db_session.query(models.SubscriptionPlan).filter_by(name="Basic").one()
        assert plan.max_posts == 1
        assert plan.max_images == 1
        assert plan.max_likes == 5
        assert plan.max_comments == 5

    def test_premium_plan_limits(self, db_session):
        db_session.add(
            _make_plan(
                name="Premium", slug="premium", price=9.99, max_posts=2, max_images=2, max_likes=25, max_comments=25
            )
        )
        db_session.commit()

        plan = db_session.query(models.SubscriptionPlan).filter_by(name="Premium").one()
        assert plan.max_posts == 2
        assert plan.max_images == 2
        assert plan.max_likes == 25
        assert plan.max_comments == 25

    def test_price_and_billing_interval_round_trip(self, db_session):
        db_session.add(_make_plan(name="Basic", slug="basic", price=4.99, billing_interval="month"))
        db_session.commit()

        plan = db_session.query(models.SubscriptionPlan).filter_by(name="Basic").one()
        assert float(plan.price) == 4.99
        assert plan.billing_interval == "month"

    def test_created_at_and_updated_at_are_set(self, db_session):
        db_session.add(_make_plan(name="Basic", slug="basic"))
        db_session.commit()

        plan = db_session.query(models.SubscriptionPlan).filter_by(name="Basic").one()
        assert plan.created_at is not None
        assert plan.updated_at is not None

    def test_updated_at_changes_on_update(self, db_session):
        plan = _make_plan(name="Basic", slug="basic", price=4.99)
        db_session.add(plan)
        db_session.commit()
        first_updated_at = plan.updated_at

        plan.price = 5.99
        db_session.commit()
        db_session.refresh(plan)

        assert plan.updated_at >= first_updated_at


# ---------------------------------------------------------------------------
# Unlimited Pro limits work correctly
# ---------------------------------------------------------------------------


class TestUnlimitedProLimits:
    def test_pro_plan_limits_are_null(self, db_session):
        db_session.add(
            _make_plan(
                name="Pro", slug="pro", price=19.99, max_posts=None, max_images=None, max_likes=None, max_comments=None
            )
        )
        db_session.commit()

        plan = db_session.query(models.SubscriptionPlan).filter_by(name="Pro").one()
        assert plan.max_posts is None
        assert plan.max_images is None
        assert plan.max_likes is None
        assert plan.max_comments is None

    def test_null_limit_is_stored_as_null_at_the_database_level(self, db_engine, db_session):
        db_session.add(_make_plan(name="Pro", slug="pro", max_posts=None, max_images=None))
        db_session.commit()

        cur = db_engine.raw_connection().cursor()
        cur.execute("SELECT max_posts, max_images FROM subscription_plans WHERE name = 'Pro'")
        row = cur.fetchone()
        assert row == (None, None)
