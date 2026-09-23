"""
Sub-Task 11 -- the subscription API surface itself: GET /subscriptions/plans,
GET /subscriptions/me, and POST /subscriptions/subscribe (the fake
billing/subscription flow -- no real payment gateway).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app
from app.services import invoices as invoices_module

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


class TestListPlans:
    def test_returns_name_price_and_all_four_limits_for_each_plan(self, client):
        # Plans are seeded lazily on first registration in a fresh
        # database (see app/services/subscription.py's
        # get_or_create_basic_plan) -- register a user first so this
        # freshly created test database actually has plans to list.
        _register_and_login(client)

        resp = client.get("/subscriptions/plans")
        assert resp.status_code == 200

        body = resp.json()
        assert set(body) == {"plans"}
        by_slug = {p["slug"]: p for p in body["plans"]}
        assert set(by_slug) == {"basic", "premium", "pro"}
        for plan in by_slug.values():
            for field in ("name", "price", "max_posts", "max_images", "max_likes", "max_comments"):
                assert field in plan

        assert by_slug["basic"]["max_posts"] == 1
        assert by_slug["premium"]["max_posts"] == 2
        assert by_slug["pro"]["max_posts"] is None

    def test_is_public_no_authentication_required(self, client):
        _register_and_login(client)

        resp = client.get("/subscriptions/plans")
        assert resp.status_code == 200


class TestCurrentSubscription:
    def test_default_basic_user_sees_their_plan_and_limits(self, client):
        headers = _register_and_login(client)

        resp = client.get("/subscriptions/me", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == 1
        assert body["plan"]["name"] == "Basic"
        assert body["plan"]["price"] == 499
        assert body["plan"]["max_posts"] == 1
        assert body["plan"]["max_images"] == 1
        assert body["plan"]["max_likes"] == 5
        assert body["plan"]["max_comments"] == 5
        # Never explicitly subscribed -- no formal subscription period.
        assert body["status"] == "default"
        assert body["start_date"] is None
        assert body["end_date"] is None

    def test_after_subscribing_shows_the_new_plan_and_dates(self, client):
        headers = _register_and_login(client)
        premium_id = _plan_id(client, "premium")
        client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)

        resp = client.get("/subscriptions/me", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == 1
        assert body["plan"]["name"] == "Premium"
        assert body["plan"]["id"] == premium_id
        assert body["status"] == "active"
        assert body["start_date"] is not None
        assert body["end_date"] is not None
        assert body["start_date"] < body["end_date"]

    def test_requires_authentication(self, client):
        resp = client.get("/subscriptions/me")
        assert resp.status_code == 401


class TestChangeSubscription:
    def test_change_creates_new_billing_record_and_invoice(self, client):
        headers = _register_and_login(client)
        basic_id = _plan_id(client, "basic")
        pro_id = _plan_id(client, "pro")

        first = client.post("/subscriptions/subscribe", json={"plan_id": basic_id}, headers=headers)
        assert first.status_code == 201
        first_transaction_id = first.json()["invoice"]["transaction_id"]

        resp = client.post("/subscriptions/change", json={"plan_id": pro_id}, headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["message"] == "Subscription changed successfully."
        assert body["subscription"]["plan"] == "Pro"
        assert body["subscription"]["status"] == "active"
        assert body["subscription"]["start_date"] < body["subscription"]["end_date"]
        assert body["billing"]["transaction_id"] != first_transaction_id
        assert body["billing"]["amount"] == 1999
        assert body["billing"]["invoice_url"].startswith("/media/invoices/invoice_")

        history = client.get("/subscriptions/billing-history", headers=headers)
        assert history.status_code == 200
        assert len(history.json()["billing_history"]) == 2
        assert {entry["transaction_id"] for entry in history.json()["billing_history"]} >= {
            first_transaction_id,
            body["billing"]["transaction_id"],
        }

        invoice_filename = body["billing"]["invoice_url"].rsplit("/", 1)[-1]
        invoice_path = invoices_module.INVOICES_DIR / invoice_filename
        assert invoice_path.is_file()
        assert invoice_path.read_bytes().startswith(b"%PDF")

    def test_change_requires_authentication(self, client):
        resp = client.post("/subscriptions/change", json={"plan_id": 3})
        assert resp.status_code == 401


class TestSubscribeFakeBillingFlow:
    def test_subscribing_returns_both_subscription_and_invoice(self, client):
        headers = _register_and_login(client)
        premium_id = _plan_id(client, "premium")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)

        assert resp.status_code == 201
        body = resp.json()
        assert "subscription" in body and "invoice" in body

        subscription = body["subscription"]
        assert subscription["plan_id"] == premium_id
        assert subscription["status"] == "active"

        invoice = body["invoice"]
        assert invoice["subscription_plan_id"] == premium_id
        assert invoice["amount"] == 999
        assert invoice["status"] == "paid"
        assert invoice["transaction_id"].startswith("TXN-")

    def test_a_real_pdf_file_is_written_and_the_path_is_stored(self, client):
        headers = _register_and_login(client)
        premium_id = _plan_id(client, "premium")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)

        invoice_path = resp.json()["invoice"]["invoice_path"]
        assert invoice_path.startswith("/media/invoices/invoice_")
        assert invoice_path.endswith(".pdf")

        # invoices_module.INVOICES_DIR is read here (not imported as a bare
        # name at module load time) so it reflects the isolated tmp
        # directory the autouse conftest fixture redirects it to.
        filename = invoice_path.rsplit("/", 1)[-1]
        pdf_file = invoices_module.INVOICES_DIR / filename
        assert pdf_file.is_file()
        assert pdf_file.read_bytes().startswith(b"%PDF")  # a real PDF, not a placeholder string

    def test_no_real_payment_gateway_subscribing_is_immediate_and_free_of_external_calls(self, client):
        """The flow is entirely local: no external HTTP call is made, and
        the plan activates synchronously in the same response."""
        headers = _register_and_login(client)
        pro_id = _plan_id(client, "pro")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers)
        assert resp.status_code == 201
        assert resp.json()["subscription"]["status"] == "active"

        me = client.get("/subscriptions/me", headers=headers).json()
        assert me["plan"]["name"] == "Pro"

    def test_invalid_plan_is_rejected(self, client):
        headers = _register_and_login(client)

        resp = client.post("/subscriptions/subscribe", json={"plan_id": 999999}, headers=headers)
        assert resp.status_code == 404

    def test_requires_authentication(self, client):
        _register_and_login(client)  # seeds the plans in this fresh database
        premium_id = _plan_id(client, "premium")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": premium_id})
        assert resp.status_code == 401


class TestExistingAuthAndBlogApisUndisturbed:
    """Sanity check: none of this sub-task's changes touch auth or the
    blog CRUD surface."""

    def test_auth_register_and_login_still_work(self, client):
        headers = _register_and_login(client)
        resp = client.get("/auth/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["username"] == USER_A["username"]

    def test_post_crud_still_works(self, client):
        headers = _register_and_login(client)
        create = client.post("/posts", json={"title": "t", "content": "c"}, headers=headers)
        assert create.status_code == 201
        get_resp = client.get(f"/posts/{create.json()['id']}")
        assert get_resp.status_code == 200


@pytest.fixture()
def client_with_db():
    """Same isolated setup as the `client` fixture above, but also exposes
    the session factory so tests can query the Notification table directly
    -- the plain `client` fixture is left untouched since many existing
    tests in this file destructure it as a single TestClient value."""
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


def _notifications_for(session_factory, user_id: int) -> list[models.Notification]:
    db = session_factory()
    try:
        return (
            db.query(models.Notification)
            .filter(models.Notification.user_id == user_id)
            .order_by(models.Notification.id)
            .all()
        )
    finally:
        db.close()


class TestSubscriptionNotifications:
    """In-app Notification records for subscription activation and
    renewal, alongside (not replacing) the existing subscribe/change API
    responses and BillingHistory/invoice behavior tested above."""

    def test_activation_creates_notification(self, client_with_db):
        client, session_factory = client_with_db
        headers = _register_and_login(client)
        me = client.get("/auth/me", headers=headers).json()
        premium_id = _plan_id(client, "premium")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)
        assert resp.status_code == 201

        notifications = _notifications_for(session_factory, me["id"])
        assert len(notifications) == 1
        assert notifications[0].notification_type == "subscription_activated"
        assert notifications[0].message == "Your subscription has been activated successfully."
        assert notifications[0].is_read is False

    def test_renewal_creates_notification_not_activation(self, client_with_db):
        client, session_factory = client_with_db
        headers = _register_and_login(client)
        me = client.get("/auth/me", headers=headers).json()
        premium_id = _plan_id(client, "premium")

        first = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)
        assert first.status_code == 201

        # Re-subscribing to the same already-active plan is this app's
        # renewal path (see app/routers/subscriptions.py's
        # _change_subscription) -- there is no separate scheduled
        # auto-renew job.
        second = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)
        assert second.status_code == 201

        notifications = _notifications_for(session_factory, me["id"])
        assert len(notifications) == 2
        assert notifications[0].notification_type == "subscription_activated"
        assert notifications[1].notification_type == "subscription_renewed"
        assert notifications[1].message == "Your subscription has been renewed successfully."
        assert notifications[1].is_read is False

    def test_switching_to_a_different_plan_is_activation_not_renewal(self, client_with_db):
        client, session_factory = client_with_db
        headers = _register_and_login(client)
        me = client.get("/auth/me", headers=headers).json()
        basic_id = _plan_id(client, "basic")
        pro_id = _plan_id(client, "pro")

        client.post("/subscriptions/subscribe", json={"plan_id": basic_id}, headers=headers)
        resp = client.post("/subscriptions/change", json={"plan_id": pro_id}, headers=headers)
        assert resp.status_code == 200

        notifications = _notifications_for(session_factory, me["id"])
        assert [n.notification_type for n in notifications] == [
            "subscription_activated",
            "subscription_activated",
        ]

    def test_failed_subscribe_creates_no_notification(self, client_with_db):
        client, session_factory = client_with_db
        headers = _register_and_login(client)
        me = client.get("/auth/me", headers=headers).json()

        resp = client.post("/subscriptions/subscribe", json={"plan_id": 999999}, headers=headers)
        assert resp.status_code == 404
        assert _notifications_for(session_factory, me["id"]) == []

    def test_unauthenticated_subscribe_creates_no_notification(self, client_with_db):
        client, session_factory = client_with_db
        headers = _register_and_login(client)  # seeds the plans in this fresh database
        me = client.get("/auth/me", headers=headers).json()
        premium_id = _plan_id(client, "premium")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": premium_id})
        assert resp.status_code == 401
        assert _notifications_for(session_factory, me["id"]) == []
