"""
GET /subscriptions/billing/{billing_id}/invoice -- lets a user download
their own invoice PDF, reusing the existing media/file-serving
architecture (app/services/invoices.py's INVOICES_DIR / invoice_path
convention) behind an authenticated, ownership-checked route instead of
the public /media static mount.

Covers the exact security sequence the spec lists: authenticate, retrieve
the billing record, verify it belongs to the caller, verify the invoice
file exists, then return the PDF -- and that a billing record belonging to
another user is never exposed (same 404 as a nonexistent one, so the
endpoint can't be used to enumerate other users' billing_ids).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
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
        yield test_client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient, user: dict) -> dict:
    client.post("/auth/register", json=user)
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()["plans"]
    return next(p["id"] for p in plans if p["slug"] == slug)


def _subscribe(client: TestClient, headers: dict, slug: str) -> dict:
    resp = client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, slug)}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["invoice"]


class TestAuthRequired:
    def test_requires_authentication(self, client):
        test_client, _ = client
        resp = test_client.get("/subscriptions/billing/1/invoice")
        assert resp.status_code == 401


class TestOwnerCanDownload:
    def test_returns_the_pdf(self, client):
        test_client, _ = client
        headers = _register_and_login(test_client, USER_A)
        invoice = _subscribe(test_client, headers, "premium")
        billing_id = invoice["id"]

        resp = test_client.get(f"/subscriptions/billing/{billing_id}/invoice", headers=headers)

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")


class TestCrossUserAccessDenied:
    def test_another_users_invoice_is_never_exposed(self, client):
        test_client, _ = client
        headers_a = _register_and_login(test_client, USER_A)
        invoice_a = _subscribe(test_client, headers_a, "premium")

        headers_b = _register_and_login(test_client, USER_B)
        _subscribe(test_client, headers_b, "pro")

        resp = test_client.get(f"/subscriptions/billing/{invoice_a['id']}/invoice", headers=headers_b)
        assert resp.status_code == 404

    def test_wrong_owner_and_nonexistent_id_return_the_identical_response(self, client):
        """A 403 here would itself leak that billing_id belongs to someone
        else, letting a client enumerate other users' invoices by id --
        so both cases must be indistinguishable."""
        test_client, _ = client
        headers_a = _register_and_login(test_client, USER_A)
        invoice_a = _subscribe(test_client, headers_a, "premium")

        headers_b = _register_and_login(test_client, USER_B)

        wrong_owner_resp = test_client.get(f"/subscriptions/billing/{invoice_a['id']}/invoice", headers=headers_b)
        nonexistent_resp = test_client.get("/subscriptions/billing/999999/invoice", headers=headers_b)

        assert wrong_owner_resp.status_code == nonexistent_resp.status_code == 404
        assert wrong_owner_resp.json() == nonexistent_resp.json()


class TestNonexistentBillingId:
    def test_returns_404(self, client):
        test_client, _ = client
        headers = _register_and_login(test_client, USER_A)
        resp = test_client.get("/subscriptions/billing/999999/invoice", headers=headers)
        assert resp.status_code == 404


class TestMissingInvoiceFile:
    def test_returns_404_when_the_invoice_path_is_missing(self, client):
        """A BillingHistory row can in principle exist without a
        successfully generated invoice_path -- must 404, not 500."""
        test_client, session_factory = client
        headers = _register_and_login(test_client, USER_A)
        invoice = _subscribe(test_client, headers, "premium")

        db = session_factory()
        try:
            record = db.query(models.BillingHistory).filter_by(id=invoice["id"]).one()
            record.invoice_path = None
            db.commit()
        finally:
            db.close()

        resp = test_client.get(f"/subscriptions/billing/{invoice['id']}/invoice", headers=headers)
        assert resp.status_code == 404
