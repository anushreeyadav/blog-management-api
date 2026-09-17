"""
BillingHistory model tests.

Mirrors the direct-model, isolated-in-memory-SQLite style of
test_database.py / test_subscription_plan_model.py: these exercise the ORM
model and the real schema-level constraints (via SQLite's own enforcement),
rather than going through the HTTP API.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from app import models
from app.auth import hash_password
from app.database import Base
from app.services import subscription as subscription_service


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


@pytest.fixture()
def user(db_session):
    basic_plan = subscription_service.get_or_create_basic_plan(db_session)
    u = models.User(
        username="billing_user",
        email="billing_user@example.com",
        password_hash=hash_password("Password123"),
        subscription_plan_id=basic_plan.id,
    )
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def plan(db_session):
    # A distinct slug from the default Basic/Premium/Pro plans -- the user
    # fixture above already seeds those (via get_or_create_basic_plan), and
    # this fixture only needs *some* plan to attach BillingHistory rows to.
    p = models.SubscriptionPlan(
        name="Test Plan",
        slug="billing-test-plan",
        price=9.99,
        billing_interval="month",
        max_posts=2,
        max_images=2,
        max_likes=25,
        max_comments=25,
        is_active=True,
    )
    db_session.add(p)
    db_session.commit()
    return p


def _make_record(user_id: int, plan_id: int, **overrides) -> models.BillingHistory:
    now = datetime.now(timezone.utc)
    defaults = dict(
        user_id=user_id,
        subscription_plan_id=plan_id,
        transaction_id="TXN-2026-0000000001",
        amount=9.99,
        start_date=now,
        end_date=now + timedelta(days=30),
        status="paid",
        invoice_path="/invoices/TXN-2026-0000000001.pdf",
    )
    defaults.update(overrides)
    return models.BillingHistory(**defaults)


# ---------------------------------------------------------------------------
# Billing record creation
# ---------------------------------------------------------------------------


class TestBillingRecordCreation:
    def test_record_persists_with_all_fields(self, db_session, user, plan):
        record = _make_record(user.id, plan.id)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.id is not None
        assert record.created_at is not None

    def test_user_can_have_multiple_billing_records(self, db_session, user, plan):
        db_session.add(_make_record(user.id, plan.id, transaction_id="TXN-A"))
        db_session.add(_make_record(user.id, plan.id, transaction_id="TXN-B"))
        db_session.commit()

        records = db_session.query(models.BillingHistory).filter_by(user_id=user.id).all()
        assert len(records) == 2


# ---------------------------------------------------------------------------
# User relationship
# ---------------------------------------------------------------------------


class TestUserRelationship:
    def test_record_references_the_correct_user(self, db_session, user, plan):
        record = _make_record(user.id, plan.id)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.user_id == user.id
        assert record.user.username == "billing_user"

    def test_user_billing_history_backref(self, db_session, user, plan):
        record = _make_record(user.id, plan.id)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(user)

        assert len(user.billing_history) == 1
        assert user.billing_history[0].id == record.id

    def test_billing_history_requires_a_valid_user(self, db_session, plan):
        record = _make_record(user_id=999999, plan_id=plan.id)
        db_session.add(record)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ---------------------------------------------------------------------------
# Plan relationship
# ---------------------------------------------------------------------------


class TestPlanRelationship:
    def test_record_references_the_correct_plan(self, db_session, user, plan):
        record = _make_record(user.id, plan.id)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.subscription_plan_id == plan.id
        assert record.plan.name == "Test Plan"

    def test_plan_billing_history_backref(self, db_session, user, plan):
        record = _make_record(user.id, plan.id)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(plan)

        assert len(plan.billing_history) == 1
        assert plan.billing_history[0].id == record.id

    def test_billing_history_requires_a_valid_plan(self, db_session, user):
        record = _make_record(user_id=user.id, plan_id=999999)
        db_session.add(record)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ---------------------------------------------------------------------------
# Unique transaction ID
# ---------------------------------------------------------------------------


class TestUniqueTransactionId:
    def test_duplicate_transaction_id_rejected(self, db_session, user, plan):
        db_session.add(_make_record(user.id, plan.id, transaction_id="TXN-DUPLICATE"))
        db_session.commit()

        db_session.add(_make_record(user.id, plan.id, transaction_id="TXN-DUPLICATE"))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        count = db_session.query(models.BillingHistory).filter_by(transaction_id="TXN-DUPLICATE").count()
        assert count == 1

    def test_unique_index_exists_on_transaction_id(self, db_engine):
        cur = db_engine.raw_connection().cursor()
        cur.execute("PRAGMA index_list(billing_history)")
        found = False
        for row in cur.fetchall():
            index_name, is_unique = row[1], row[2]
            if not is_unique:
                continue
            cur.execute(f"PRAGMA index_info({index_name})")
            cols = {r[2] for r in cur.fetchall()}
            if cols == {"transaction_id"}:
                found = True
        assert found


# ---------------------------------------------------------------------------
# Invoice path storage
# ---------------------------------------------------------------------------


class TestInvoicePathStorage:
    def test_invoice_path_is_stored(self, db_session, user, plan):
        record = _make_record(user.id, plan.id, invoice_path="/invoices/TXN-2026-0000000001.pdf")
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.invoice_path == "/invoices/TXN-2026-0000000001.pdf"

    def test_invoice_path_is_optional(self, db_session, user, plan):
        record = _make_record(user.id, plan.id, transaction_id="TXN-NO-PATH", invoice_path=None)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.invoice_path is None


# ---------------------------------------------------------------------------
# Billing status
# ---------------------------------------------------------------------------


class TestBillingStatus:
    @pytest.mark.parametrize("status_value", ["paid", "failed", "refunded"])
    def test_status_round_trips(self, db_session, user, plan, status_value):
        record = _make_record(user.id, plan.id, transaction_id=f"TXN-{status_value}", status=status_value)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        assert record.status == status_value

    def test_amount_and_dates_round_trip(self, db_session, user, plan):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        record = _make_record(user.id, plan.id, amount=19.99, start_date=start, end_date=end)
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)

        # SQLite drops tzinfo on round-trip (a SQLite/SQLAlchemy quirk, not
        # an app bug), so compare naive values same as test_database.py does.
        assert float(record.amount) == 19.99
        assert record.start_date.replace(tzinfo=None) == start.replace(tzinfo=None)
        assert record.end_date.replace(tzinfo=None) == end.replace(tzinfo=None)
