"""
GET /subscriptions/billing-history -- the user-facing billing history
listing. Covers what test_fake_billing.py / test_subscriptions.py /
test_subscription_api.py / test_subscription_billing_comprehensive.py /
test_user_subscription_plan.py only exercise incidentally while testing
other things (they call this endpoint and check record counts/fields, but
none of them dedicate a test to the endpoint's own contract):

- requires authentication
- response shape: {"billing_history": [...], "page", "limit", "total",
  "total_pages"} -- each item has id/plan/transaction_id/amount/
  start_date/end_date/status/invoice_url/created_at, and never the raw
  user_id/subscription_plan_id/invoice_path used internally
- never returns another user's billing records
- paginated the same way GET /posts already is
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}


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


def _register_and_login(client: TestClient, user: dict) -> dict:
    client.post("/auth/register", json=user)
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()["plans"]
    return next(p["id"] for p in plans if p["slug"] == slug)


def _subscribe(client: TestClient, headers: dict, slug: str):
    return client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, slug)}, headers=headers)


class TestAuthRequired:
    def test_requires_authentication(self, client):
        resp = client.get("/subscriptions/billing-history")
        assert resp.status_code == 401


class TestResponseShape:
    def test_matches_the_documented_shape(self, client):
        headers = _register_and_login(client, USER_A)
        _subscribe(client, headers, "premium")

        resp = client.get("/subscriptions/billing-history", headers=headers)
        assert resp.status_code == 200
        body = resp.json()

        assert set(body.keys()) == {"billing_history", "page", "limit", "total", "total_pages"}
        assert body["page"] == 1
        assert body["total"] == 1
        assert body["total_pages"] == 1

        record = body["billing_history"][0]
        assert set(record.keys()) == {
            "id",
            "plan",
            "transaction_id",
            "amount",
            "start_date",
            "end_date",
            "status",
            "invoice_url",
            "created_at",
        }
        assert record["plan"] == "Premium"
        assert record["transaction_id"].startswith("TXN-")
        assert record["status"] == "paid"

        # Never the raw internal fields -- those belong to the admin-only
        # GET /admin/billing-history view, not this user-facing one.
        assert "user_id" not in record
        assert "subscription_plan_id" not in record
        assert "invoice_path" not in record


class TestUserIsolation:
    def test_never_returns_another_users_billing_history(self, client):
        headers_a = _register_and_login(client, USER_A)
        _subscribe(client, headers_a, "premium")

        headers_b = _register_and_login(client, USER_B)
        _subscribe(client, headers_b, "pro")

        history_a = client.get("/subscriptions/billing-history", headers=headers_a).json()["billing_history"]
        history_b = client.get("/subscriptions/billing-history", headers=headers_b).json()["billing_history"]

        assert len(history_a) == 1
        assert len(history_b) == 1
        assert history_a[0]["plan"] == "Premium"
        assert history_b[0]["plan"] == "Pro"

        ids_a = {r["transaction_id"] for r in history_a}
        ids_b = {r["transaction_id"] for r in history_b}
        assert ids_a.isdisjoint(ids_b)


class TestPagination:
    def test_page_and_limit_control_the_page_returned(self, client):
        headers = _register_and_login(client, USER_A)
        for slug in ("basic", "premium", "pro", "basic", "premium"):
            _subscribe(client, headers, slug)  # 5 billing records total

        page1 = client.get("/subscriptions/billing-history?page=1&limit=2", headers=headers).json()
        assert page1["total"] == 5
        assert page1["total_pages"] == 3
        assert page1["page"] == 1
        assert page1["limit"] == 2
        assert len(page1["billing_history"]) == 2

        page3 = client.get("/subscriptions/billing-history?page=3&limit=2", headers=headers).json()
        assert len(page3["billing_history"]) == 1  # last, partial page

        # No overlap between pages.
        page1_ids = {r["id"] for r in page1["billing_history"]}
        page3_ids = {r["id"] for r in page3["billing_history"]}
        assert page1_ids.isdisjoint(page3_ids)

    def test_defaults_to_page_1_limit_10(self, client):
        headers = _register_and_login(client, USER_A)
        _subscribe(client, headers, "basic")

        body = client.get("/subscriptions/billing-history", headers=headers).json()
        assert body["page"] == 1
        assert body["limit"] == 10
