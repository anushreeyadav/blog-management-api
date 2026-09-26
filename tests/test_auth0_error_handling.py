"""
Error handling of the Auth0 / Google / Facebook login flow (app/auth0.py,
app/routers/auth0.py): every failure lands the user back on the dashboard
with a plain message, the server log gets the technical reason, and no
secret, token, authorization code or stack trace ever reaches the browser.

Reuses the fake Auth0 tenant (signing key, fixtures) from tests/test_auth0.py.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app import auth0 as auth0_service
from app import models
from app.routers import auth0 as auth0_router
from tests.test_auth0 import client, configured, db_factory, make_id_token  # noqa: F401 (pytest fixtures)

SECRET = "TOPSECRET-client-secret-123"
ACCESS = "ACCESS-TOKEN-xyz-789"
CODE = "AUTHCODE-abc-456"
# Captured at import, before fixtures replace it with a fake.
REAL_FETCH_JWKS = auth0_service.fetch_jwks


@pytest.fixture()
def secret_config(configured, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth0_service, "AUTH0_CLIENT_SECRET", SECRET)


def start(client, provider=None):  # noqa: F811
    resp = client.get("/auth0/login", params={"provider": provider} if provider else None, follow_redirects=False)
    assert resp.status_code == 302
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


def callback(client, **params):  # noqa: F811
    resp = client.get("/auth/callback/", params=params, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert urlparse(location).path == "/static/dashboard.html"
    return location, {k: v[0] for k, v in parse_qs(urlparse(location).fragment).items()}


def fake_token_endpoint(monkeypatch, response=None, raises=None):
    def post(url, data=None, timeout=None):
        assert url.endswith("/oauth/token")
        if raises:
            raise raises
        return response
    monkeypatch.setattr(auth0_service.httpx, "post", post)


def good_tokens(login_nonce, **claims):
    # A "nonce" in claims overrides the login's own (replay test).
    nonce = claims.pop("nonce", login_nonce)
    return httpx.Response(200, json={"id_token": make_id_token(nonce, **claims), "access_token": ACCESS})


def app_log_text(caplog):
    # The app's own log lines (app.*). The web server's access log is
    # covered separately by the RedactCallbackQuery tests below; the test
    # client's own request logging is not part of the running app.
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))


def assert_nothing_leaked(location, caplog):
    logged = app_log_text(caplog)
    for secret in (SECRET, ACCESS, CODE):
        assert secret not in location
        assert secret not in logged
    assert "Traceback" not in location


# 1. Missing callback parameters ------------------------------------------------
@pytest.mark.parametrize("params", [{}, {"code": CODE}, {"state": "s"}])
def test_1_missing_parameters(secret_config, client, params):  # noqa: F811
    start(client, "google")
    _, frag = callback(client, **params)
    assert frag == {"auth0_error": "Login response from Auth0 was incomplete. Please try again."}


# 2. Invalid authorization response ---------------------------------------------
@pytest.mark.parametrize("response,logged", [
    (httpx.Response(200, text="<html>gateway error</html>"), "non-JSON"),
    (httpx.Response(200, json={"access_token": ACCESS}), "no id_token"),
    (httpx.Response(403, json={"error": "invalid_grant", "error_description": "Invalid authorization code"}), "invalid_grant"),
    (httpx.Response(401, json={"error": "access_denied", "error_description": "Unauthorized"}), "HTTP 401"),
])
def test_2_invalid_token_endpoint_response(secret_config, client, monkeypatch, caplog, response, logged):  # noqa: F811
    state, _ = start(client, "facebook")
    fake_token_endpoint(monkeypatch, response)
    with caplog.at_level("WARNING"):
        location, frag = callback(client, code=CODE, state=state)
    assert frag == {"auth0_error": auth0_service.MSG_RETRY}
    assert logged in caplog.text
    assert_nothing_leaked(location, caplog)


def test_2_forged_state(secret_config, client):  # noqa: F811
    start(client, "google")
    _, frag = callback(client, code=CODE, state="forged")
    assert frag["auth0_error"] == "Your sign-in couldn't be matched to this browser. Please start again."


# 3. Invalid / expired token ----------------------------------------------------
@pytest.mark.parametrize("claims,expected", [
    ({"exp": 1}, auth0_service.MSG_EXPIRED),
    ({"aud": "another-app"}, auth0_service.MSG_VERIFY),
    ({"iss": "https://evil.example/"}, auth0_service.MSG_VERIFY),
    ({"nonce": "replayed"}, auth0_service.MSG_VERIFY),
])
def test_3_bad_id_token(secret_config, client, monkeypatch, caplog, claims, expected):  # noqa: F811
    state, nonce = start(client, "google")
    fake_token_endpoint(monkeypatch, good_tokens(nonce, **claims))
    with caplog.at_level("WARNING"):
        location, frag = callback(client, code=CODE, state=state)
    assert frag == {"auth0_error": expected}
    assert "Auth0 callback rejected (provider=google)" in caplog.text
    assert_nothing_leaked(location, caplog)


# 4. Auth0 authentication failure / unreachable ---------------------------------
def test_4_auth0_returns_error(secret_config, client, caplog):  # noqa: F811
    start(client)
    with caplog.at_level("WARNING"):
        _, frag = callback(client, error="server_error", error_description="Rule script crashed at line 12")
    assert frag == {"auth0_error": "Sign-in didn't complete. Please try again or use another way to log in."}
    assert "Rule script crashed" in caplog.text  # detail kept for the server log only


def test_4_token_endpoint_unreachable(secret_config, client, monkeypatch, caplog):  # noqa: F811
    state, _ = start(client, "facebook")
    fake_token_endpoint(monkeypatch, raises=httpx.ConnectError("connection refused"))
    with caplog.at_level("WARNING"):
        location, frag = callback(client, code=CODE, state=state)
    assert frag == {"auth0_error": auth0_service.MSG_UNREACHABLE}
    assert "ConnectError" in caplog.text
    assert_nothing_leaked(location, caplog)


def test_4_signing_keys_unreachable(secret_config, client, monkeypatch):  # noqa: F811
    state, nonce = start(client, "google")
    fake_token_endpoint(monkeypatch, good_tokens(nonce))

    def jwks_down():
        raise auth0_service.Auth0Error("Could not fetch Auth0 signing keys (ConnectTimeout)", auth0_service.MSG_UNREACHABLE)

    monkeypatch.setattr(auth0_service, "fetch_jwks", jwks_down)
    monkeypatch.setattr(auth0_service, "_jwks_cache", None)
    _, frag = callback(client, code=CODE, state=state)
    assert frag == {"auth0_error": auth0_service.MSG_UNREACHABLE}


def test_4_real_fetch_jwks_non_json(configured, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth0_service.httpx, "get", lambda url, timeout=None: httpx.Response(200, text="oops", request=httpx.Request("GET", url)))
    with pytest.raises(auth0_service.Auth0Error) as exc:
        REAL_FETCH_JWKS()
    assert exc.value.user_message == auth0_service.MSG_UNREACHABLE


# 5 & 6. Google / Facebook failures and 7. cancellation --------------------------
@pytest.mark.parametrize("provider,name", [("google", "Google"), ("facebook", "Facebook")])
def test_5_6_provider_failure_is_named(secret_config, client, provider, name):  # noqa: F811
    start(client, provider)
    _, frag = callback(client, error="server_error", error_description="upstream IdP 500")
    assert frag == {"auth0_error": f"{name} sign-in didn't complete. Please try again or use another way to log in."}


@pytest.mark.parametrize("provider,name", [("google", "Google"), ("facebook", "Facebook")])
def test_5_6_provider_not_enabled(secret_config, client, provider, name):  # noqa: F811
    start(client, provider)
    _, frag = callback(client, error="invalid_request", error_description="the connection is not enabled")
    assert frag == {"auth0_error": f"{name} sign-in isn't available right now. Please use another way to log in."}


@pytest.mark.parametrize("provider,expected", [
    ("google", "Google sign-in was cancelled."),
    ("facebook", "Facebook sign-in was cancelled."),
    (None, "Sign-in was cancelled."),
])
def test_7_cancellation(secret_config, client, provider, expected):  # noqa: F811
    start(client, provider)
    _, frag = callback(client, error="access_denied")
    assert frag == {"auth0_error": expected}


def test_7_cancellation_with_auth0_wording(secret_config, client):  # noqa: F811
    start(client, "facebook")
    _, frag = callback(client, error="access_denied", error_description="User did not authorize the request")
    assert frag == {"auth0_error": "User did not authorize the request"}


# 8. Existing email conflict ----------------------------------------------------
def test_8_unverified_email_conflict(secret_config, client, monkeypatch, db_factory):  # noqa: F811
    client.post("/auth/register", json={"username": "erin", "email": "erin@example.com", "password": "strongpass123"})
    state, nonce = start(client, "facebook")
    fake_token_endpoint(monkeypatch, good_tokens(nonce, sub="facebook|9", email="erin@example.com", email_verified=False))
    _, frag = callback(client, code=CODE, state=state)
    assert frag["auth0_error"] == "Verify your email with Auth0 before signing in to an existing account"
    with db_factory() as db:
        assert db.query(models.User).count() == 1


def test_8_ambiguous_email_conflict(secret_config, client, monkeypatch, db_factory):  # noqa: F811
    from app.auth import hash_password
    from app.services import subscription as subscription_service

    with db_factory() as db:
        plan = subscription_service.get_or_create_basic_plan(db)
        for name, email in (("x1", "dup@example.com"), ("x2", "DUP@example.com")):
            db.add(models.User(username=name, email=email, password_hash=hash_password("p4ssword!"), subscription_plan_id=plan.id))
        db.commit()
    state, nonce = start(client, "google")
    fake_token_endpoint(monkeypatch, good_tokens(nonce, sub="google-oauth2|9", email="dup@example.com"))
    _, frag = callback(client, code=CODE, state=state)
    assert frag["auth0_error"].startswith("More than one account uses this email")


# 9. Database failure while creating the user -----------------------------------
def test_9_database_failure(secret_config, client, monkeypatch, caplog, db_factory):  # noqa: F811
    state, nonce = start(client, "facebook")
    fake_token_endpoint(monkeypatch, good_tokens(nonce))

    def db_down(db, claims):
        raise OperationalError("INSERT INTO users", {}, Exception("could not connect to server"))

    monkeypatch.setattr(auth0_service, "get_or_create_user", db_down)
    with caplog.at_level("ERROR"):
        location, frag = callback(client, code=CODE, state=state)
    assert frag == {"auth0_error": auth0_service.MSG_SERVER}
    assert "database error while signing in (provider=facebook)" in caplog.text
    assert "could not connect" not in location
    assert_nothing_leaked(location, caplog)


# 10. Session / JWT creation failure --------------------------------------------
def test_10_app_jwt_creation_failure(secret_config, client, monkeypatch, caplog, db_factory):  # noqa: F811
    state, nonce = start(client, "google")
    fake_token_endpoint(monkeypatch, good_tokens(nonce))

    def broken(data):
        raise RuntimeError("SECRET_KEY misconfigured")

    monkeypatch.setattr(auth0_router, "create_access_token", broken)
    with caplog.at_level("ERROR"):
        location, frag = callback(client, code=CODE, state=state)
    assert frag == {"auth0_error": auth0_service.MSG_SERVER}
    assert "unexpected error while signing in (provider=google)" in caplog.text
    assert "SECRET_KEY" not in location
    assert_nothing_leaked(location, caplog)


def test_10_login_start_failure(secret_config, client, monkeypatch, caplog):  # noqa: F811
    def broken_encode(*args, **kwargs):
        raise RuntimeError("bad algorithm")

    monkeypatch.setattr(auth0_router.jwt, "encode", broken_encode)
    with caplog.at_level("ERROR"):
        resp = client.get("/auth0/login", params={"provider": "facebook"}, follow_redirects=False)
    assert resp.status_code == 302
    assert parse_qs(urlparse(resp.headers["location"]).fragment) == {"auth0_error": [auth0_service.MSG_SERVER]}
    assert "Auth0 login could not start (provider=facebook)" in caplog.text


# 11. Configuration missing or invalid ------------------------------------------
@pytest.mark.parametrize("attr,value,problem", [
    ("AUTH0_DOMAIN", "", "AUTH0_DOMAIN is not set"),
    ("AUTH0_DOMAIN", "not a domain", "AUTH0_DOMAIN is not a bare host name"),
    ("AUTH0_CLIENT_ID", "", "AUTH0_CLIENT_ID is not set"),
    ("AUTH0_CLIENT_SECRET", "", "AUTH0_CLIENT_SECRET is not set"),
    ("AUTH0_CALLBACK_URL", "localhost:8000/auth/callback/", "AUTH0_CALLBACK_URL must be an http(s) URL"),
])
def test_11_bad_config(secret_config, client, monkeypatch, caplog, attr, value, problem):  # noqa: F811
    monkeypatch.setattr(auth0_service, attr, value)
    assert client.get("/auth0/status").json() == {"enabled": False}  # dashboard hides the social buttons
    with caplog.at_level("ERROR"):
        resp = client.get("/auth0/login", params={"provider": "google"}, follow_redirects=False)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Auth0 login is not configured"}
    assert problem in caplog.text
    assert SECRET not in caplog.text


def test_11_config_problems_never_include_values(configured, monkeypatch):  # noqa: F811
    monkeypatch.setattr(auth0_service, "AUTH0_DOMAIN", "https://bad value/")
    monkeypatch.setattr(auth0_service, "AUTH0_CLIENT_SECRET", SECRET)
    joined = " ".join(auth0_service.config_problems())
    assert "bad value" not in joined and SECRET not in joined


# Successful sign-in is unchanged ------------------------------------------------
def test_success_unchanged_and_logged_without_tokens(secret_config, client, monkeypatch, caplog, db_factory):  # noqa: F811
    state, nonce = start(client, "google")
    fake_token_endpoint(monkeypatch, good_tokens(nonce))
    with caplog.at_level("INFO"):
        location, frag = callback(client, code=CODE, state=state)
    assert set(frag) == {"access_token", "token_type", "login_method"}
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {frag['access_token']}"}).status_code == 200
    assert "Auth0 sign-in succeeded (provider=google" in caplog.text
    assert frag["access_token"] not in app_log_text(caplog)
    assert_nothing_leaked(location, caplog)


# Web server access log: the one-time code and state are blanked ------------------
def _access_record(path):
    import logging

    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", path, "1.1", 302), None)


@pytest.mark.parametrize("path", ["/auth/callback/", "/auth0/callback"])
def test_access_log_redacts_code_and_state(path):
    record = _access_record(f"{path}?code={CODE}&state=abc123&error=")
    auth0_router.RedactCallbackQuery().filter(record)
    line = record.getMessage()
    assert CODE not in line and "abc123" not in line
    assert f"{path}?code=[redacted]&state=[redacted]&error=" in line


def test_access_log_keeps_error_details_visible():
    record = _access_record("/auth/callback/?error=access_denied&error_description=User%20cancelled&state=s")
    auth0_router.RedactCallbackQuery().filter(record)
    assert "error=access_denied&error_description=User%20cancelled&state=[redacted]" in record.getMessage()


@pytest.mark.parametrize("path", ["/auth/login", "/posts?page=2&code=keepme", "/static/dashboard.html"])
def test_access_log_other_paths_untouched(path):
    record = _access_record(path)
    auth0_router.RedactCallbackQuery().filter(record)
    assert path in record.getMessage()


def test_filter_is_installed_on_uvicorn_access_logger():
    import logging

    assert any(isinstance(f, auth0_router.RedactCallbackQuery) for f in logging.getLogger("uvicorn.access").filters)
