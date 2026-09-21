"""
Sub-Task 12 -- fake billing/transaction generation. No real payment
gateway: subscribing generates a sample TXN-<unique-value> transaction id
and a BillingHistory row locally, synchronously, in the same request.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app
from app.services import billing as billing_service

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}


@pytest.fixture()
def client():
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
        yield test_client
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient) -> dict:
    client.post("/auth/register", json=USER_A)
    resp = client.post("/auth/login", json={"username": USER_A["username"], "password": USER_A["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()["plans"]
    return next(p["id"] for p in plans if p["slug"] == slug)


def _subscribe(client: TestClient, headers: dict, slug: str):
    return client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, slug)}, headers=headers)


def _billing_history(client: TestClient, headers: dict) -> list[dict]:
    return client.get("/subscriptions/billing-history", headers=headers).json()["billing_history"]


class TestNewSubscription:
    def test_subscribing_stores_user_plan_amount_dates_and_status(self, client):
        headers = _register_and_login(client)

        resp = _subscribe(client, headers, "premium")

        assert resp.status_code == 201
        invoice = resp.json()["invoice"]
        assert invoice["user_id"] is not None
        assert invoice["subscription_plan_id"] == _plan_id(client, "premium")
        assert invoice["amount"] == 999
        assert invoice["transaction_id"].startswith("TXN-")
        assert invoice["start_date"] is not None
        assert invoice["end_date"] is not None
        assert invoice["status"] == "paid"

    def test_new_subscriber_billing_history_has_exactly_one_record(self, client):
        headers = _register_and_login(client)
        _subscribe(client, headers, "basic")

        history = _billing_history(client, headers)
        assert len(history) == 1


class TestPlanUpgrade:
    def test_upgrading_basic_to_pro_creates_a_new_record_for_the_higher_plan(self, client):
        headers = _register_and_login(client)
        _subscribe(client, headers, "basic")

        resp = _subscribe(client, headers, "pro")

        assert resp.status_code == 201
        assert resp.json()["invoice"]["amount"] == 1999
        assert resp.json()["subscription"]["plan_id"] == _plan_id(client, "pro")

        me = client.get("/subscriptions/me", headers=headers).json()
        assert me["plan"]["name"] == "Pro"


class TestPlanChange:
    def test_switching_from_premium_to_basic_is_a_downgrade_that_still_works(self, client):
        headers = _register_and_login(client)
        _subscribe(client, headers, "premium")

        resp = _subscribe(client, headers, "basic")

        assert resp.status_code == 201
        assert resp.json()["invoice"]["amount"] == 499

        me = client.get("/subscriptions/me", headers=headers).json()
        assert me["plan"]["name"] == "Basic"

    def test_the_previous_subscription_is_marked_canceled_not_deleted(self, client):
        headers = _register_and_login(client)
        _subscribe(client, headers, "basic")
        _subscribe(client, headers, "premium")

        # get_active_subscription only returns the current (Premium) one --
        # confirm indirectly via /me, and that two distinct invoices exist.
        me = client.get("/subscriptions/me", headers=headers).json()
        assert me["plan"]["name"] == "Premium"
        assert len(_billing_history(client, headers)) == 2


class TestUniqueTransactionId:
    def test_every_subscribe_call_gets_a_distinct_transaction_id(self, client):
        headers = _register_and_login(client)

        ids = set()
        for slug in ("basic", "premium", "pro", "basic", "premium"):
            resp = _subscribe(client, headers, slug)
            ids.add(resp.json()["invoice"]["transaction_id"])

        assert len(ids) == 5  # no collisions across repeated plan switches

    def test_transaction_ids_follow_the_txn_prefix_format(self, client):
        headers = _register_and_login(client)
        resp = _subscribe(client, headers, "premium")

        transaction_id = resp.json()["invoice"]["transaction_id"]
        assert transaction_id.startswith("TXN-")
        assert len(transaction_id.split("-")) == 3  # TXN-<year>-<unique value>


class TestBillingHistoryPreservation:
    def test_old_billing_records_remain_after_a_plan_change(self, client):
        headers = _register_and_login(client)
        first = _subscribe(client, headers, "basic").json()["invoice"]

        _subscribe(client, headers, "premium")
        _subscribe(client, headers, "pro")

        history = _billing_history(client, headers)
        assert len(history) == 3
        transaction_ids = {inv["transaction_id"] for inv in history}
        assert first["transaction_id"] in transaction_ids  # never removed or altered

    def test_billing_records_are_immutable_snapshots_of_their_own_plan_and_amount(self, client):
        """Even if the user's plan changes later, the historical invoice
        still reflects what was actually charged at the time."""
        headers = _register_and_login(client)
        basic_invoice = _subscribe(client, headers, "basic").json()["invoice"]
        _subscribe(client, headers, "pro")

        history = _billing_history(client, headers)
        preserved = next(inv for inv in history if inv["transaction_id"] == basic_invoice["transaction_id"])
        assert preserved["amount"] == 499
        assert preserved["plan"] == "Basic"


class TestSubscriptionDates:
    def test_default_duration_is_thirty_days(self, client):
        assert billing_service.DEFAULT_SUBSCRIPTION_DURATION_DAYS == 30

    def test_monthly_plan_period_spans_the_configured_duration(self, client):
        headers = _register_and_login(client)
        resp = _subscribe(client, headers, "premium")

        subscription = resp.json()["subscription"]
        me = client.get("/subscriptions/me", headers=headers).json()

        from datetime import datetime

        start = datetime.fromisoformat(subscription["current_period_start"])
        end = datetime.fromisoformat(subscription["current_period_end"])
        assert (end - start).days == billing_service.DEFAULT_SUBSCRIPTION_DURATION_DAYS
        assert me["start_date"] == subscription["current_period_start"]
        assert me["end_date"] == subscription["current_period_end"]

    def test_invoice_billing_period_matches_the_subscription_period(self, client):
        headers = _register_and_login(client)
        resp = _subscribe(client, headers, "basic")

        subscription = resp.json()["subscription"]
        invoice = resp.json()["invoice"]
        assert invoice["start_date"] == subscription["current_period_start"]
        assert invoice["end_date"] == subscription["current_period_end"]


class TestDuplicateSubscriptionRequestSafety:
    """Not part of Sub-Task 12's required test list by name, but directly
    covers its "safe against accidental duplicate subscription requests"
    requirement -- kept alongside the rest of this sub-task's coverage."""

    def test_resubscribing_to_the_same_plan_does_not_create_a_duplicate_invoice(self, client):
        headers = _register_and_login(client)
        first = _subscribe(client, headers, "premium")
        assert len(_billing_history(client, headers)) == 1

        second = _subscribe(client, headers, "premium")

        assert second.status_code == 201
        assert len(_billing_history(client, headers)) == 1  # still just one
        assert second.json()["invoice"]["transaction_id"] == first.json()["invoice"]["transaction_id"]
        assert second.json()["subscription"]["id"] == first.json()["subscription"]["id"]

    def test_resubscribing_to_a_different_plan_still_creates_a_new_record(self, client):
        headers = _register_and_login(client)
        _subscribe(client, headers, "premium")

        resp = _subscribe(client, headers, "pro")

        assert resp.status_code == 201
        assert len(_billing_history(client, headers)) == 2
