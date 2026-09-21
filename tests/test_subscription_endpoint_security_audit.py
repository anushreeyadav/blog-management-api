"""
Subscription Endpoint Security -- a consolidated audit against two rules
that span every subscription/billing endpoint, rather than living inside
any single endpoint's own test file:

1. Never trust client-provided user_id, plan_name, price, max_posts,
   max_images, max_likes, max_comments, transaction_id, subscription
   dates, or invoice path -- the server must determine all of these.
2. User isolation -- a user may only ever view their own subscription,
   usage, and billing history, access their own invoices, and change
   their own subscription; never another user's, not even by editing an
   id in the URL.

No production code changes were needed for this audit: SubscribeRequest
already forbids unknown fields (app/schemas.py), and every subscription/
billing endpoint already derives its user exclusively from the JWT via
get_current_user (app/auth.py), never from anything client-supplied (see
app/routers/subscriptions.py). This file is the consolidated evidence for
both rules, the same way test_validation_response_standardization.py
consolidates a cross-action check that already held per-endpoint.
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


def _register(client: TestClient, user: dict) -> dict:
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


# ---------------------------------------------------------------------------
# 1. Never trust client-provided values
# ---------------------------------------------------------------------------


class TestClientSuppliedValuesAreNeverHonored:
    """POST /subscriptions/subscribe (and /change, which shares the same
    SubscribeRequest body) is the only subscription/billing endpoint that
    accepts a request body at all -- every value the spec lists is either
    not an accepted field on it (rejected outright by extra="forbid") or,
    for user identity, never read from the body/query/path in the first
    place (see TestUserIsolation below)."""

    def test_every_listed_field_is_rejected_in_one_subscribe_call(self, client):
        headers = _register(client, USER_A)
        basic_id = _plan_id(client, "basic")

        spoofed_body = {
            "plan_id": basic_id,
            "user_id": 999,
            "plan_name": "Pro",
            "price": 0,
            "max_posts": None,
            "max_images": None,
            "max_likes": None,
            "max_comments": None,
            "transaction_id": "TXN-FAKE-0000000000",
            "start_date": "2000-01-01T00:00:00",
            "end_date": "2099-01-01T00:00:00",
            "invoice_path": "/media/invoices/not_real.pdf",
        }
        resp = client.post("/subscriptions/subscribe", json=spoofed_body, headers=headers)

        # SubscribeRequest forbids any field beyond plan_id -- a client
        # cannot smuggle any of these into a subscribe/change call.
        assert resp.status_code == 422

    def test_same_fields_rejected_on_change_too(self, client):
        headers = _register(client, USER_A)
        _subscribe(client, headers, "basic")
        pro_id = _plan_id(client, "pro")

        resp = client.post(
            "/subscriptions/change",
            json={"plan_id": pro_id, "price": 0, "transaction_id": "TXN-FAKE", "invoice_path": "/evil.pdf"},
            headers=headers,
        )
        assert resp.status_code == 422

    def test_server_actually_generates_these_values_itself(self, client):
        """The flip side of "never trust client input": confirms the
        server-derived transaction_id/dates/invoice_path/amount actually
        come from the real plan and a real invoice, not an empty/blank
        fallback -- i.e. rejecting the client's values isn't masking a
        production code path that silently defaults to something equally
        untrustworthy."""
        headers = _register(client, USER_A)
        invoice = _subscribe(client, headers, "premium")

        assert invoice["transaction_id"].startswith("TXN-")
        assert invoice["amount"] == 999  # the real Premium price, not a client-supplied 0
        assert invoice["invoice_path"].startswith("/media/invoices/invoice_")
        assert invoice["start_date"] < invoice["end_date"]


# ---------------------------------------------------------------------------
# 2. User isolation
# ---------------------------------------------------------------------------


class TestUserIsolation:
    def test_view_own_subscription_only(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_a, "premium")
        _subscribe(client, headers_b, "pro")

        assert client.get("/subscriptions/me", headers=headers_a).json()["plan"]["name"] == "Premium"
        assert client.get("/subscriptions/me", headers=headers_b).json()["plan"]["name"] == "Pro"

    def test_view_own_usage_only(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        client.post("/posts", json={"title": "a's post", "content": "x"}, headers=headers_a)

        usage_a = client.get("/subscriptions/usage", headers=headers_a).json()["usage"]
        usage_b = client.get("/subscriptions/usage", headers=headers_b).json()["usage"]
        assert usage_a["posts"]["used"] == 1
        assert usage_b["posts"]["used"] == 0

    def test_view_own_billing_history_only(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_a, "premium")
        _subscribe(client, headers_b, "pro")

        history_a = client.get("/subscriptions/billing-history", headers=headers_a).json()["billing_history"]
        history_b = client.get("/subscriptions/billing-history", headers=headers_b).json()["billing_history"]
        assert {r["plan"] for r in history_a} == {"Premium"}
        assert {r["plan"] for r in history_b} == {"Pro"}

    def test_access_own_invoice_only(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        invoice_a = _subscribe(client, headers_a, "premium")

        own = client.get(f"/subscriptions/billing/{invoice_a['id']}/invoice", headers=headers_a)
        assert own.status_code == 200
        assert own.content.startswith(b"%PDF")

        other = client.get(f"/subscriptions/billing/{invoice_a['id']}/invoice", headers=headers_b)
        assert other.status_code == 404

    def test_change_own_subscription_only(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_b, "pro")

        _subscribe(client, headers_a, "basic")

        assert client.get("/subscriptions/me", headers=headers_a).json()["plan"]["name"] == "Basic"
        assert client.get("/subscriptions/me", headers=headers_b).json()["plan"]["name"] == "Pro"  # untouched


class TestNoIdInUrlCanLeakAnotherUsersBillingInfo:
    def test_the_only_id_bearing_billing_route_is_ownership_checked(self, client):
        """Enumerates every route this app actually exposes (via its own
        OpenAPI schema, which reflects what a client can really reach,
        rather than internal router wiring) under /subscriptions and
        /admin that takes a path parameter, to confirm
        GET /subscriptions/billing/{billing_id}/invoice is the *only* one
        that could even be probed by editing an id in the URL -- and that
        it is in fact gated (proven concretely by
        test_access_own_invoice_only above and
        tests/test_invoice_download_api.py)."""
        paths = main_app.openapi()["paths"]
        id_bearing_routes = sorted(
            path
            for path in paths
            if (path.startswith("/subscriptions") or path.startswith("/admin")) and "{" in path
        )
        assert id_bearing_routes == ["/subscriptions/billing/{billing_id}/invoice"]

    def test_incrementing_the_billing_id_never_returns_another_users_invoice(self, client):
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        invoice_a = _subscribe(client, headers_a, "premium")
        invoice_b = _subscribe(client, headers_b, "pro")

        # Whichever id belongs to the other user -- probed directly, not
        # assumed -- must never come back as a 200 for this caller.
        for candidate_id in (invoice_a["id"], invoice_b["id"], invoice_a["id"] + 1000):
            if candidate_id == invoice_b["id"]:
                continue
            resp = client.get(f"/subscriptions/billing/{candidate_id}/invoice", headers=headers_b)
            assert resp.status_code == 404
