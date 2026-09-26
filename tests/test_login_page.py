"""
Login page update: email sign-in (POST /auth/login/email, app/routers/auth_email.py),
the dashboard's sign-up view, and plain-language Auth0 errors. The existing
POST /auth/login and /auth/register are exercised unchanged alongside.
Each test gets its own in-memory SQLite database.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth0 as auth0_service
from app import models
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app as main_app
from app.services import subscription as subscription_service


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture()
def client(db_factory):
    def override_get_db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    main_app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_app) as test_client:
        yield test_client
    main_app.dependency_overrides.clear()


def register(client, username="dana", email="Dana@Example.com", password="strongpass123"):
    resp = client.post("/auth/register", json={"username": username, "email": email, "password": password})
    assert resp.status_code == 201
    return resp.json()


class TestEmailLogin:
    def test_email_login_returns_working_token(self, client):
        user = register(client)
        resp = client.post("/auth/login/email", json={"email": "Dana@Example.com", "password": "strongpass123"})
        assert resp.status_code == 200
        assert resp.json()["token_type"] == "bearer"
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"})
        assert me.status_code == 200
        assert me.json()["id"] == user["id"]

    def test_email_is_case_insensitive(self, client):
        register(client)
        resp = client.post("/auth/login/email", json={"email": "dana@example.COM", "password": "strongpass123"})
        assert resp.status_code == 200

    def test_same_token_kind_as_username_login(self, client):
        register(client)
        by_email = client.post("/auth/login/email", json={"email": "dana@example.com", "password": "strongpass123"}).json()
        by_name = client.post("/auth/login", json={"username": "dana", "password": "strongpass123"}).json()
        for token in (by_email["access_token"], by_name["access_token"]):
            assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["username"] == "dana"

    @pytest.mark.parametrize("email,password", [("dana@example.com", "wrong-password"), ("nobody@example.com", "strongpass123")])
    def test_wrong_credentials_give_same_401(self, client, email, password):
        register(client)
        resp = client.post("/auth/login/email", json={"email": email, "password": password})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Incorrect email or password"

    @pytest.mark.parametrize("payload", [{"email": "not-an-email", "password": "x"}, {"email": "a@b.com", "password": ""}, {}])
    def test_invalid_input_is_422(self, client, payload):
        assert client.post("/auth/login/email", json=payload).status_code == 422

    def test_social_only_user_cannot_use_email_login(self, client, db_factory):
        # Auth0-created users get a random password nobody knows.
        with db_factory() as db:
            auth0_service.get_or_create_user(db, {"sub": "facebook|1", "email": "fb@example.com", "email_verified": True, "nickname": "fb"})
        resp = client.post("/auth/login/email", json={"email": "fb@example.com", "password": "anything123"})
        assert resp.status_code == 401

    def test_emails_differing_only_by_case_are_refused(self, client, db_factory):
        with db_factory() as db:
            plan = subscription_service.get_or_create_basic_plan(db)
            for name, email in (("a", "same@example.com"), ("b", "SAME@example.com")):
                db.add(models.User(username=name, email=email, password_hash=hash_password("strongpass123"), subscription_plan_id=plan.id))
            db.commit()
        resp = client.post("/auth/login/email", json={"email": "same@example.com", "password": "strongpass123"})
        assert resp.status_code == 401
        # Still reachable by username.
        assert client.post("/auth/login", json={"username": "a", "password": "strongpass123"}).status_code == 200


class TestExistingAuthUnchanged:
    def test_username_login_still_works(self, client):
        register(client)
        assert client.post("/auth/login", json={"username": "dana", "password": "strongpass123"}).status_code == 200

    def test_username_login_does_not_accept_email_field(self, client):
        # /auth/login keeps its original username-only contract.
        register(client)
        resp = client.post("/auth/login", json={"username": "Dana@Example.com", "password": "strongpass123"})
        assert resp.status_code == 401

    def test_register_duplicate_messages_unchanged(self, client):
        register(client)
        dup = client.post("/auth/register", json={"username": "dana", "email": "x@example.com", "password": "strongpass123"})
        assert (dup.status_code, dup.json()["detail"]) == (400, "Username already registered")


class TestAuth0ErrorsArePlain:
    @pytest.fixture()
    def configured(self, monkeypatch):
        monkeypatch.setattr(auth0_service, "AUTH0_DOMAIN", "t.auth0.com")
        monkeypatch.setattr(auth0_service, "AUTH0_CLIENT_ID", "cid")
        monkeypatch.setattr(auth0_service, "AUTH0_CLIENT_SECRET", "secret")

    def _error(self, client, **params):
        resp = client.get("/auth/callback/", params=params, follow_redirects=False)
        return parse_qs(urlparse(resp.headers["location"]).fragment)["auth0_error"][0]

    @pytest.mark.parametrize("error,description", [
        ("server_error", "Unhandled exception in rule xyz: TypeError at line 42"),
        ("invalid_request", "Parameter 'redirect_uri' is invalid"),
        ("unauthorized", None),
    ])
    def test_technical_auth0_errors_are_not_shown(self, configured, client, error, description):
        params = {"error": error, **({"error_description": description} if description else {})}
        message = self._error(client, **params)
        assert message == "Sign-in didn't complete. Please try again or use another way to log in."
        if description:
            assert description not in message

    def test_cancel_messages_still_shown(self, configured, client):
        assert self._error(client, error="access_denied", error_description="User cancelled") == "User cancelled"
        assert self._error(client, error="access_denied") == "Sign-in was cancelled."


class TestLoginPageMarkup:
    @pytest.fixture()
    def page(self, client):
        return client.get("/static/dashboard.html").text

    def test_login_form_fields(self, page):
        assert '<label for="login-username">Email or username</label>' in page
        assert 'id="login-password" type="password"' in page
        assert '<button id="login-btn" type="button">Log in</button>' in page

    def test_order_login_then_social_then_signup_link(self, page):
        order = [page.index(marker) for marker in (
            'id="login-btn"', 'or continue with', 'id="google-login-btn"', 'id="facebook-login-btn"', 'id="show-signup"',
        )]
        assert order == sorted(order)

    def test_signup_view_uses_existing_register_endpoint(self, page):
        for marker in ('id="signup-view"', 'id="signup-username"', 'id="signup-email" type="email"',
                       'id="signup-password" type="password"', 'id="signup-btn"', 'id="show-login"', 'fetch("/auth/register"'):
            assert marker in page

    def test_email_goes_to_new_endpoint_username_to_old(self, page):
        assert '"/auth/login/email"' in page
        assert '"/auth/login"' in page

    def test_all_three_auth0_options_present(self, page):
        # Google, Facebook, and Auth0's own login page (no provider).
        assert '"/auth0/login?provider=google"' in page
        assert '"/auth0/login?provider=facebook"' in page
        assert '<button id="auth0-login-btn" type="button">Continue with Auth0</button>' in page
        assert 'window.location.href = "/auth0/login";' in page

    def test_errors_are_announced_and_responsive_rule_exists(self, page):
        assert '<p id="login-error" role="alert"></p>' in page
        assert '<p id="signup-error" role="alert"></p>' in page
        assert "#login-panel { margin: 24px 16px; padding: 20px; }" in page

    def test_no_raw_server_detail_rendered(self, page):
        # The old code rendered body.detail straight into the page.
        assert 'throw new Error(body.detail || "Login failed")' not in page
