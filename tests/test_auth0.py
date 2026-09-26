"""
Auth0 login (app/auth0.py, app/routers/auth0.py). Auth0 itself is never
contacted: a locally generated RSA key stands in for the tenant's signing
key, and the code->token exchange and JWKS fetch are replaced with fakes.
Each test gets its own in-memory SQLite database, like tests/test_api.py.
"""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk, jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth0 as auth0_service
from app import models
from app.database import Base, get_db
from app.main import app as main_app

DOMAIN = "test-tenant.us.auth0.com"
CLIENT_ID = "test-client-id"
KID = "test-kid"


def _rsa_private_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


SIGNING_KEY_PEM = _rsa_private_pem()
OTHER_KEY_PEM = _rsa_private_pem()


def _public_jwk(private_pem: bytes, kid: str) -> dict:
    public_pem = serialization.load_pem_private_key(private_pem, password=None).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key = jwk.construct(public_pem, "RS256").to_dict()
    key["kid"] = kid
    return key


JWKS = {"keys": [_public_jwk(SIGNING_KEY_PEM, KID)]}


def make_id_token(nonce: str, private_pem: bytes = SIGNING_KEY_PEM, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://{DOMAIN}/",
        "aud": CLIENT_ID,
        "sub": "auth0|abc123",
        "email": "carol@example.com",
        "email_verified": True,
        "nickname": "carol",
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem.decode(), algorithm="RS256", headers={"kid": KID})


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(auth0_service, "AUTH0_DOMAIN", DOMAIN)
    monkeypatch.setattr(auth0_service, "AUTH0_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth0_service, "AUTH0_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(auth0_service, "AUTH0_CALLBACK_URL", "http://testserver/auth0/callback")
    monkeypatch.setattr(auth0_service, "_jwks_cache", None)
    monkeypatch.setattr(auth0_service, "fetch_jwks", lambda: JWKS)


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    yield factory
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


def start_login(client) -> tuple[str, str]:
    resp = client.get("/auth0/login", follow_redirects=False)
    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    return query["state"][0], query["nonce"][0]


def finish_login(client, monkeypatch, state: str, id_token: str) -> dict:
    monkeypatch.setattr(
        auth0_service, "exchange_code_for_tokens", lambda code: {"id_token": id_token, "access_token": "opaque"}
    )
    resp = client.get("/auth0/callback", params={"code": "the-code", "state": state}, follow_redirects=False)
    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.path == "/static/dashboard.html"
    return {k: v[0] for k, v in parse_qs(location.fragment).items()}


def full_login(client, monkeypatch, **claim_overrides) -> dict:
    state, nonce = start_login(client)
    return finish_login(client, monkeypatch, state, make_id_token(nonce, **claim_overrides))


class TestNotConfigured:
    def test_status_reports_disabled(self, client, monkeypatch):
        monkeypatch.setattr(auth0_service, "AUTH0_DOMAIN", "")
        assert client.get("/auth0/status").json() == {"enabled": False}

    @pytest.mark.parametrize("path", ["/auth0/login", "/auth0/callback", "/auth/callback/", "/auth0/logout"])
    def test_routes_return_503(self, client, monkeypatch, path):
        monkeypatch.setattr(auth0_service, "AUTH0_DOMAIN", "")
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 503


class TestLoginRedirect:
    def test_status_reports_enabled(self, configured, client):
        assert client.get("/auth0/status").json() == {"enabled": True}

    def test_login_redirects_to_auth0_authorize(self, configured, client):
        resp = client.get("/auth0/login", follow_redirects=False)
        assert resp.status_code == 302
        location = urlparse(resp.headers["location"])
        assert location.netloc == DOMAIN
        assert location.path == "/authorize"
        query = parse_qs(location.query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["response_type"] == ["code"]
        assert query["redirect_uri"] == ["http://testserver/auth0/callback"]
        assert "openid" in query["scope"][0] and "email" in query["scope"][0]
        assert query["state"][0] and query["nonce"][0]

    def test_login_sets_httponly_state_cookie(self, configured, client):
        resp = client.get("/auth0/login", follow_redirects=False)
        cookie = resp.headers["set-cookie"]
        assert "auth0_login_state=" in cookie
        assert "HttpOnly" in cookie
        # Path=/ so it also reaches /auth/callback/, not just /auth0/callback.
        assert "Path=/;" in cookie or cookie.endswith("Path=/")


class TestCallbackSuccess:
    def test_creates_user_and_returns_working_app_token(self, configured, client, monkeypatch, db_factory):
        fragment = full_login(client, monkeypatch)
        assert fragment["login_method"] == "auth0"
        assert fragment["token_type"] == "bearer"

        me = client.get("/auth/me", headers={"Authorization": f"Bearer {fragment['access_token']}"})
        assert me.status_code == 200
        assert me.json()["username"] == "carol"
        assert me.json()["email"] == "carol@example.com"

        with db_factory() as db:
            user = db.query(models.User).filter_by(email="carol@example.com").one()
            assert user.auth0_sub == "auth0|abc123"
            assert user.subscription_plan.slug == "basic"

    def test_app_token_works_on_other_protected_routes(self, configured, client, monkeypatch):
        token = full_login(client, monkeypatch)["access_token"]
        resp = client.post(
            "/posts", json={"title": "Hello", "content": "From Auth0"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 201

    def test_second_login_reuses_same_user(self, configured, client, monkeypatch, db_factory):
        full_login(client, monkeypatch)
        full_login(client, monkeypatch, email="carol-renamed@example.com")
        with db_factory() as db:
            assert db.query(models.User).count() == 1

    def test_state_cookie_cleared_after_callback(self, configured, client, monkeypatch):
        state, nonce = start_login(client)
        monkeypatch.setattr(auth0_service, "exchange_code_for_tokens", lambda code: {"id_token": make_id_token(nonce)})
        resp = client.get("/auth0/callback", params={"code": "c", "state": state}, follow_redirects=False)
        assert 'auth0_login_state=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]

    def test_username_collision_gets_suffix(self, configured, client, monkeypatch, db_factory):
        client.post(
            "/auth/register", json={"username": "carol", "email": "other@example.com", "password": "strongpass123"}
        )
        full_login(client, monkeypatch)
        with db_factory() as db:
            assert db.query(models.User).filter_by(auth0_sub="auth0|abc123").one().username == "carol2"

    def test_auth0_user_cannot_use_password_login(self, configured, client, monkeypatch):
        full_login(client, monkeypatch)
        resp = client.post("/auth/login", json={"username": "carol", "password": ""})
        assert resp.status_code in (401, 422)


class TestAccountLinking:
    def _register_carol(self, client):
        resp = client.post(
            "/auth/register", json={"username": "carol_local", "email": "carol@example.com", "password": "strongpass123"}
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_verified_email_links_existing_user(self, configured, client, monkeypatch, db_factory):
        local_id = self._register_carol(client)
        token = full_login(client, monkeypatch)["access_token"]
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["id"] == local_id
        with db_factory() as db:
            assert db.query(models.User).count() == 1

    def test_password_login_still_works_after_linking(self, configured, client, monkeypatch):
        self._register_carol(client)
        full_login(client, monkeypatch)
        resp = client.post("/auth/login", json={"username": "carol_local", "password": "strongpass123"})
        assert resp.status_code == 200

    def test_unverified_email_is_not_linked(self, configured, client, monkeypatch, db_factory):
        self._register_carol(client)
        fragment = full_login(client, monkeypatch, email_verified=False)
        assert "access_token" not in fragment
        assert "Verify your email" in fragment["auth0_error"]
        with db_factory() as db:
            assert db.query(models.User).filter_by(email="carol@example.com").one().auth0_sub is None

    def test_second_verified_identity_is_added_to_same_user(self, configured, client, monkeypatch, db_factory):
        # Was "rejected" before user_auth0_identities existed; now a second
        # Auth0 identity with the same verified email is another login for
        # the same account.
        local_id = self._register_carol(client)
        full_login(client, monkeypatch)
        fragment = full_login(client, monkeypatch, sub="google-oauth2|999")
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {fragment['access_token']}"}).json()
        assert me["id"] == local_id
        with db_factory() as db:
            assert db.query(models.User).count() == 1
            subs = {i.auth0_sub for i in db.query(models.UserAuth0Identity).all()}
            assert subs == {"auth0|abc123", "google-oauth2|999"}

    def test_missing_email_is_rejected(self, configured, client, monkeypatch):
        fragment = full_login(client, monkeypatch, email=None)
        assert "access_token" not in fragment
        assert "email" in fragment["auth0_error"]


class TestCallbackRejections:
    def test_state_mismatch(self, configured, client, monkeypatch):
        _, nonce = start_login(client)
        fragment = finish_login(client, monkeypatch, "forged-state", make_id_token(nonce))
        assert "access_token" not in fragment
        assert fragment["auth0_error"] == "Your sign-in couldn't be matched to this browser. Please start again."

    def test_missing_state_cookie(self, configured, client, monkeypatch):
        state, nonce = start_login(client)
        client.cookies.clear()
        fragment = finish_login(client, monkeypatch, state, make_id_token(nonce))
        assert "access_token" not in fragment
        assert "expired" in fragment["auth0_error"]

    def test_nonce_mismatch(self, configured, client, monkeypatch):
        state, _ = start_login(client)
        fragment = finish_login(client, monkeypatch, state, make_id_token("some-other-nonce"))
        assert fragment["auth0_error"] == auth0_service.MSG_VERIFY

    def test_token_signed_by_unknown_key(self, configured, client, monkeypatch):
        state, nonce = start_login(client)
        fragment = finish_login(client, monkeypatch, state, make_id_token(nonce, private_pem=OTHER_KEY_PEM))
        assert fragment["auth0_error"] == auth0_service.MSG_VERIFY

    @pytest.mark.parametrize(
        "overrides,expected",
        [
            ({"aud": "someone-elses-client"}, "MSG_VERIFY"),
            ({"iss": "https://evil.example.com/"}, "MSG_VERIFY"),
            ({"exp": int(time.time()) - 60}, "MSG_EXPIRED"),
        ],
    )
    def test_bad_claims(self, configured, client, monkeypatch, overrides, expected):
        state, nonce = start_login(client)
        fragment = finish_login(client, monkeypatch, state, make_id_token(nonce, **overrides))
        assert fragment["auth0_error"] == getattr(auth0_service, expected)

    def test_hs256_token_rejected(self, configured, client, monkeypatch):
        state, nonce = start_login(client)
        forged = jwt.encode({"sub": "auth0|x", "nonce": nonce}, "secret", algorithm="HS256")
        fragment = finish_login(client, monkeypatch, state, forged)
        assert fragment["auth0_error"] == auth0_service.MSG_VERIFY

    def test_code_exchange_failure(self, configured, client, monkeypatch):
        state, _ = start_login(client)

        def fail(code):
            raise auth0_service.Auth0Error("Auth0 rejected the login code")

        monkeypatch.setattr(auth0_service, "exchange_code_for_tokens", fail)
        resp = client.get("/auth0/callback", params={"code": "c", "state": state}, follow_redirects=False)
        assert "Auth0+rejected" in resp.headers["location"] or "Auth0%20rejected" in resp.headers["location"]

    def test_error_from_auth0_is_forwarded(self, configured, client):
        resp = client.get(
            "/auth0/callback",
            params={"error": "access_denied", "error_description": "User cancelled"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "auth0_error=User+cancelled" in resp.headers["location"]

    def test_raw_auth0_id_token_is_not_an_app_token(self, configured, client):
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {make_id_token('n')}"})
        assert resp.status_code == 401


class TestLogout:
    def test_logout_redirects_to_auth0(self, configured, client):
        resp = client.get("/auth0/logout", follow_redirects=False)
        assert resp.status_code == 302
        location = urlparse(resp.headers["location"])
        assert location.netloc == DOMAIN
        assert location.path == "/v2/logout"
        assert parse_qs(location.query)["client_id"] == [CLIENT_ID]


class TestExistingAuthUnchanged:
    def test_register_and_password_login_still_work(self, configured, client):
        reg = client.post(
            "/auth/register", json={"username": "dave", "email": "dave@example.com", "password": "strongpass123"}
        )
        assert reg.status_code == 201
        assert "auth0_sub" not in reg.json()
        login = client.post("/auth/login", json={"username": "dave", "password": "strongpass123"})
        assert login.status_code == 200
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
        assert me.status_code == 200


class TestAuthCallbackRoute:
    """/auth/callback/ -- the callback path Auth0 is pointed at (AUTH0_CALLBACK_URL)."""

    def _login_via(self, client, monkeypatch, path, **claim_overrides):
        state, nonce = start_login(client)
        id_token = make_id_token(nonce, **claim_overrides)
        monkeypatch.setattr(
            auth0_service, "exchange_code_for_tokens", lambda code: {"id_token": id_token, "access_token": "opaque"}
        )
        return client.get(path, params={"code": "the-code", "state": state}, follow_redirects=False)

    @staticmethod
    def _fragment(resp) -> dict:
        assert resp.status_code == 302
        location = urlparse(resp.headers["location"])
        assert location.path == "/static/dashboard.html"
        return {k: v[0] for k, v in parse_qs(location.fragment).items()}

    def test_route_is_registered(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/auth/callback/" in paths
        assert "get" in paths["/auth/callback/"]
        # The existing auth routes are all still there.
        for existing in ("/auth/register", "/auth/login", "/auth/me"):
            assert existing in paths

    def test_successful_login_through_new_path(self, configured, client, monkeypatch):
        fragment = self._fragment(self._login_via(client, monkeypatch, "/auth/callback/"))
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {fragment['access_token']}"})
        assert me.status_code == 200
        assert me.json()["email"] == "carol@example.com"

    def test_old_auth0_callback_path_still_works(self, configured, client, monkeypatch):
        fragment = self._fragment(self._login_via(client, monkeypatch, "/auth0/callback"))
        assert "access_token" in fragment

    def test_auth0_tokens_are_never_returned_to_browser(self, configured, client, monkeypatch):
        resp = self._login_via(client, monkeypatch, "/auth/callback/")
        location = resp.headers["location"]
        fragment = self._fragment(resp)
        assert set(fragment) == {"access_token", "token_type", "login_method"}
        assert "opaque" not in location  # Auth0's access token
        assert "the-code" not in location
        assert "test-secret" not in location

    @pytest.mark.parametrize(
        "params",
        [{}, {"state": "s"}, {"code": "c"}, {"code": "", "state": ""}],
        ids=["no-params", "missing-code", "missing-state", "empty-values"],
    )
    def test_missing_parameters(self, configured, client, params):
        resp = client.get("/auth/callback/", params=params, follow_redirects=False)
        fragment = self._fragment(resp)
        assert "access_token" not in fragment
        assert fragment["auth0_error"] == "Login response from Auth0 was incomplete. Please try again."

    def test_oversized_parameters(self, configured, client):
        resp = client.get("/auth/callback/", params={"code": "x" * 5000, "state": "s"}, follow_redirects=False)
        assert self._fragment(resp)["auth0_error"] == "Login response from Auth0 was invalid. Please try again."

    def test_forged_state(self, configured, client, monkeypatch):
        start_login(client)
        resp = client.get("/auth/callback/", params={"code": "c", "state": "forged"}, follow_redirects=False)
        assert "couldn't be matched to this browser" in self._fragment(resp)["auth0_error"]

    def test_no_login_started(self, configured, client):
        resp = client.get("/auth/callback/", params={"code": "c", "state": "s"}, follow_redirects=False)
        assert "expired" in self._fragment(resp)["auth0_error"]

    def test_invalid_id_token(self, configured, client, monkeypatch):
        resp = self._login_via(client, monkeypatch, "/auth/callback/", aud="someone-else")
        assert self._fragment(resp)["auth0_error"] == auth0_service.MSG_VERIFY

    def test_auth0_error_parameter(self, configured, client):
        resp = client.get(
            "/auth/callback/", params={"error": "access_denied", "error_description": "User cancelled"},
            follow_redirects=False,
        )
        assert self._fragment(resp)["auth0_error"] == "User cancelled"

    def test_rejection_logs_reason_but_not_secrets(self, configured, client, caplog):
        with caplog.at_level("WARNING", logger="app.routers.auth0"):
            client.get("/auth/callback/", params={"code": "secret-code-value", "state": "s"}, follow_redirects=False)
        assert "Auth0 callback rejected" in caplog.text
        assert "secret-code-value" not in caplog.text
        assert "test-secret" not in caplog.text

    def test_trailing_slash_less_path_redirects_to_callback(self, configured, client):
        resp = client.get("/auth/callback", params={"code": "c", "state": "s"}, follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert urlparse(resp.headers["location"]).path == "/auth/callback/"


class TestLocalUserMapping:
    """app.auth0.get_or_create_user -- how an Auth0 identity maps onto a local users row."""

    @staticmethod
    def _claims(**overrides):
        claims = {"sub": "auth0|map1", "email": "carol@example.com", "email_verified": True, "nickname": "carol"}
        claims.update(overrides)
        return claims

    @staticmethod
    def _local_user(db, username, email, password="strongpass123"):
        from app.auth import hash_password
        from app.services import subscription as subscription_service

        user = models.User(
            username=username,
            email=email,
            password_hash=hash_password(password),
            subscription_plan_id=subscription_service.get_or_create_basic_plan(db).id,
        )
        db.add(user)
        db.commit()
        return user

    @pytest.mark.parametrize(
        "sub,provider",
        [("google-oauth2|1161", "google-oauth2"), ("auth0|abc", "auth0"), ("facebook|9", "facebook"), ("odd", "unknown")],
    )
    def test_provider_derived_from_sub(self, sub, provider):
        assert auth0_service.auth0_provider(sub) == provider

    def test_first_login_creates_exactly_one_user(self, db_factory):
        with db_factory() as db:
            user = auth0_service.get_or_create_user(db, self._claims())
            assert (user.username, user.email, user.auth0_sub) == ("carol", "carol@example.com", "auth0|map1")
            assert db.query(models.User).count() == 1

    def test_repeat_login_reuses_user(self, db_factory):
        with db_factory() as db:
            first = auth0_service.get_or_create_user(db, self._claims())
            again = auth0_service.get_or_create_user(db, self._claims(nickname="changed", email="new@example.com"))
            assert again.id == first.id
            assert db.query(models.User).count() == 1

    def test_name_used_when_no_nickname(self, db_factory):
        with db_factory() as db:
            user = auth0_service.get_or_create_user(db, self._claims(nickname=None, name="Carol Danvers"))
            assert user.username == "CarolDanvers"

    def test_unverified_email_still_creates_new_account_when_no_match(self, db_factory):
        with db_factory() as db:
            user = auth0_service.get_or_create_user(db, self._claims(email_verified=False))
            assert user.auth0_sub == "auth0|map1"

    def test_linking_preserves_existing_account(self, db_factory):
        with db_factory() as db:
            local = self._local_user(db, "carol_local", "carol@example.com")
            before = (local.id, local.username, local.email, local.password_hash, local.subscription_plan_id, local.is_admin)
            linked = auth0_service.get_or_create_user(db, self._claims(sub="google-oauth2|55"))
            after = (linked.id, linked.username, linked.email, linked.password_hash, linked.subscription_plan_id, linked.is_admin)
            assert after == before
            assert linked.auth0_sub == "google-oauth2|55"
            assert db.query(models.User).count() == 1

    def test_email_match_is_case_insensitive(self, db_factory):
        with db_factory() as db:
            local = self._local_user(db, "carol_local", "Carol@Example.COM")
            linked = auth0_service.get_or_create_user(db, self._claims())
            assert linked.id == local.id
            assert linked.email == "Carol@Example.COM"  # stored email left as the user typed it
            assert db.query(models.User).count() == 1

    def test_case_insensitive_match_still_requires_verified_email(self, db_factory):
        with db_factory() as db:
            self._local_user(db, "carol_local", "Carol@Example.com")
            with pytest.raises(auth0_service.Auth0Error, match="Verify your email"):
                auth0_service.get_or_create_user(db, self._claims(email_verified=False))
            assert db.query(models.User).count() == 1

    def test_ambiguous_email_is_refused_not_guessed(self, db_factory):
        with db_factory() as db:
            self._local_user(db, "carol_a", "carol@example.com")
            self._local_user(db, "carol_b", "CAROL@example.com")
            with pytest.raises(auth0_service.Auth0Error, match="More than one account"):
                auth0_service.get_or_create_user(db, self._claims())
            assert db.query(models.User).filter(models.User.auth0_sub.isnot(None)).count() == 0
            assert db.query(models.User).count() == 2

    def test_concurrent_first_logins_reuse_winner(self, db_factory):
        from app.services import subscription as subscription_service

        with db_factory() as winner_db, db_factory() as loser_db:
            winner = auth0_service.get_or_create_user(winner_db, self._claims(sub="auth0|race"))
            # The losing request got past its lookups before the winner committed,
            # so it now tries to insert a second row for the same Auth0 identity.
            loser_db.add(
                models.User(
                    username="carol_dup",
                    email="carol-dup@example.com",
                    password_hash="x",
                    subscription_plan_id=subscription_service.get_or_create_basic_plan(loser_db).id,
                    auth0_sub="auth0|race",
                )
            )
            reused = auth0_service._commit_or_reuse(loser_db, "auth0|race")
            assert reused is not None and reused.id == winner.id
            assert loser_db.query(models.User).count() == 1


class TestGoogleLogin:
    """"Continue with Google": /auth0/login?provider=google -> Auth0 -> Google -> /auth/callback/."""

    GOOGLE_SUB = "google-oauth2|116171451139749918706"

    def _google_login(self, client, monkeypatch, **claim_overrides):
        resp = client.get("/auth0/login", params={"provider": "google"}, follow_redirects=False)
        assert resp.status_code == 302
        query = parse_qs(urlparse(resp.headers["location"]).query)
        claims = {"sub": self.GOOGLE_SUB, "email": "gina@gmail.com", "nickname": "gina", **claim_overrides}
        id_token = make_id_token(query["nonce"][0], **claims)
        monkeypatch.setattr(
            auth0_service, "exchange_code_for_tokens", lambda code: {"id_token": id_token, "access_token": "opaque"}
        )
        cb = client.get("/auth/callback/", params={"code": "c", "state": query["state"][0]}, follow_redirects=False)
        assert cb.status_code == 302
        location = urlparse(cb.headers["location"])
        assert location.path == "/static/dashboard.html"  # the existing authenticated area
        return {k: v[0] for k, v in parse_qs(location.fragment).items()}

    def test_google_login_redirects_to_auth0_with_google_connection(self, configured, client):
        resp = client.get("/auth0/login", params={"provider": "google"}, follow_redirects=False)
        assert resp.status_code == 302
        location = urlparse(resp.headers["location"])
        assert (location.netloc, location.path) == (DOMAIN, "/authorize")
        query = parse_qs(location.query)
        assert query["connection"] == ["google-oauth2"]
        assert query["response_type"] == ["code"]
        assert query["state"][0] and query["nonce"][0]
        assert "auth0_login_state=" in resp.headers["set-cookie"]

    def test_plain_auth0_login_has_no_connection(self, configured, client):
        resp = client.get("/auth0/login", follow_redirects=False)
        assert "connection" not in parse_qs(urlparse(resp.headers["location"]).query)

    @pytest.mark.parametrize("provider", ["twitter", "google-oauth2", "Facebook", "evil", ""])
    def test_unlisted_provider_is_refused(self, configured, client, provider):
        resp = client.get("/auth0/login", params={"provider": provider}, follow_redirects=False)
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Unsupported login provider"}

    def test_google_login_not_configured_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(auth0_service, "AUTH0_DOMAIN", "")
        assert client.get("/auth0/login", params={"provider": "google"}, follow_redirects=False).status_code == 503

    def test_first_google_login_creates_local_user(self, configured, client, monkeypatch, db_factory):
        fragment = self._google_login(client, monkeypatch)
        assert fragment["login_method"] == "auth0"
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {fragment['access_token']}"})
        assert me.status_code == 200
        assert (me.json()["username"], me.json()["email"]) == ("gina", "gina@gmail.com")
        with db_factory() as db:
            user = db.query(models.User).one()
            assert user.auth0_sub == self.GOOGLE_SUB
            assert auth0_service.auth0_provider(user.auth0_sub) == "google-oauth2"
            assert user.subscription_plan.slug == "basic"

    def test_repeat_google_login_reuses_user(self, configured, client, monkeypatch, db_factory):
        first = self._google_login(client, monkeypatch)
        second = self._google_login(client, monkeypatch)
        ids = {
            client.get("/auth/me", headers={"Authorization": f"Bearer {f['access_token']}"}).json()["id"]
            for f in (first, second)
        }
        assert len(ids) == 1
        with db_factory() as db:
            assert db.query(models.User).count() == 1

    def test_google_login_links_existing_password_user(self, configured, client, monkeypatch, db_factory):
        reg = client.post(
            "/auth/register", json={"username": "gina_local", "email": "Gina@Gmail.com", "password": "strongpass123"}
        )
        fragment = self._google_login(client, monkeypatch)
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {fragment['access_token']}"}).json()
        assert me["id"] == reg.json()["id"]
        # ...and the email/password login for that same account still works.
        login = client.post("/auth/login", json={"username": "gina_local", "password": "strongpass123"})
        assert login.status_code == 200
        with db_factory() as db:
            assert db.query(models.User).count() == 1

    def test_google_user_cancel_is_handled(self, configured, client):
        client.get("/auth0/login", params={"provider": "google"}, follow_redirects=False)
        resp = client.get(
            "/auth/callback/",
            params={"error": "access_denied", "error_description": "User did not authorize the request"},
            follow_redirects=False,
        )
        fragment = parse_qs(urlparse(resp.headers["location"]).fragment)
        assert "access_token" not in fragment
        assert fragment["auth0_error"] == ["User did not authorize the request"]

    def test_google_token_for_wrong_app_is_rejected(self, configured, client, monkeypatch):
        fragment = self._google_login(client, monkeypatch, aud="another-app")
        assert fragment == {"auth0_error": auth0_service.MSG_VERIFY}

    def test_logout_after_google_login(self, configured, client, monkeypatch):
        self._google_login(client, monkeypatch)
        resp = client.get("/auth0/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert urlparse(resp.headers["location"]).path == "/v2/logout"

    def test_dashboard_has_google_button_and_unchanged_password_login(self, client):
        page = client.get("/static/dashboard.html").text
        assert 'id="google-login-btn"' in page
        assert "Continue with Google" in page
        assert '"/auth0/login?provider=google"' in page
        # Existing password login still wired: usernames still go to /auth/login.
        assert '<button id="login-btn" type="button">Log in</button>' in page
        assert '"/auth/login/email" : "/auth/login"' in page


class TestFacebookLogin:
    """"Continue with Facebook": same Auth0 flow, callback and user mapping as Google."""

    FB_SUB = "facebook|10223344556677889"

    def _social_login(self, client, monkeypatch, provider="facebook", **claims):
        resp = client.get("/auth0/login", params={"provider": provider}, follow_redirects=False)
        assert resp.status_code == 302
        query = parse_qs(urlparse(resp.headers["location"]).query)
        base = {"sub": self.FB_SUB, "email": "frank@example.com", "email_verified": True, "nickname": "frank"}
        id_token = make_id_token(query["nonce"][0], **{**base, **claims})
        monkeypatch.setattr(
            auth0_service, "exchange_code_for_tokens", lambda code: {"id_token": id_token, "access_token": "opaque"}
        )
        cb = client.get("/auth/callback/", params={"code": "c", "state": query["state"][0]}, follow_redirects=False)
        assert cb.status_code == 302
        location = urlparse(cb.headers["location"])
        assert location.path == "/static/dashboard.html"
        return {k: v[0] for k, v in parse_qs(location.fragment).items()}

    def _me(self, client, fragment):
        return client.get("/auth/me", headers={"Authorization": f"Bearer {fragment['access_token']}"})

    def test_facebook_login_redirects_to_auth0_with_facebook_connection(self, configured, client):
        resp = client.get("/auth0/login", params={"provider": "facebook"}, follow_redirects=False)
        location = urlparse(resp.headers["location"])
        assert (location.netloc, location.path) == (DOMAIN, "/authorize")
        query = parse_qs(location.query)
        assert query["connection"] == ["facebook"]
        assert query["redirect_uri"] == ["http://testserver/auth0/callback"]  # AUTH0_CALLBACK_URL from config
        assert "auth0_login_state=" in resp.headers["set-cookie"]

    def test_connection_name_comes_from_config(self, configured, client, monkeypatch):
        monkeypatch.setitem(auth0_service.SOCIAL_CONNECTIONS, "facebook", "facebook-custom")
        resp = client.get("/auth0/login", params={"provider": "facebook"}, follow_redirects=False)
        assert parse_qs(urlparse(resp.headers["location"]).query)["connection"] == ["facebook-custom"]

    def test_first_facebook_login_creates_local_user(self, configured, client, monkeypatch, db_factory):
        fragment = self._social_login(client, monkeypatch)
        me = self._me(client, fragment)
        assert me.status_code == 200
        assert (me.json()["username"], me.json()["email"]) == ("frank", "frank@example.com")
        with db_factory() as db:
            user = db.query(models.User).one()
            assert user.auth0_sub == self.FB_SUB
            assert auth0_service.auth0_provider(user.auth0_sub) == "facebook"
            assert user.subscription_plan.slug == "basic"

    def test_repeat_facebook_login_reuses_user(self, configured, client, monkeypatch, db_factory):
        first = self._me(client, self._social_login(client, monkeypatch)).json()["id"]
        second = self._me(client, self._social_login(client, monkeypatch, nickname="renamed")).json()["id"]
        assert first == second
        with db_factory() as db:
            assert db.query(models.User).count() == 1

    def test_facebook_links_existing_password_user_with_verified_email(self, configured, client, monkeypatch, db_factory):
        reg = client.post(
            "/auth/register", json={"username": "frank_local", "email": "Frank@Example.com", "password": "strongpass123"}
        )
        assert self._me(client, self._social_login(client, monkeypatch)).json()["id"] == reg.json()["id"]
        assert client.post("/auth/login", json={"username": "frank_local", "password": "strongpass123"}).status_code == 200
        with db_factory() as db:
            assert db.query(models.User).count() == 1

    def test_unverified_facebook_email_does_not_take_over_existing_user(self, configured, client, monkeypatch, db_factory):
        client.post(
            "/auth/register", json={"username": "frank_local", "email": "frank@example.com", "password": "strongpass123"}
        )
        fragment = self._social_login(client, monkeypatch, email_verified=False)
        assert "access_token" not in fragment
        assert "Verify your email" in fragment["auth0_error"]
        with db_factory() as db:
            assert db.query(models.User).count() == 1
            assert db.query(models.User).one().auth0_sub is None

    def test_facebook_account_without_email_is_refused(self, configured, client, monkeypatch, db_factory):
        # Facebook accounts registered with a phone number share no email.
        fragment = self._social_login(client, monkeypatch, email=None)
        assert "access_token" not in fragment
        assert "email" in fragment["auth0_error"]
        with db_factory() as db:
            assert db.query(models.User).count() == 0

    def test_same_person_on_google_then_facebook_gets_no_duplicate(self, configured, client, monkeypatch, db_factory):
        google = self._social_login(client, monkeypatch, provider="google", sub="google-oauth2|777")
        assert self._me(client, google).status_code == 200
        facebook = self._social_login(client, monkeypatch)  # same verified email, different Auth0 identity
        # Both logins land on the one account -- no duplicate, no refusal.
        assert self._me(client, facebook).json()["id"] == self._me(client, google).json()["id"]
        with db_factory() as db:
            assert db.query(models.User).count() == 1
            user = db.query(models.User).one()
            assert user.auth0_sub == "google-oauth2|777"  # first link kept as-is
            assert auth0_service.linked_subs(db, user.id) == {"google-oauth2|777", self.FB_SUB}

    def test_google_login_still_works(self, configured, client, monkeypatch):
        fragment = self._social_login(client, monkeypatch, provider="google", sub="google-oauth2|888", email="g@example.com")
        assert self._me(client, fragment).status_code == 200

    def test_facebook_connection_not_enabled_is_friendly(self, configured, client):
        client.get("/auth0/login", params={"provider": "facebook"}, follow_redirects=False)
        resp = client.get(
            "/auth/callback/",
            params={"error": "invalid_request", "error_description": "the connection is not enabled"},
            follow_redirects=False,
        )
        fragment = parse_qs(urlparse(resp.headers["location"]).fragment)
        assert fragment == {
            "auth0_error": ["Facebook sign-in isn't available right now. Please use another way to log in."]
        }

    def test_facebook_cancel_without_description(self, configured, client):
        client.get("/auth0/login", params={"provider": "facebook"}, follow_redirects=False)
        resp = client.get("/auth/callback/", params={"error": "access_denied"}, follow_redirects=False)
        assert parse_qs(urlparse(resp.headers["location"]).fragment) == {"auth0_error": ["Facebook sign-in was cancelled."]}

    def test_facebook_token_for_wrong_app_is_rejected(self, configured, client, monkeypatch):
        assert self._social_login(client, monkeypatch, aud="other-app") == {"auth0_error": auth0_service.MSG_VERIFY}

    def test_logout_after_facebook_login(self, configured, client, monkeypatch):
        self._social_login(client, monkeypatch)
        resp = client.get("/auth0/logout", follow_redirects=False)
        assert urlparse(resp.headers["location"]).path == "/v2/logout"

    def test_dashboard_has_facebook_button_beside_google(self, client):
        page = client.get("/static/dashboard.html").text
        assert '"/auth0/login?provider=facebook"' in page
        google, facebook = page.index('id="google-login-btn"'), page.index('id="facebook-login-btn"')
        auth0 = page.index('id="auth0-login-btn"')
        assert page.index('id="login-btn"') < google < facebook < auth0 < page.index('id="login-error"')
        assert '<button id="login-btn" type="button">Log in</button>' in page
        assert '"/auth0/login?provider=google"' in page


class TestMultipleLoginsPerAccount:
    """One local user, several Auth0 identities (models.UserAuth0Identity)."""

    def test_user_model_relationships_untouched(self):
        # Append-only: the link lives on UserAuth0Identity, not on User.
        from sqlalchemy import inspect

        assert "auth0_identities" not in inspect(models.User).relationships.keys()

    GOOGLE = {"sub": "google-oauth2|g1", "email": "multi@example.com", "email_verified": True, "nickname": "multi"}
    FACEBOOK = {"sub": "facebook|f1", "email": "multi@example.com", "email_verified": True, "nickname": "Multi User"}
    DATABASE = {"sub": "auth0|d1", "email": "MULTI@example.com", "email_verified": True, "nickname": "m"}

    def test_three_providers_one_account(self, db_factory):
        with db_factory() as db:
            ids = {auth0_service.get_or_create_user(db, c).id for c in (self.GOOGLE, self.FACEBOOK, self.DATABASE)}
            assert len(ids) == 1
            user = db.query(models.User).one()
            assert auth0_service.linked_subs(db, user.id) == {"google-oauth2|g1", "facebook|f1", "auth0|d1"}
            assert user.auth0_sub == "google-oauth2|g1"  # first link, unchanged by later ones

    def test_each_provider_reuses_account_on_repeat(self, db_factory):
        with db_factory() as db:
            first = auth0_service.get_or_create_user(db, self.GOOGLE)
            auth0_service.get_or_create_user(db, self.FACEBOOK)
            for claims in (self.GOOGLE, self.FACEBOOK, self.GOOGLE, self.FACEBOOK):
                assert auth0_service.get_or_create_user(db, claims).id == first.id
            assert db.query(models.User).count() == 1
            assert db.query(models.UserAuth0Identity).count() == 2

    def test_repeat_login_uses_identity_even_after_email_changes(self, db_factory):
        with db_factory() as db:
            first = auth0_service.get_or_create_user(db, self.FACEBOOK)
            again = auth0_service.get_or_create_user(db, {**self.FACEBOOK, "email": None})
            assert again.id == first.id  # already-linked identity doesn't need the email again

    def test_unverified_second_provider_is_still_refused(self, db_factory):
        with db_factory() as db:
            auth0_service.get_or_create_user(db, self.GOOGLE)
            with pytest.raises(auth0_service.Auth0Error, match="Verify your email"):
                auth0_service.get_or_create_user(db, {**self.FACEBOOK, "email_verified": False})
            assert db.query(models.UserAuth0Identity).count() == 1

    def test_password_user_can_add_several_social_logins(self, db_factory):
        with db_factory() as db:
            local = TestLocalUserMapping._local_user(db, "multi_local", "multi@example.com")
            pw_hash = local.password_hash
            for claims in (self.GOOGLE, self.FACEBOOK):
                assert auth0_service.get_or_create_user(db, claims).id == local.id
            db.refresh(local)
            assert local.password_hash == pw_hash
            assert len(auth0_service.linked_subs(db, local.id)) == 2

    def test_legacy_auth0_sub_only_link_is_found_and_backfilled(self, db_factory):
        # A user linked before user_auth0_identities existed: only users.auth0_sub is set.
        with db_factory() as db:
            local = TestLocalUserMapping._local_user(db, "legacy", "legacy@example.com")
            local.auth0_sub = "google-oauth2|legacy"
            db.commit()
            assert db.query(models.UserAuth0Identity).count() == 0
            user = auth0_service.get_or_create_user(
                db, {"sub": "google-oauth2|legacy", "email": "legacy@example.com", "email_verified": True}
            )
            assert user.id == local.id
            assert [i.auth0_sub for i in db.query(models.UserAuth0Identity).all()] == ["google-oauth2|legacy"]

    def test_identity_rows_deleted_with_user(self, db_factory):
        from sqlalchemy import text

        with db_factory() as db:
            # Removal is the database's ON DELETE CASCADE (no ORM relationship
            # on User); SQLite only enforces it with this pragma, which the
            # app's own engine sets on every connection (app/database.py).
            db.execute(text("PRAGMA foreign_keys=ON"))
            user = auth0_service.get_or_create_user(db, self.GOOGLE)
            auth0_service.get_or_create_user(db, self.FACEBOOK)
            db.delete(user)
            db.commit()
            assert db.query(models.UserAuth0Identity).count() == 0

    def test_identity_cannot_belong_to_two_users(self, db_factory):
        from sqlalchemy.exc import IntegrityError

        with db_factory() as db:
            a = TestLocalUserMapping._local_user(db, "a", "a@example.com")
            b = TestLocalUserMapping._local_user(db, "b", "b@example.com")
            db.add(models.UserAuth0Identity(user_id=a.id, auth0_sub="facebook|same"))
            db.commit()
            db.add(models.UserAuth0Identity(user_id=b.id, auth0_sub="facebook|same"))
            with pytest.raises(IntegrityError):
                db.commit()


class TestFacebookPromptConsent:
    """Facebook's /authorize request carries prompt=consent; nothing else does."""

    @staticmethod
    def _query(client, **params):
        resp = client.get("/auth0/login", params=params, follow_redirects=False)
        assert resp.status_code == 302
        return parse_qs(urlparse(resp.headers["location"]).query)

    def test_facebook_request_has_prompt_consent(self, configured, client):
        query = self._query(client, provider="facebook")
        assert query["prompt"] == ["consent"]
        assert query["connection"] == ["facebook"]
        assert query["response_type"] == ["code"]

    def test_google_request_has_no_prompt(self, configured, client):
        assert "prompt" not in self._query(client, provider="google")

    def test_plain_auth0_request_has_no_prompt(self, configured, client):
        assert "prompt" not in self._query(client)

    def test_extra_params_cannot_override_security_params(self, configured):
        url = auth0_service.build_authorize_url(
            "real-state", "real-nonce", "facebook",
            {"prompt": "consent", "state": "evil", "redirect_uri": "https://evil.example", "response_type": "token"},
        )
        query = parse_qs(urlparse(url).query)
        assert query["state"] == ["real-state"]
        assert query["redirect_uri"] == ["http://testserver/auth0/callback"]
        assert query["response_type"] == ["code"]
        assert query["prompt"] == ["consent"]
