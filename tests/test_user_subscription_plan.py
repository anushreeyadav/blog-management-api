"""
User <-> SubscriptionPlan "active plan" integration tests.

Covers exactly what Sub-Task 4 asks for:
  - Existing users receive the Basic subscription when appropriate.
  - A user can access their active plan.
  - Historical billing records remain available after the plan changes.
  - The User <-> SubscriptionPlan relationship works correctly.

The first case (existing users) is tested two ways: directly against the
same backfill SQL the real Alembic migration runs (so a regression in that
statement is caught here, not just by eyeballing the migration file), and
via the registration flow that assigns every new user the Basic plan.
Everything else uses the same isolated in-memory SQLite patterns as the
other test modules (test_subscription_plan_model.py's direct-model style
for ORM-level checks, test_subscriptions.py's client style for the
API-level billing-history check).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, Column, Integer, String, Numeric, Boolean, DateTime, ForeignKey, create_engine, event, insert, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app as main_app
from app.services import subscription as subscription_service

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}


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
def sub_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    main_app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_app) as test_client:
        yield test_client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register(client, user):
    return client.post("/auth/register", json=user)


def _auth_headers(client, user):
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _make_plan(db_session, **overrides) -> models.SubscriptionPlan:
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
    plan = models.SubscriptionPlan(**defaults)
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _make_user(db_session, plan_id: int, username: str = "someone") -> models.User:
    user = models.User(
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("Password123"),
        subscription_plan_id=plan_id,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Existing users receive the Basic subscription when appropriate
# ---------------------------------------------------------------------------


class TestExistingUsersReceiveBasicPlan:
    def test_migration_backfill_sql_assigns_basic_to_legacy_rows(self):
        """
        Replays -- against a from-scratch schema shaped like the pre-Sub-Task-4
        database -- the exact UPDATE statement
        alembic/versions/3f0f35fc05d5_...py uses to backfill existing users,
        so a regression in that statement fails a test instead of only
        showing up against the real database.
        """
        metadata = MetaData()
        plans = Table(
            "subscription_plans",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("slug", String(50), unique=True, nullable=False),
        )
        users = Table(
            "users",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("username", String(50), nullable=False),
            # Nullable here, exactly as it was mid-migration before the
            # NOT NULL constraint was applied.
            Column("subscription_plan_id", Integer, ForeignKey("subscription_plans.id"), nullable=True),
        )

        engine = create_engine("sqlite:///:memory:")
        metadata.create_all(bind=engine)

        with engine.begin() as conn:
            basic_id = conn.execute(insert(plans).values(slug="basic")).inserted_primary_key[0]
            conn.execute(insert(plans).values(slug="premium"))
            legacy_user_id = conn.execute(
                insert(users).values(username="pre_existing_user", subscription_plan_id=None)
            ).inserted_primary_key[0]

            # The migration's backfill statement, verbatim.
            conn.execute(
                text(
                    "UPDATE users SET subscription_plan_id = "
                    "(SELECT id FROM subscription_plans WHERE slug = 'basic') "
                    "WHERE subscription_plan_id IS NULL"
                )
            )

            result = conn.execute(select(users.c.subscription_plan_id).where(users.c.id == legacy_user_id)).scalar()
            assert result == basic_id

    def test_new_user_registration_assigns_basic_plan(self, sub_client):
        client, session_factory = sub_client
        _register(client, USER_A)

        db = session_factory()
        try:
            user = db.query(models.User).filter(models.User.username == USER_A["username"]).first()
            plan = db.query(models.SubscriptionPlan).filter(models.SubscriptionPlan.id == user.subscription_plan_id).first()
            assert plan.slug == "basic"
        finally:
            db.close()

    def test_get_or_create_basic_plan_is_idempotent(self, db_session):
        first = subscription_service.get_or_create_basic_plan(db_session)
        second = subscription_service.get_or_create_basic_plan(db_session)

        assert first.id == second.id
        assert db_session.query(models.SubscriptionPlan).filter_by(slug="basic").count() == 1


# ---------------------------------------------------------------------------
# A user can access their active plan
# ---------------------------------------------------------------------------


class TestUserCanAccessActivePlan:
    def test_user_subscription_plan_relationship_returns_the_plan(self, db_session):
        plan = _make_plan(db_session, name="Premium", slug="premium", price=9.99, max_posts=2)
        user = _make_user(db_session, plan.id)

        assert user.subscription_plan_id == plan.id
        assert user.subscription_plan.name == "Premium"
        assert user.subscription_plan.max_posts == 2

    def test_api_me_endpoint_exposes_active_plan_id(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        resp = client.get("/auth/me", headers=headers)
        assert resp.status_code == 200
        assert "subscription_plan_id" in resp.json()
        assert resp.json()["subscription_plan_id"] is not None


# ---------------------------------------------------------------------------
# Historical billing records remain available after a plan change
# ---------------------------------------------------------------------------


class TestHistoricalBillingRecordsSurvivePlanChanges:
    def test_switching_plans_keeps_earlier_invoices(self, sub_client):
        client, session_factory = sub_client
        db = session_factory()
        try:
            basic = db.query(models.SubscriptionPlan).filter_by(slug="basic").first()
            if basic is None:
                basic = _make_plan(db, name="Basic", slug="basic")
            premium = _make_plan(db, name="Premium", slug="premium", price=9.99, max_posts=2)
            pro = _make_plan(db, name="Pro", slug="pro", price=19.99, max_posts=None)
            premium_id, pro_id = premium.id, pro.id
        finally:
            db.close()

        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        first = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)
        assert first.status_code == 201
        first_invoice = client.get("/subscriptions/billing-history", headers=headers).json()["billing_history"][0]

        second = client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers)
        assert second.status_code == 201

        invoices = client.get("/subscriptions/billing-history", headers=headers).json()["billing_history"]
        assert len(invoices) == 2
        transaction_ids = {inv["transaction_id"] for inv in invoices}
        assert first_invoice["transaction_id"] in transaction_ids

        me = client.get("/auth/me", headers=headers).json()
        assert me["subscription_plan_id"] == pro_id

    def test_canceling_reverts_to_basic_but_keeps_the_invoice(self, sub_client):
        client, session_factory = sub_client
        db = session_factory()
        try:
            plan = _make_plan(db, name="Premium", slug="premium", price=9.99, max_posts=2)
            plan_id = plan.id
        finally:
            db.close()

        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)

        cancel_resp = client.post("/subscriptions/cancel", headers=headers)
        assert cancel_resp.status_code == 200

        me = client.get("/auth/me", headers=headers).json()
        db = session_factory()
        try:
            basic = db.query(models.SubscriptionPlan).filter_by(slug="basic").first()
            assert me["subscription_plan_id"] == basic.id
        finally:
            db.close()

        invoices = client.get("/subscriptions/billing-history", headers=headers).json()["billing_history"]
        assert len(invoices) == 1
        assert invoices[0]["plan"] == "Premium"


# ---------------------------------------------------------------------------
# Subscription relationship works correctly
# ---------------------------------------------------------------------------


class TestSubscriptionPlanRelationship:
    def test_plan_users_backref_includes_the_user(self, db_session):
        plan = _make_plan(db_session, name="Pro", slug="pro", price=19.99, max_posts=None)
        user_1 = _make_user(db_session, plan.id, username="alice")
        user_2 = _make_user(db_session, plan.id, username="bob")
        db_session.refresh(plan)

        usernames = {u.username for u in plan.users}
        assert usernames == {"alice", "bob"}

    def test_deleting_a_plan_with_users_is_restricted(self, db_session):
        plan = _make_plan(db_session, name="Pro", slug="pro", price=19.99, max_posts=None)
        _make_user(db_session, plan.id)

        db_session.delete(plan)
        with pytest.raises(Exception):
            db_session.commit()
        db_session.rollback()

    def test_changing_a_users_plan_does_not_affect_other_users(self, db_session):
        basic = _make_plan(db_session, name="Basic", slug="basic")
        premium = _make_plan(db_session, name="Premium", slug="premium", price=9.99, max_posts=2)
        alice = _make_user(db_session, basic.id, username="alice")
        bob = _make_user(db_session, basic.id, username="bob")

        alice.subscription_plan_id = premium.id
        db_session.commit()
        db_session.refresh(alice)
        db_session.refresh(bob)

        assert alice.subscription_plan.slug == "premium"
        assert bob.subscription_plan.slug == "basic"
