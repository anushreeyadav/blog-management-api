"""
Subscription-based access control: plans, subscribing, plan-based limits on
post creation and image upload, billing history, and admin-only visibility.

Mirrors the security_client / image_client pattern from test_api.py and
test_images.py: an isolated in-memory SQLite database per test via a
get_db override, plus direct session-factory access where a test needs to
set up state (e.g. marking a user as admin) that has no API of its own.
"""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app
from app.services import media as media_module

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}


@pytest.fixture(autouse=True)
def _isolated_media_dir(tmp_path, monkeypatch):
    posts_dir = tmp_path / "media" / "posts"
    posts_dir.mkdir(parents=True)
    monkeypatch.setattr(media_module, "POSTS_MEDIA_DIR", posts_dir)
    yield posts_dir


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


def _create_post(client, headers, title="Hello", content="World"):
    return client.post("/posts", json={"title": title, "content": content}, headers=headers)


def _make_admin(session_factory, username: str) -> None:
    db = session_factory()
    try:
        user = db.query(models.User).filter(models.User.username == username).first()
        user.is_admin = True
        db.commit()
    finally:
        db.close()


def _create_plan(
    session_factory, *, name="Pro", price=9.99, max_posts=None, max_images=None, max_likes=None, max_comments=None
) -> int:
    db = session_factory()
    try:
        plan = models.SubscriptionPlan(
            name=name,
            slug=name.lower(),
            price=price,
            billing_interval="month",
            max_posts=max_posts,
            max_images=max_images,
            max_likes=max_likes,
            max_comments=max_comments,
            is_active=True,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Plan listing (public)
# ---------------------------------------------------------------------------


class TestPlanListing:
    def test_list_plans_is_public(self, sub_client):
        client, session_factory = sub_client
        _create_plan(session_factory, name="Pro", price=9.99)

        resp = client.get("/subscriptions/plans")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["plans"]]
        assert "Pro" in names

    def test_inactive_plans_are_excluded(self, sub_client):
        client, session_factory = sub_client
        db = session_factory()
        try:
            db.add(
                models.SubscriptionPlan(
                    name="Retired",
                    slug="retired",
                    price=5,
                    billing_interval="month",
                    max_posts=None,
                    max_images=None,
                    max_likes=None,
                    max_comments=None,
                    is_active=False,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/subscriptions/plans")
        names = [p["name"] for p in resp.json()["plans"]]
        assert "Retired" not in names


# ---------------------------------------------------------------------------
# Basic-plan default: every registered user is assigned the Basic plan at
# registration (see app/services/subscription.py), even without ever
# subscribing through the API, and its limits apply from the first request.
# ---------------------------------------------------------------------------


class TestBasicPlanDefaultLimits:
    def test_basic_plan_allows_posts_up_to_its_limit(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        resp = _create_post(client, headers, title="Post 0")
        assert resp.status_code == 201

    def test_basic_plan_blocks_posts_beyond_its_limit(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        assert _create_post(client, headers, title="Post 0").status_code == 201

        resp = _create_post(client, headers, title="One too many")
        assert resp.status_code == 403

    def test_basic_plan_allows_image_upload(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers).json()

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 200

    def test_new_user_is_registered_on_basic_plan(self, sub_client):
        client, _ = sub_client
        register_resp = _register(client, USER_A)
        assert register_resp.status_code == 201

        headers = _auth_headers(client, USER_A)
        me_resp = client.get("/auth/me", headers=headers)
        assert me_resp.status_code == 200

        plans_resp = client.get("/subscriptions/plans")
        basic_plan = next(p for p in plans_resp.json()["plans"] if p["slug"] == "basic")
        assert me_resp.json()["subscription_plan_id"] == basic_plan["id"]

    def test_basic_level_like_cap_blocks_the_next_like(self, sub_client):
        """
        Isolates the like limit specifically: subscribes to a plan with
        Basic's exact like cap (5) but unlimited posts, so post creation
        itself never gets in the way of reaching that cap.
        """
        client, session_factory = sub_client
        plan_id = _create_plan(
            session_factory, name="LikeCapped", price=4.99, max_posts=None, max_images=None, max_likes=5
        )
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)

        # A user can only like a given post once (unique constraint), so
        # reaching the 5-like cumulative cap takes 5 distinct posts.
        post_ids = [_create_post(client, headers, title=f"target {i}").json()["id"] for i in range(6)]

        for post_id in post_ids[:5]:
            assert client.post(f"/posts/{post_id}/like", headers=headers).status_code == 201

        resp = client.post(f"/posts/{post_ids[5]}/like", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "You’ve reached your plan limit. Kindly upgrade your plan to continue."

    def test_basic_level_comment_cap_blocks_the_next_comment(self, sub_client):
        client, session_factory = sub_client
        plan_id = _create_plan(
            session_factory, name="CommentCapped", price=4.99, max_posts=None, max_images=None, max_comments=5
        )
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)
        post = _create_post(client, headers).json()

        for i in range(5):
            resp = client.post(f"/posts/{post['id']}/comments", json={"text": f"comment {i}"}, headers=headers)
            assert resp.status_code == 201

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "one too many"}, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "You’ve reached your plan limit. Kindly upgrade your plan to continue."


# ---------------------------------------------------------------------------
# Subscribing lifts the free-tier limits and generates a billing record.
# ---------------------------------------------------------------------------


class TestSubscribeLiftsLimits:
    def test_subscribing_to_unlimited_plan_allows_more_posts(self, sub_client):
        client, session_factory = sub_client
        plan_id = _create_plan(session_factory, name="Pro", price=9.99, max_posts=None)
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        # Basic (the default) caps posts at 1.
        assert _create_post(client, headers, title="Post 0").status_code == 201
        assert _create_post(client, headers, title="Post 1").status_code == 403

        subscribe_resp = client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)
        assert subscribe_resp.status_code == 201
        assert subscribe_resp.json()["subscription"]["status"] == "active"

        resp = _create_post(client, headers, title="Now allowed")
        assert resp.status_code == 201

    def test_subscribing_creates_a_billing_history_invoice(self, sub_client):
        client, session_factory = sub_client
        plan_id = _create_plan(session_factory, name="Pro", price=9.99)
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)

        resp = client.get("/subscriptions/billing-history", headers=headers)
        assert resp.status_code == 200
        invoices = resp.json()["billing_history"]
        assert len(invoices) == 1
        assert invoices[0]["amount"] == 9.99
        assert invoices[0]["status"] == "paid"
        assert invoices[0]["transaction_id"].startswith("TXN-")
        assert invoices[0]["invoice_url"] is not None

    def test_subscribing_again_cancels_the_previous_subscription(self, sub_client):
        client, session_factory = sub_client
        basic_id = _create_plan(session_factory, name="Basic", price=4.99)
        pro_id = _create_plan(session_factory, name="Pro", price=9.99)
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        client.post("/subscriptions/subscribe", json={"plan_id": basic_id}, headers=headers)
        client.post("/subscriptions/subscribe", json={"plan_id": pro_id}, headers=headers)

        resp = client.get("/subscriptions/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["plan"]["id"] == pro_id

    def test_subscribing_to_unknown_plan_returns_404(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        resp = client.post("/subscriptions/subscribe", json={"plan_id": 999999}, headers=headers)
        assert resp.status_code == 404

    def test_subscribe_requires_authentication(self, sub_client):
        client, session_factory = sub_client
        plan_id = _create_plan(session_factory)

        resp = client.post("/subscriptions/subscribe", json={"plan_id": plan_id})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancelSubscription:
    def test_cancel_marks_subscription_inactive(self, sub_client):
        client, session_factory = sub_client
        plan_id = _create_plan(session_factory)
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers)

        cancel_resp = client.post("/subscriptions/cancel", headers=headers)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "canceled"

        # /me always resolves (every user always has an effective plan --
        # see Sub-Task 11) rather than 404ing; canceling reverts to Basic.
        me_resp = client.get("/subscriptions/me", headers=headers)
        assert me_resp.status_code == 200
        assert me_resp.json()["plan"]["name"] == "Basic"
        assert me_resp.json()["status"] == "default"

    def test_cancel_without_active_subscription_returns_404(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        resp = client.post("/subscriptions/cancel", headers=headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin-only visibility
# ---------------------------------------------------------------------------


class TestAdminVisibility:
    def test_non_admin_cannot_access_admin_routes(self, sub_client):
        client, _ = sub_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        assert client.get("/admin/subscriptions", headers=headers).status_code == 403
        assert client.get("/admin/plans", headers=headers).status_code == 403
        assert client.get("/admin/billing-history", headers=headers).status_code == 403

    def test_admin_can_see_all_subscriptions_and_billing_history(self, sub_client):
        client, session_factory = sub_client
        plan_id = _create_plan(session_factory, name="Pro", price=9.99)

        _register(client, USER_A)
        user_a_headers = _auth_headers(client, USER_A)
        client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=user_a_headers)

        _register(client, USER_B)
        _make_admin(session_factory, USER_B["username"])
        admin_headers = _auth_headers(client, USER_B)

        subs_resp = client.get("/admin/subscriptions", headers=admin_headers)
        assert subs_resp.status_code == 200
        assert len(subs_resp.json()) == 1

        billing_resp = client.get("/admin/billing-history", headers=admin_headers)
        assert billing_resp.status_code == 200
        assert len(billing_resp.json()) == 1

    def test_admin_routes_require_authentication(self, sub_client):
        client, _ = sub_client
        assert client.get("/admin/subscriptions").status_code == 401
