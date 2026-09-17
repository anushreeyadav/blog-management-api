"""
Sub-Task 19 -- comprehensive test suite for subscription and billing
functionality, organized to mirror the sub-task's own checklist exactly
(Subscription models / Basic / Premium / Pro / Billing / API / Security)
so every line item has a directly corresponding, clearly-named test here.

Most of these areas already have deep, dedicated coverage built up across
earlier sub-tasks (test_subscription_plan_model.py, test_billing_history_
model.py, test_post_creation_subscription_limit.py, test_image_upload_
subscription_limit.py, test_like_subscription_limit.py, test_comment_
subscription_limit.py, test_fake_billing.py, test_invoice_pdf_generation.py,
test_subscription_api.py, test_subscription_usage.py, test_subscription_
security.py) -- this file is a single, auditable pass over the whole
checklist rather than a replacement for any of them. One real gap was found
and filled here: Premium-tier like/comment limits had no dedicated
API-level test (only Basic and Pro did) before this file.
"""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app as main_app
from app.services.subscription import LIMIT_EXCEEDED_MESSAGE

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
VALID_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

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
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def _register(client: TestClient, user: dict) -> dict:
    client.post("/auth/register", json=user)
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    return next(p["id"] for p in client.get("/subscriptions/plans").json() if p["slug"] == slug)


def _subscribe(client: TestClient, headers: dict, slug: str):
    return client.post("/subscriptions/subscribe", json={"plan_id": _plan_id(client, slug)}, headers=headers)


def _create_post(client: TestClient, headers: dict, title: str = "t"):
    return client.post("/posts", json={"title": title, "content": "body"}, headers=headers).json()


def _upload_image(client: TestClient, headers: dict, post_id: int, filename="a.jpg", content=VALID_JPEG, ctype="image/jpeg"):
    return client.post(
        f"/posts/{post_id}/image", headers=headers, files={"image": (filename, io.BytesIO(content), ctype)}
    )


def _make_plan(db_session, **overrides) -> models.SubscriptionPlan:
    defaults = dict(
        name="Basic", slug="basic", price=4.99, billing_interval="month",
        max_posts=1, max_images=1, max_likes=5, max_comments=5, is_active=True,
    )
    defaults.update(overrides)
    plan = models.SubscriptionPlan(**defaults)
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _make_user(db_session, plan_id: int, username: str = "someone") -> models.User:
    user = models.User(
        username=username, email=f"{username}@example.com",
        password_hash=hash_password("Password123"), subscription_plan_id=plan_id,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ===========================================================================
# Subscription models
# ===========================================================================


class TestSubscriptionModels:
    def test_subscription_plan_creation(self, db_session):
        plan = _make_plan(db_session, name="Test Plan", slug="test-plan")
        assert plan.id is not None
        assert db_session.query(models.SubscriptionPlan).filter_by(slug="test-plan").count() == 1

    def test_billing_history_creation(self, db_session):
        plan = _make_plan(db_session)
        user = _make_user(db_session, plan.id)
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        record = models.BillingHistory(
            user_id=user.id, subscription_plan_id=plan.id, transaction_id="TXN-COMPREHENSIVE-1",
            amount=4.99, start_date=now, end_date=now + timedelta(days=30), status="paid",
        )
        db_session.add(record)
        db_session.commit()
        assert record.id is not None

    def test_user_relationship(self, db_session):
        plan = _make_plan(db_session)
        user = _make_user(db_session, plan.id)
        assert user.subscription_plan.id == plan.id
        assert user.subscription_plan.name == "Basic"

    def test_plan_relationship(self, db_session):
        plan = _make_plan(db_session)
        user_1 = _make_user(db_session, plan.id, "u1")
        user_2 = _make_user(db_session, plan.id, "u2")
        db_session.refresh(plan)
        assert {u.username for u in plan.users} == {"u1", "u2"}


# ===========================================================================
# Basic plan
# ===========================================================================


class TestBasicPlan:
    def test_one_post_allowed(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        assert _create_post(client, headers, "only").get("id") is not None

    def test_second_post_rejected(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _create_post(client, headers, "first")
        resp = client.post("/posts", json={"title": "second", "content": "c"}, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE

    def test_one_image_allowed(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        post = _create_post(client, headers)
        resp = _upload_image(client, headers, post["id"])
        assert resp.status_code == 200

    def test_additional_image_rejected(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        post = _create_post(client, headers)
        _upload_image(client, headers, post["id"])
        resp = _upload_image(client, headers, post["id"], filename="b.jpg", content=VALID_PNG, ctype="image/png")
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE

    def test_like_limit_enforced(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        _subscribe(client, headers_a, "pro")  # author needs room for 6 posts
        headers_b = _register(client, USER_B)  # Basic: max_likes = 5
        post_ids = [_create_post(client, headers_a, f"t{i}")["id"] for i in range(6)]

        for pid in post_ids[:5]:
            assert client.post(f"/posts/{pid}/like", headers=headers_b).status_code == 201
        resp = client.post(f"/posts/{post_ids[5]}/like", headers=headers_b)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE

    def test_comment_limit_enforced(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)  # Basic: max_comments = 5
        post = _create_post(client, headers_a)

        for i in range(5):
            assert client.post(f"/posts/{post['id']}/comments", json={"text": f"c{i}"}, headers=headers_b).status_code == 201
        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "extra"}, headers=headers_b)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE


# ===========================================================================
# Premium plan
# ===========================================================================


class TestPremiumPlan:
    def test_two_posts_allowed(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "premium")
        assert _create_post(client, headers, "p1").get("id") is not None
        assert _create_post(client, headers, "p2").get("id") is not None

    def test_third_post_rejected(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "premium")
        _create_post(client, headers, "p1")
        _create_post(client, headers, "p2")
        resp = client.post("/posts", json={"title": "p3", "content": "c"}, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE

    def test_two_images_per_post_allowed(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "premium")
        post = _create_post(client, headers)
        assert _upload_image(client, headers, post["id"], "a.jpg", VALID_JPEG, "image/jpeg").status_code == 200
        resp2 = _upload_image(client, headers, post["id"], "b.png", VALID_PNG, "image/png")
        assert resp2.status_code == 200
        assert len(resp2.json()["images"]) == 2

    def test_third_image_rejected(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "premium")
        post = _create_post(client, headers)
        _upload_image(client, headers, post["id"], "a.jpg", VALID_JPEG, "image/jpeg")
        _upload_image(client, headers, post["id"], "b.png", VALID_PNG, "image/png")
        resp = _upload_image(client, headers, post["id"], "c.webp", VALID_WEBP, "image/webp")
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE

    def test_moderate_like_limit(self, client):
        """Fills a gap: Premium's like cap (25) had no dedicated API-level
        test before this file (only Basic and Pro did)."""
        client, _ = client
        headers_a = _register(client, USER_A)
        _subscribe(client, headers_a, "pro")  # author needs room for 26 posts
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_b, "premium")  # Premium: max_likes = 25
        post_ids = [_create_post(client, headers_a, f"t{i}")["id"] for i in range(26)]

        for pid in post_ids[:25]:
            assert client.post(f"/posts/{pid}/like", headers=headers_b).status_code == 201
        resp = client.post(f"/posts/{post_ids[25]}/like", headers=headers_b)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE

    def test_moderate_comment_limit(self, client):
        """Fills the same gap for comments (Premium: max_comments = 25)."""
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_b, "premium")
        post = _create_post(client, headers_a)

        for i in range(25):
            assert client.post(f"/posts/{post['id']}/comments", json={"text": f"c{i}"}, headers=headers_b).status_code == 201
        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "extra"}, headers=headers_b)
        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE


# ===========================================================================
# Pro plan
# ===========================================================================


class TestProPlan:
    def test_multiple_posts(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "pro")
        for i in range(8):
            assert client.post("/posts", json={"title": f"p{i}", "content": "c"}, headers=headers).status_code == 201

    def test_multiple_images(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "pro")
        post = _create_post(client, headers)
        for i in range(6):
            resp = _upload_image(client, headers, post["id"], f"img{i}.jpg", VALID_JPEG, "image/jpeg")
            assert resp.status_code == 200
        assert len(resp.json()["images"]) == 6

    def test_multiple_likes(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        _subscribe(client, headers_a, "pro")
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_b, "pro")
        post_ids = [_create_post(client, headers_a, f"t{i}")["id"] for i in range(9)]
        for pid in post_ids:
            assert client.post(f"/posts/{pid}/like", headers=headers_b).status_code == 201

    def test_multiple_comments(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_b, "pro")
        post = _create_post(client, headers_a)
        for i in range(12):
            assert client.post(f"/posts/{post['id']}/comments", json={"text": f"c{i}"}, headers=headers_b).status_code == 201


# ===========================================================================
# Billing
# ===========================================================================


class TestBilling:
    def test_subscription_creation(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        resp = _subscribe(client, headers, "premium")
        assert resp.status_code == 201
        assert resp.json()["subscription"]["status"] == "active"

    def test_transaction_generation(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        resp = _subscribe(client, headers, "premium")
        txn = resp.json()["invoice"]["transaction_id"]
        assert txn.startswith("TXN-")

    def test_billing_history(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        _subscribe(client, headers, "basic")
        _subscribe(client, headers, "premium")
        history = client.get("/subscriptions/billing-history", headers=headers).json()
        assert len(history) == 2

    def test_start_end_dates(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        resp = _subscribe(client, headers, "premium")
        subscription = resp.json()["subscription"]
        assert subscription["current_period_start"] < subscription["current_period_end"]
        assert resp.json()["invoice"]["start_date"] == subscription["current_period_start"]
        assert resp.json()["invoice"]["end_date"] == subscription["current_period_end"]

    def test_invoice_generation(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        resp = _subscribe(client, headers, "premium")
        assert resp.json()["invoice"]["invoice_path"] is not None
        assert resp.json()["invoice"]["status"] == "paid"

    def test_invoice_path_storage(self, client):
        client, session_factory = client
        headers = _register(client, USER_A)
        resp = _subscribe(client, headers, "premium")
        invoice_path = resp.json()["invoice"]["invoice_path"]
        assert invoice_path.startswith("/media/invoices/invoice_")

        db = session_factory()
        try:
            row = db.query(models.BillingHistory).filter_by(
                transaction_id=resp.json()["invoice"]["transaction_id"]
            ).one()
            assert row.invoice_path == invoice_path
        finally:
            db.close()


# ===========================================================================
# API
# ===========================================================================


class TestApi:
    def test_plan_listing(self, client):
        client, _ = client
        _register(client, USER_A)  # seeds the plans in this fresh database
        plans = client.get("/subscriptions/plans").json()
        assert {p["slug"] for p in plans} == {"basic", "premium", "pro"}

    def test_current_subscription(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        me = client.get("/subscriptions/me", headers=headers).json()
        assert me["plan_name"] == "Basic"

    def test_usage_endpoint(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        usage = client.get("/subscriptions/usage", headers=headers).json()
        assert usage == {
            "plan": "Basic",
            "posts": {"used": 0, "limit": 1},
            "images": {"used": 0, "limit": 1},
            "likes": {"used": 0, "limit": 5},
            "comments": {"used": 0, "limit": 5},
        }

    def test_subscription_purchase_and_change(self, client):
        client, _ = client
        headers = _register(client, USER_A)
        assert _subscribe(client, headers, "premium").status_code == 201
        assert client.get("/subscriptions/me", headers=headers).json()["plan_name"] == "Premium"

        assert _subscribe(client, headers, "pro").status_code == 201
        assert client.get("/subscriptions/me", headers=headers).json()["plan_name"] == "Pro"


# ===========================================================================
# Security
# ===========================================================================


class TestSecurity:
    def test_authentication_required(self, client):
        client, _ = client
        assert client.get("/subscriptions/me").status_code == 401
        assert client.get("/subscriptions/usage").status_code == 401
        assert client.post("/subscriptions/subscribe", json={"plan_id": 1}).status_code == 401
        assert client.post("/posts", json={"title": "t", "content": "c"}).status_code == 401

    def test_user_cannot_manipulate_another_users_subscription(self, client):
        client, _ = client
        headers_a = _register(client, USER_A)
        headers_b = _register(client, USER_B)
        _subscribe(client, headers_b, "pro")

        # A has no way to reference B in a subscribe/cancel call -- both
        # endpoints only ever act on the authenticated user.
        _subscribe(client, headers_a, "basic")
        me_a = client.get("/subscriptions/me", headers=headers_a).json()
        me_b = client.get("/subscriptions/me", headers=headers_b).json()
        assert me_a["plan_name"] == "Basic"
        assert me_b["plan_name"] == "Pro"  # untouched by A's actions

    def test_limits_enforced_server_side(self, client):
        """A client cannot influence enforcement by sending fake plan/limit
        data -- the real plan is always looked up from the authenticated
        user's own DB row, never from the request."""
        client, _ = client
        headers = _register(client, USER_A)
        _create_post(client, headers, "first")

        resp = client.post(
            "/posts",
            json={"title": "second", "content": "c", "max_posts": None, "plan": "pro"},
            headers=headers,
        )
        assert resp.status_code in (403, 422)
        if resp.status_code == 403:
            assert resp.json()["detail"] == LIMIT_EXCEEDED_MESSAGE
