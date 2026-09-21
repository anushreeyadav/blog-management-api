from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from jose import JWTError
from jose import jwt as jose_jwt
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.services import notifications as notifications_module
from app.services import subscription as subscription_service
from app.auth import (
    ALGORITHM,
    SECRET_KEY,
    authenticate_user,
    create_access_token,
    get_current_user,
    hash_password,
    register_user,
    verify_password,
)
from app.database import Base, get_db
from app.main import app as main_app
from app.schemas import (
    CommentCreate,
    PostCreate,
    PostUpdate,
    UserLogin,
    UserRegister,
    UserResponse,
)

@pytest.fixture(autouse=True)
def _no_basic_plan_action_limits(monkeypatch):
    """
    Every registered user is assigned the Basic plan by default (see
    app/services/subscription.py), which caps post/image/like/comment
    creation. This file tests core CRUD behavior, not plan-based limits
    (that's tests/test_subscriptions.py's and
    tests/test_subscription_service.py's job), and many tests here create
    several posts/likes/comments for the same user -- so disable the cap
    globally for this file rather than opting every such test in
    individually.
    """
    monkeypatch.setattr(subscription_service, "enforce_action_limit", lambda db, user, action, **kwargs: None)


# ---------------------------------------------------------------------------
# Schema-level validation tests
# ---------------------------------------------------------------------------


class TestUserRegisterSchema:
    def test_valid_registration_passes(self):
        user = UserRegister(username="alice", email="alice@example.com", password="strongpass123")
        assert user.username == "alice"
        assert user.email == "alice@example.com"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(username="alice", email="not-an-email", password="strongpass123")

    def test_missing_username_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(email="alice@example.com", password="strongpass123")

    def test_empty_username_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(username="", email="alice@example.com", password="strongpass123")

    def test_blank_username_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(username="   ", email="alice@example.com", password="strongpass123")

    def test_short_password_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(username="alice", email="alice@example.com", password="short")

    def test_missing_email_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(username="alice", password="strongpass123")

    def test_missing_password_rejected(self):
        with pytest.raises(ValidationError):
            UserRegister(username="alice", email="alice@example.com")

    def test_username_is_trimmed(self):
        user = UserRegister(username="  alice  ", email="alice@example.com", password="strongpass123")
        assert user.username == "alice"

    def test_email_is_trimmed(self):
        user = UserRegister(username="alice", email="  alice@example.com  ", password="strongpass123")
        assert user.email == "alice@example.com"


class TestUserLoginSchema:
    def test_valid_login_passes(self):
        login = UserLogin(username="alice", password="strongpass123")
        assert login.username == "alice"

    def test_empty_username_rejected(self):
        with pytest.raises(ValidationError):
            UserLogin(username="", password="strongpass123")

    def test_missing_password_rejected(self):
        with pytest.raises(ValidationError):
            UserLogin(username="alice")


class TestPostSchemas:
    def test_valid_post_passes(self):
        post = PostCreate(title="Hello World", content="Some content")
        assert post.title == "Hello World"
        assert post.content == "Some content"

    def test_empty_title_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(title="", content="Some content")

    def test_blank_title_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(title="   ", content="Some content")

    def test_empty_content_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(title="Hello", content="")

    def test_blank_content_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(title="Hello", content="   ")

    def test_missing_title_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(content="Some content")

    def test_missing_content_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(title="Hello")

    def test_title_is_trimmed(self):
        post = PostCreate(title="  Hello  ", content="World")
        assert post.title == "Hello"

    def test_post_update_allows_partial_data(self):
        update = PostUpdate(title="New title")
        assert update.title == "New title"
        assert update.content is None

    def test_post_update_rejects_blank_title(self):
        with pytest.raises(ValidationError):
            PostUpdate(title="   ")

    def test_post_update_with_no_fields_is_valid(self):
        update = PostUpdate()
        assert update.title is None
        assert update.content is None


class TestCommentSchema:
    def test_valid_comment_passes(self):
        comment = CommentCreate(text="Nice post!")
        assert comment.text == "Nice post!"

    def test_empty_text_rejected(self):
        with pytest.raises(ValidationError):
            CommentCreate(text="")

    def test_blank_text_rejected(self):
        with pytest.raises(ValidationError):
            CommentCreate(text="   ")

    def test_missing_text_rejected(self):
        with pytest.raises(ValidationError):
            CommentCreate()


class TestResponseSchemas:
    def test_user_response_excludes_password_fields(self):
        assert "password" not in UserResponse.model_fields
        assert "password_hash" not in UserResponse.model_fields

    def test_user_response_builds_from_orm_like_object(self):
        class FakeUser:
            id = 1
            username = "alice"
            email = "alice@example.com"
            password_hash = "hashed-secret"
            subscription_plan_id = 1

        response = UserResponse.model_validate(FakeUser())
        assert response.username == "alice"
        assert response.email == "alice@example.com"
        assert "password" not in response.model_dump()
        assert "password_hash" not in response.model_dump()


# ---------------------------------------------------------------------------
# API-level validation tests
#
# app/routers and app/auth are not implemented yet, so there are no real
# endpoints to exercise end-to-end. To still verify the schemas behave
# correctly as FastAPI request bodies (i.e. invalid input -> HTTP 422), these
# tests wire the schemas into a small local FastAPI app defined only in this
# test file. This does not touch app.main or app.routers.
# ---------------------------------------------------------------------------

validation_app = FastAPI()


@validation_app.post("/register")
def _register(payload: UserRegister):
    return {"username": payload.username, "email": payload.email}


@validation_app.post("/posts")
def _create_post(payload: PostCreate):
    return {"title": payload.title, "content": payload.content}


@validation_app.post("/comments")
def _create_comment(payload: CommentCreate):
    return {"text": payload.text}


client = TestClient(validation_app)


class TestAPILevelValidation:
    def test_valid_registration_returns_200(self):
        resp = client.post(
            "/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        assert resp.status_code == 200

    def test_invalid_email_returns_422(self):
        resp = client.post(
            "/register",
            json={"username": "alice", "email": "not-an-email", "password": "strongpass123"},
        )
        assert resp.status_code == 422

    def test_missing_username_returns_422(self):
        resp = client.post(
            "/register",
            json={"email": "alice@example.com", "password": "strongpass123"},
        )
        assert resp.status_code == 422

    def test_empty_username_returns_422(self):
        resp = client.post(
            "/register",
            json={"username": "", "email": "alice@example.com", "password": "strongpass123"},
        )
        assert resp.status_code == 422

    def test_short_password_returns_422(self):
        resp = client.post(
            "/register",
            json={"username": "alice", "email": "alice@example.com", "password": "short"},
        )
        assert resp.status_code == 422

    def test_valid_post_returns_200(self):
        resp = client.post("/posts", json={"title": "Hello", "content": "World"})
        assert resp.status_code == 200

    def test_empty_post_title_returns_422(self):
        resp = client.post("/posts", json={"title": "", "content": "World"})
        assert resp.status_code == 422

    def test_empty_post_content_returns_422(self):
        resp = client.post("/posts", json={"title": "Hello", "content": ""})
        assert resp.status_code == 422

    def test_missing_required_post_field_returns_422(self):
        resp = client.post("/posts", json={"title": "Hello"})
        assert resp.status_code == 422

    def test_valid_comment_returns_200(self):
        resp = client.post("/comments", json={"text": "Nice post"})
        assert resp.status_code == 200

    def test_empty_comment_text_returns_422(self):
        resp = client.post("/comments", json={"text": ""})
        assert resp.status_code == 422

    def test_missing_required_field_returns_422(self):
        resp = client.post("/comments", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Password hashing / verification and registration / login integration tests
#
# Registration and login are exercised through app.auth's helper functions
# (register_user, authenticate_user) directly, against an isolated in-memory
# SQLite database, since app/routers/auth.py does not implement HTTP
# endpoints yet.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class TestPasswordHashing:
    def test_hash_password_does_not_return_original_password(self):
        hashed = hash_password("mysecretpassword")
        assert hashed != "mysecretpassword"

    def test_hash_password_returns_non_empty_hash(self):
        hashed = hash_password("mysecretpassword")
        assert isinstance(hashed, str)
        assert len(hashed) > 0

    def test_verify_password_correct_returns_true(self):
        hashed = hash_password("mysecretpassword")
        assert verify_password("mysecretpassword", hashed) is True

    def test_verify_password_incorrect_returns_false(self):
        hashed = hash_password("mysecretpassword")
        assert verify_password("wrongpassword", hashed) is False

    def test_same_password_produces_different_hashes_due_to_salting(self):
        hash1 = hash_password("mysecretpassword")
        hash2 = hash_password("mysecretpassword")
        assert hash1 != hash2
        assert verify_password("mysecretpassword", hash1) is True
        assert verify_password("mysecretpassword", hash2) is True


class TestRegistrationPersistsHashedPassword:
    def test_registration_stores_hashed_password_in_db(self, db_session):
        user_data = UserRegister(username="alice", email="alice@example.com", password="strongpass123")
        user = register_user(db_session, user_data)

        stored = db_session.query(models.User).filter(models.User.username == "alice").first()
        assert stored is not None
        assert stored.password_hash == user.password_hash
        assert verify_password("strongpass123", stored.password_hash) is True

    def test_registration_does_not_store_plaintext_password(self, db_session):
        user_data = UserRegister(username="bob", email="bob@example.com", password="anotherpassword1")
        register_user(db_session, user_data)

        stored = db_session.query(models.User).filter(models.User.username == "bob").first()
        assert stored.password_hash != "anotherpassword1"
        assert not hasattr(stored, "password")


class TestLoginAuthentication:
    def test_login_succeeds_with_correct_password(self, db_session):
        user_data = UserRegister(username="carol", email="carol@example.com", password="correcthorse1")
        register_user(db_session, user_data)

        authenticated = authenticate_user(db_session, "carol", "correcthorse1")
        assert authenticated is not None
        assert authenticated.username == "carol"

    def test_login_fails_with_incorrect_password(self, db_session):
        user_data = UserRegister(username="dave", email="dave@example.com", password="correcthorse1")
        register_user(db_session, user_data)

        authenticated = authenticate_user(db_session, "dave", "wrongpassword")
        assert authenticated is None

    def test_login_fails_for_nonexistent_user(self, db_session):
        authenticated = authenticate_user(db_session, "ghost", "whatever")
        assert authenticated is None


# ---------------------------------------------------------------------------
# End-to-end tests for the real /auth/register, /auth/login and /auth/me
# endpoints defined in app/routers/auth.py, wired into app.main.app. Each
# test gets its own isolated in-memory SQLite database via a get_db override,
# so these tests never touch the project's real blog.db.
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client():
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


class TestAuthEndpoints:
    def test_register_returns_201_and_user_without_password(self, auth_client):
        resp = auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "alice"
        assert body["email"] == "alice@example.com"
        assert "password" not in body
        assert "password_hash" not in body

    def test_register_duplicate_username_returns_400(self, auth_client):
        auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        resp = auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "someoneelse@example.com", "password": "strongpass123"},
        )
        assert resp.status_code == 400

    def test_register_duplicate_email_returns_400(self, auth_client):
        auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        resp = auth_client.post(
            "/auth/register",
            json={"username": "someoneelse", "email": "alice@example.com", "password": "strongpass123"},
        )
        assert resp.status_code == 400

    def test_login_with_correct_password_returns_token(self, auth_client):
        auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        resp = auth_client.post("/auth/login", json={"username": "alice", "password": "strongpass123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert isinstance(body["access_token"], str) and len(body["access_token"]) > 0

    def test_login_with_incorrect_password_returns_401(self, auth_client):
        auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        resp = auth_client.post("/auth/login", json={"username": "alice", "password": "wrongpassword"})
        assert resp.status_code == 401

    def test_login_with_nonexistent_user_returns_401(self, auth_client):
        resp = auth_client.post("/auth/login", json={"username": "ghost", "password": "whatever123"})
        assert resp.status_code == 401

    def test_me_with_valid_token_returns_user(self, auth_client):
        auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        login_resp = auth_client.post("/auth/login", json={"username": "alice", "password": "strongpass123"})
        token = login_resp.json()["access_token"]

        resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["username"] == "alice"

    def test_me_without_token_returns_401(self, auth_client):
        resp = auth_client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_with_invalid_token_returns_401(self, auth_client):
        resp = auth_client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# JWT token creation tests (app.auth.create_access_token)
# ---------------------------------------------------------------------------


class TestJWTTokenCreation:
    def test_create_access_token_stores_sub_and_expiry(self):
        token = create_access_token(data={"sub": "42"})
        payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == "42"
        assert "exp" in payload

    def test_create_access_token_is_signed_with_the_configured_secret(self):
        token = create_access_token(data={"sub": "42"})
        with pytest.raises(JWTError):
            jose_jwt.decode(token, "a-totally-different-secret", algorithms=[ALGORITHM])


# ---------------------------------------------------------------------------
# get_current_user tests (app.auth.get_current_user), exercised directly as
# a function against an isolated in-memory database.
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    def test_valid_token_returns_the_user(self, db_session):
        user_data = UserRegister(username="erin", email="erin@example.com", password="strongpass123")
        user = register_user(db_session, user_data)
        token = create_access_token(data={"sub": str(user.id)})
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        result = get_current_user(credentials=credentials, db=db_session)
        assert result.id == user.id
        assert result.username == "erin"

    def test_missing_token_raises_401(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(credentials=None, db=db_session)
        assert exc_info.value.status_code == 401

    def test_invalid_signature_token_raises_401(self, db_session):
        bad_token = jose_jwt.encode({"sub": "1"}, "a-totally-different-secret", algorithm=ALGORITHM)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=bad_token)
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(credentials=credentials, db=db_session)
        assert exc_info.value.status_code == 401

    def test_malformed_token_raises_401(self, db_session):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt-at-all")
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(credentials=credentials, db=db_session)
        assert exc_info.value.status_code == 401

    def test_expired_token_raises_401(self, db_session):
        user_data = UserRegister(username="frank", email="frank@example.com", password="strongpass123")
        user = register_user(db_session, user_data)
        expired_token = create_access_token(data={"sub": str(user.id)}, expires_delta=timedelta(minutes=-5))
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(credentials=credentials, db=db_session)
        assert exc_info.value.status_code == 401

    def test_nonexistent_user_id_raises_401(self, db_session):
        token = create_access_token(data={"sub": "999999"})
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(credentials=credentials, db=db_session)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Posts CRUD tests against the real /posts endpoints, wired into
# app.main.app. Each test gets its own isolated in-memory SQLite database
# via the auth_client fixture (defined above), so these never touch the
# project's real blog.db.
# ---------------------------------------------------------------------------


def _register_and_login(client: TestClient, username: str, email: str, password: str = "strongpass123") -> dict:
    client.post("/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestPostsCRUD:
    def test_create_post_requires_authentication(self, auth_client):
        resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"})
        assert resp.status_code == 401

    def test_authenticated_user_can_create_post(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Hello"
        assert body["content"] == "World"
        assert "author_id" in body
        assert "password" not in body

    def test_author_id_is_not_taken_from_request_body(self, auth_client):
        register_resp = auth_client.post(
            "/auth/register",
            json={"username": "alice", "email": "alice@example.com", "password": "strongpass123"},
        )
        real_user_id = register_resp.json()["id"]
        login_resp = auth_client.post("/auth/login", json={"username": "alice", "password": "strongpass123"})
        headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

        resp = auth_client.post(
            "/posts",
            json={"title": "Hello", "content": "World", "author_id": 9999},
            headers=headers,
        )
        assert resp.status_code == 201
        assert resp.json()["author_id"] == real_user_id
        assert resp.json()["author_id"] != 9999

    def test_get_all_posts_is_public(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)

        resp = auth_client.get("/posts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_get_single_post_is_public(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)
        post_id = create_resp.json()["id"]

        resp = auth_client.get(f"/posts/{post_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Hello"

    def test_get_nonexistent_post_returns_404(self, auth_client):
        resp = auth_client.get("/posts/999999")
        assert resp.status_code == 404

    def test_owner_can_update_post(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)
        post_id = create_resp.json()["id"]

        resp = auth_client.put(f"/posts/{post_id}", json={"title": "Updated"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated"
        assert resp.json()["content"] == "World"

    def test_update_requires_authentication(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)
        post_id = create_resp.json()["id"]

        resp = auth_client.put(f"/posts/{post_id}", json={"title": "Updated"})
        assert resp.status_code == 401

    def test_non_owner_cannot_update_post(self, auth_client):
        headers_a = _register_and_login(auth_client, "userA", "usera@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers_a)
        post_id = create_resp.json()["id"]

        headers_b = _register_and_login(auth_client, "userB", "userb@example.com")
        resp = auth_client.put(f"/posts/{post_id}", json={"title": "Hacked"}, headers=headers_b)
        assert resp.status_code == 403

        # confirm the post content was not actually changed
        unchanged = auth_client.get(f"/posts/{post_id}")
        assert unchanged.json()["title"] == "Hello"

    def test_update_nonexistent_post_returns_404(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        resp = auth_client.put("/posts/999999", json={"title": "Updated"}, headers=headers)
        assert resp.status_code == 404

    def test_owner_can_delete_post(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)
        post_id = create_resp.json()["id"]

        resp = auth_client.delete(f"/posts/{post_id}", headers=headers)
        assert resp.status_code == 204

        get_resp = auth_client.get(f"/posts/{post_id}")
        assert get_resp.status_code == 404

    def test_delete_requires_authentication(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers)
        post_id = create_resp.json()["id"]

        resp = auth_client.delete(f"/posts/{post_id}")
        assert resp.status_code == 401

    def test_non_owner_cannot_delete_post(self, auth_client):
        headers_a = _register_and_login(auth_client, "userA", "usera@example.com")
        create_resp = auth_client.post("/posts", json={"title": "Hello", "content": "World"}, headers=headers_a)
        post_id = create_resp.json()["id"]

        headers_b = _register_and_login(auth_client, "userB", "userb@example.com")
        resp = auth_client.delete(f"/posts/{post_id}", headers=headers_b)
        assert resp.status_code == 403

        still_there = auth_client.get(f"/posts/{post_id}")
        assert still_there.status_code == 200

    def test_delete_nonexistent_post_returns_404(self, auth_client):
        headers = _register_and_login(auth_client, "alice", "alice@example.com")
        resp = auth_client.delete("/posts/999999", headers=headers)
        assert resp.status_code == 404


class TestPostOwnershipSecurityScenario:
    """Mirrors the exact User A / User B ownership scenario from the spec."""

    def test_user_b_cannot_modify_or_delete_user_a_post_but_user_a_can(self, auth_client):
        headers_a = _register_and_login(auth_client, "userA", "usera@example.com")
        create_resp = auth_client.post(
            "/posts", json={"title": "Post 1", "content": "Body"}, headers=headers_a
        )
        post_id = create_resp.json()["id"]

        headers_b = _register_and_login(auth_client, "userB", "userb@example.com")

        assert auth_client.put(f"/posts/{post_id}", json={"title": "Hacked"}, headers=headers_b).status_code == 403
        assert auth_client.delete(f"/posts/{post_id}", headers=headers_b).status_code == 403

        assert (
            auth_client.put(f"/posts/{post_id}", json={"title": "Updated by A"}, headers=headers_a).status_code
            == 200
        )
        assert auth_client.delete(f"/posts/{post_id}", headers=headers_a).status_code == 204


# ---------------------------------------------------------------------------
# STEP 9 — dedicated User A vs User B ownership security suite.
#
# Uses its own fixture (security_client) that, unlike auth_client, also
# exposes a session factory bound to the same in-memory database, so tests
# can query the database directly to prove a rejected PUT/DELETE left the
# row completely unchanged -- not just that the HTTP response was a 403.
# ---------------------------------------------------------------------------


@pytest.fixture()
def security_client():
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


def _get_post_from_db(session_factory, post_id: int) -> models.Post:
    db = session_factory()
    try:
        post = db.query(models.Post).filter(models.Post.id == post_id).first()
        if post is not None:
            db.expunge(post)
        return post
    finally:
        db.close()


class TestUserAVsUserBSecurity:
    """
    Implements STEP 9 end-to-end: two real users, one post, and every
    ownership / auth bypass attempt the spec calls out, each checked both
    at the HTTP layer and directly against the database.
    """

    USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
    USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

    def _register(self, client, user):
        return client.post("/auth/register", json=user)

    def _login(self, client, user):
        resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
        return resp

    def _auth_headers(self, client, user):
        resp = self._login(client, user)
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    # 1 & 2: User A / User B registration
    def test_01_user_a_registration_succeeds(self, security_client):
        client, _ = security_client
        resp = self._register(client, self.USER_A)
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "user_a"
        assert body["email"] == "usera@example.com"
        assert "password" not in body
        assert "password_hash" not in body

    def test_02_user_b_registration_succeeds(self, security_client):
        client, _ = security_client
        resp = self._register(client, self.USER_B)
        assert resp.status_code == 201
        assert resp.json()["username"] == "user_b"

    # 3 & 4: User A / User B login
    def test_03_user_a_login_succeeds(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        resp = self._login(client, self.USER_A)
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert len(body["access_token"]) > 0

    def test_04_user_b_login_succeeds(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_B)
        resp = self._login(client, self.USER_B)
        assert resp.status_code == 200
        assert len(resp.json()["access_token"]) > 0

    # 5: User A creates a post, author_id must be User A's real id
    def test_05_user_a_creates_post_with_correct_author_id(self, security_client):
        client, session_factory = security_client
        register_resp = self._register(client, self.USER_A)
        user_a_id = register_resp.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)

        resp = client.post(
            "/posts",
            json={"title": "User A Post", "content": "This post belongs to User A."},
            headers=headers_a,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["author_id"] == user_a_id

        # confirmed directly against the database, not just the HTTP response
        db_post = _get_post_from_db(session_factory, body["id"])
        assert db_post is not None
        assert db_post.author_id == user_a_id

    # 6, 8: User A can update own post; verify the update actually happened
    def test_06_and_08_user_a_can_update_own_post_and_change_persists(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts",
            json={"title": "User A Post", "content": "This post belongs to User A."},
            headers=headers_a,
        ).json()["id"]

        resp = client.put(
            f"/posts/{post_id}",
            json={"title": "Updated User A Post", "content": "Updated content."},
            headers=headers_a,
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated User A Post"
        assert resp.json()["content"] == "Updated content."

        db_post = _get_post_from_db(session_factory, post_id)
        assert db_post.title == "Updated User A Post"
        assert db_post.content == "Updated content."

    # 7, 8: User B cannot update User A's post; row must remain unchanged
    def test_07_and_08_user_b_cannot_update_user_a_post(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts",
            json={"title": "User A Post", "content": "This post belongs to User A."},
            headers=headers_a,
        ).json()["id"]

        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)

        resp = client.put(
            f"/posts/{post_id}",
            json={"title": "Hacked Title", "content": "User B should not be able to modify this post."},
            headers=headers_b,
        )
        assert resp.status_code == 403

        db_post = _get_post_from_db(session_factory, post_id)
        assert db_post.title == "User A Post"
        assert db_post.content == "This post belongs to User A."

    # 9, 10: User B cannot delete User A's post; post must still exist
    def test_09_and_10_user_b_cannot_delete_user_a_post(self, security_client):
        client, session_factory = security_client
        register_resp = self._register(client, self.USER_A)
        user_a_id = register_resp.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts",
            json={"title": "Second User A Post", "content": "Another post by User A."},
            headers=headers_a,
        ).json()["id"]

        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)

        resp = client.delete(f"/posts/{post_id}", headers=headers_b)
        assert resp.status_code == 403

        db_post = _get_post_from_db(session_factory, post_id)
        assert db_post is not None
        assert db_post.author_id == user_a_id
        assert db_post.title == "Second User A Post"

    # 11: User A deletes own post -> success, and it is gone from the DB
    def test_11_user_a_deletes_own_post_successfully(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts",
            json={"title": "User A Post", "content": "This post belongs to User A."},
            headers=headers_a,
        ).json()["id"]

        resp = client.delete(f"/posts/{post_id}", headers=headers_a)
        assert resp.status_code == 204

        assert _get_post_from_db(session_factory, post_id) is None
        assert client.get(f"/posts/{post_id}").status_code == 404

    # 12, 13, 14: unauthenticated create/update/delete -> 401
    def test_12_unauthenticated_create_post_returns_401(self, security_client):
        client, _ = security_client
        resp = client.post("/posts", json={"title": "X", "content": "Y"})
        assert resp.status_code == 401

    def test_13_unauthenticated_update_post_returns_401(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts", json={"title": "User A Post", "content": "Body"}, headers=headers_a
        ).json()["id"]

        resp = client.put(f"/posts/{post_id}", json={"title": "No auth"})
        assert resp.status_code == 401

    def test_14_unauthenticated_delete_post_returns_401(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts", json={"title": "User A Post", "content": "Body"}, headers=headers_a
        ).json()["id"]

        resp = client.delete(f"/posts/{post_id}")
        assert resp.status_code == 401

    # 15, 16: public GET endpoints keep working without authentication
    def test_15_public_get_all_posts_without_authentication(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        client.post("/posts", json={"title": "User A Post", "content": "Body"}, headers=headers_a)

        resp = client.get("/posts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_16_public_get_single_post_without_authentication(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts", json={"title": "User A Post", "content": "Body"}, headers=headers_a
        ).json()["id"]

        resp = client.get(f"/posts/{post_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "User A Post"

    # 17: author_id always belongs to the authenticated user, never the client
    def test_17_author_id_always_belongs_to_authenticated_user_even_when_spoofed(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)

        # User A tries to impersonate User B by spoofing author_id in the body
        resp = client.post(
            "/posts",
            json={"title": "Unauthorized Post", "content": "Test", "author_id": user_b_id},
            headers=headers_a,
        )
        assert resp.status_code == 201
        assert resp.json()["author_id"] == user_a_id
        assert resp.json()["author_id"] != user_b_id

        db_post = _get_post_from_db(session_factory, resp.json()["id"])
        assert db_post.author_id == user_a_id

    # Section 9: ID manipulation -- User B obtains User A's post id directly
    # and tries to act on it. Ownership must still be enforced by
    # post.author_id == current_user.id, never by trusting the id alone.
    def test_id_manipulation_user_b_cannot_act_on_known_user_a_post_id(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_id = client.post(
            "/posts", json={"title": "Post 1", "content": "Owned by A"}, headers=headers_a
        ).json()["id"]

        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)

        # User B knows the exact post id and tries both mutating operations
        assert client.put(f"/posts/{post_id}", json={"title": "Taken over"}, headers=headers_b).status_code == 403
        assert client.delete(f"/posts/{post_id}", headers=headers_b).status_code == 403

        db_post = _get_post_from_db(session_factory, post_id)
        assert db_post is not None
        assert db_post.title == "Post 1"

    # Section 11: nothing but the validated JWT determines the current user
    # -- a user_id in the body or a query string must have no effect.
    def test_current_user_ignores_user_id_supplied_in_body_or_query(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_b = self._auth_headers(client, self.USER_B)

        # User B is authenticated as themself, but tries to smuggle User A's
        # id in the body and as a query parameter to see if either is honored.
        resp = client.post(
            f"/posts?user_id={user_a_id}",
            json={"title": "Spoofed owner", "content": "Test", "user_id": user_a_id},
            headers=headers_b,
        )
        assert resp.status_code == 201
        assert resp.json()["author_id"] == user_b_id
        assert resp.json()["author_id"] != user_a_id

        db_post = _get_post_from_db(session_factory, resp.json()["id"])
        assert db_post.author_id == user_b_id


# ---------------------------------------------------------------------------
# Pagination & search on GET /posts.
#
# Reuses the security_client fixture (TestClient + session factory bound to
# the same isolated in-memory database).
# ---------------------------------------------------------------------------


class TestPostsPaginationAndSearch:
    USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}

    def _register(self, client, user):
        return client.post("/auth/register", json=user)

    def _auth_headers(self, client, user):
        resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _create_post(self, client, headers, title="Hello", content="World"):
        return client.post("/posts", json={"title": title, "content": content}, headers=headers).json()

    def _create_many(self, client, headers, count, title_prefix="Post", content="Body"):
        for i in range(count):
            self._create_post(client, headers, title=f"{title_prefix} {i}", content=content)

    def _grant_unlimited_plan(self, session_factory, username):
        """
        The Basic plan (the default every user gets at registration -- see
        app/services/subscription.py) caps post creation. These tests
        exercise pagination/search over many posts, which is orthogonal to
        that cap, so switch the test user's active plan to one with no
        post limit.
        """
        db = session_factory()
        try:
            user = db.query(models.User).filter(models.User.username == username).first()
            plan = models.SubscriptionPlan(
                name=f"Unlimited ({username})",
                slug=f"unlimited-{username}",
                price=0,
                billing_interval="month",
                max_posts=None,
                max_images=None,
                max_likes=None,
                max_comments=None,
                is_active=True,
            )
            db.add(plan)
            db.commit()
            db.refresh(plan)
            user.subscription_plan_id = plan.id
            db.commit()
        finally:
            db.close()

    # Default pagination (no query params): page=1, limit=10
    def test_default_pagination(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        self._grant_unlimited_plan(session_factory, self.USER_A["username"])
        headers = self._auth_headers(client, self.USER_A)
        self._create_many(client, headers, 3)

        resp = client.get("/posts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["limit"] == 10
        assert body["total"] == 3
        assert body["total_pages"] == 1
        assert len(body["items"]) == 3

    # Custom page and limit
    def test_custom_page_and_limit(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        self._grant_unlimited_plan(session_factory, self.USER_A["username"])
        headers = self._auth_headers(client, self.USER_A)
        self._create_many(client, headers, 15)

        resp = client.get("/posts?page=1&limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["limit"] == 5
        assert body["total"] == 15
        assert body["total_pages"] == 3
        assert len(body["items"]) == 5

    # Page navigation: page 2 returns the next slice, not the same posts
    def test_page_navigation_returns_different_posts(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        self._grant_unlimited_plan(session_factory, self.USER_A["username"])
        headers = self._auth_headers(client, self.USER_A)
        self._create_many(client, headers, 15)

        page1 = client.get("/posts?page=1&limit=5").json()
        page2 = client.get("/posts?page=2&limit=5").json()

        page1_ids = {item["id"] for item in page1["items"]}
        page2_ids = {item["id"] for item in page2["items"]}
        assert len(page2["items"]) == 5
        assert page1_ids.isdisjoint(page2_ids)

    def test_last_page_may_be_partial(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        self._grant_unlimited_plan(session_factory, self.USER_A["username"])
        headers = self._auth_headers(client, self.USER_A)
        self._create_many(client, headers, 12)

        resp = client.get("/posts?page=3&limit=5")
        body = resp.json()
        assert body["total_pages"] == 3
        assert len(body["items"]) == 2

    def test_page_beyond_last_page_returns_empty_items(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        self._grant_unlimited_plan(session_factory, self.USER_A["username"])
        headers = self._auth_headers(client, self.USER_A)
        self._create_many(client, headers, 3)

        resp = client.get("/posts?page=5&limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 3

    # Search by title
    def test_search_matches_title(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers = self._auth_headers(client, self.USER_A)
        self._create_post(client, headers, title="FastAPI Tutorial", content="unrelated")
        self._create_post(client, headers, title="Something else", content="unrelated")

        resp = client.get("/posts?search=fastapi")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "FastAPI Tutorial"

    # Search by content
    def test_search_matches_content(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers = self._auth_headers(client, self.USER_A)
        self._create_post(client, headers, title="Post One", content="Learning FastAPI is fun")
        self._create_post(client, headers, title="Post Two", content="Nothing relevant here")

        resp = client.get("/posts?search=fastapi")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Post One"

    # Search is case-insensitive
    def test_search_is_case_insensitive(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers = self._auth_headers(client, self.USER_A)
        self._create_post(client, headers, title="FastAPI Tutorial", content="body")

        resp = client.get("/posts?search=FASTAPI")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    # Search with no results
    def test_search_with_no_results(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers = self._auth_headers(client, self.USER_A)
        self._create_post(client, headers, title="FastAPI Tutorial", content="body")

        resp = client.get("/posts?search=xyzabc123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["total_pages"] == 0

    # Search + pagination together
    def test_search_combined_with_pagination(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        self._grant_unlimited_plan(session_factory, self.USER_A["username"])
        headers = self._auth_headers(client, self.USER_A)
        self._create_many(client, headers, 12, title_prefix="FastAPI Post", content="body")
        self._create_many(client, headers, 3, title_prefix="Unrelated", content="nothing")

        page1 = client.get("/posts?search=fastapi&page=1&limit=5").json()
        assert page1["total"] == 12
        assert page1["total_pages"] == 3
        assert len(page1["items"]) == 5

        page2 = client.get("/posts?search=fastapi&page=2&limit=5").json()
        assert len(page2["items"]) == 5
        page1_ids = {item["id"] for item in page1["items"]}
        page2_ids = {item["id"] for item in page2["items"]}
        assert page1_ids.isdisjoint(page2_ids)

        page3 = client.get("/posts?search=fastapi&page=3&limit=5").json()
        assert len(page3["items"]) == 2

    # Invalid page / limit -> FastAPI validation error, not a 500 or silent clamp
    def test_invalid_page_zero_returns_422(self, security_client):
        client, _ = security_client
        resp = client.get("/posts?page=0&limit=10")
        assert resp.status_code == 422

    def test_invalid_page_negative_returns_422(self, security_client):
        client, _ = security_client
        resp = client.get("/posts?page=-1&limit=10")
        assert resp.status_code == 422

    def test_invalid_limit_zero_returns_422(self, security_client):
        client, _ = security_client
        resp = client.get("/posts?page=1&limit=0")
        assert resp.status_code == 422

    def test_limit_above_maximum_returns_422(self, security_client):
        client, _ = security_client
        resp = client.get("/posts?page=1&limit=101")
        assert resp.status_code == 422

    def test_limit_at_maximum_is_accepted(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers = self._auth_headers(client, self.USER_A)
        self._create_post(client, headers)

        resp = client.get("/posts?page=1&limit=100")
        assert resp.status_code == 200

    # Existing post fields (and the image field from the image-upload
    # feature) must still be present on each paginated item.
    def test_paginated_items_preserve_existing_post_fields(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers = self._auth_headers(client, self.USER_A)
        self._create_post(client, headers, title="Hello", content="World")

        resp = client.get("/posts")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert set(["id", "title", "content", "author_id", "created_at", "image"]).issubset(item.keys())
        assert item["title"] == "Hello"
        assert item["content"] == "World"
        assert item["image"] is None


# ---------------------------------------------------------------------------
# STEP 10 — Comments on posts.
#
# Reuses the security_client fixture (TestClient + session factory bound to
# the same isolated in-memory database) so tests can verify comments are
# actually persisted with the correct post_id/user_id, not just that the
# HTTP response looks right.
# ---------------------------------------------------------------------------


def _get_comment_from_db(session_factory, comment_id: int) -> models.Comment:
    db = session_factory()
    try:
        comment = db.query(models.Comment).filter(models.Comment.id == comment_id).first()
        if comment is not None:
            db.expunge(comment)
        return comment
    finally:
        db.close()


class TestCommentsAPI:
    USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
    USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

    def _register(self, client, user):
        return client.post("/auth/register", json=user)

    def _auth_headers(self, client, user):
        resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _create_post(self, client, headers, title="Hello", content="World"):
        return client.post("/posts", json={"title": title, "content": content}, headers=headers).json()

    # 1: authenticated user can create a comment
    def test_authenticated_user_can_create_comment(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "Great post!"}, headers=headers_a)
        assert resp.status_code == 201
        body = resp.json()
        assert body["text"] == "Great post!"
        assert "password" not in body
        assert "password_hash" not in body

    # 2, 3, 4: comment stored in SQLite with correct post_id and user_id
    def test_comment_stored_with_correct_post_id_and_user_id(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_b = self._auth_headers(client, self.USER_B)

        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "Comment from B"}, headers=headers_b)
        assert resp.status_code == 201
        body = resp.json()
        assert body["post_id"] == post["id"]
        assert body["user_id"] == user_b_id

        db_comment = _get_comment_from_db(session_factory, body["id"])
        assert db_comment is not None
        assert db_comment.post_id == post["id"]
        assert db_comment.user_id == user_b_id
        assert db_comment.text == "Comment from B"
        assert db_comment.created_at is not None

    # 5, 6: public user can view comments, returned for the correct post
    def test_public_user_can_view_comments_for_correct_post(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post_1 = self._create_post(client, headers_a, title="Post 1")
        post_2 = self._create_post(client, headers_a, title="Post 2")

        client.post(f"/posts/{post_1['id']}/comments", json={"text": "On post 1"}, headers=headers_a)
        client.post(f"/posts/{post_2['id']}/comments", json={"text": "On post 2"}, headers=headers_a)

        resp = client.get(f"/posts/{post_1['id']}/comments")
        assert resp.status_code == 200
        comments = resp.json()
        assert len(comments) == 1
        assert comments[0]["text"] == "On post 1"
        assert comments[0]["post_id"] == post_1["id"]

    # 7: empty comments return []
    def test_empty_comments_returns_empty_list(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.get(f"/posts/{post['id']}/comments")
        assert resp.status_code == 200
        assert resp.json() == []

    # 8: creating a comment for a nonexistent post returns 404
    def test_create_comment_for_nonexistent_post_returns_404(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)

        resp = client.post("/posts/999999/comments", json={"text": "orphan"}, headers=headers_a)
        assert resp.status_code == 404

    def test_get_comments_for_nonexistent_post_returns_404(self, security_client):
        client, _ = security_client
        resp = client.get("/posts/999999/comments")
        assert resp.status_code == 404

    # 9: creating a comment without JWT returns 401
    def test_create_comment_without_jwt_returns_401(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "no auth"})
        assert resp.status_code == 401

    # 10, 11: empty / missing comment text returns 422
    def test_empty_comment_text_returns_422(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": ""}, headers=headers_a)
        assert resp.status_code == 422

    def test_blank_comment_text_returns_422(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/comments", json={"text": "   "}, headers=headers_a)
        assert resp.status_code == 422

    def test_missing_comment_text_returns_422(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/comments", json={}, headers=headers_a)
        assert resp.status_code == 422

    # 12: client cannot override user_id
    def test_client_cannot_override_user_id(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        post = self._create_post(client, headers_a)

        resp = client.post(
            f"/posts/{post['id']}/comments",
            json={"text": "Comment", "user_id": 999},
            headers=headers_a,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["user_id"] != 999
        assert body["user_id"] != user_b_id

        db_comment = _get_comment_from_db(session_factory, body["id"])
        assert db_comment.user_id == body["user_id"]

    # 13: multiple users can comment on the same post
    def test_multiple_users_can_comment_on_same_post(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)

        post = self._create_post(client, headers_a)
        client.post(f"/posts/{post['id']}/comments", json={"text": "From A"}, headers=headers_a)
        client.post(f"/posts/{post['id']}/comments", json={"text": "From B"}, headers=headers_b)

        resp = client.get(f"/posts/{post['id']}/comments")
        assert resp.status_code == 200
        texts = {c["text"] for c in resp.json()}
        assert texts == {"From A", "From B"}

    # Ownership relationship: a user does not need to own a post to comment
    def test_non_owner_can_comment_on_someone_elses_post(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_b = self._auth_headers(client, self.USER_B)

        post = self._create_post(client, headers_a, title="Post 1")

        resp = client.post(
            f"/posts/{post['id']}/comments",
            json={"text": "Comment from User B"},
            headers=headers_b,
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == user_b_id
        assert resp.json()["post_id"] == post["id"]

        db_comment = _get_comment_from_db(session_factory, resp.json()["id"])
        assert db_comment.user_id == user_b_id
        assert db_comment.post_id == post["id"]


# ---------------------------------------------------------------------------
# STEP 11 — Likes on posts.
#
# Reuses the security_client fixture (TestClient + session factory bound to
# the same isolated in-memory database) so tests can verify like rows are
# actually created/removed correctly, not just that the HTTP response looks
# right.
# ---------------------------------------------------------------------------


def _get_like_from_db(session_factory, post_id: int, user_id: int) -> models.Like:
    db = session_factory()
    try:
        like = (
            db.query(models.Like)
            .filter(models.Like.post_id == post_id, models.Like.user_id == user_id)
            .first()
        )
        if like is not None:
            db.expunge(like)
        return like
    finally:
        db.close()


def _count_likes_in_db(session_factory, post_id: int) -> int:
    db = session_factory()
    try:
        return db.query(models.Like).filter(models.Like.post_id == post_id).count()
    finally:
        db.close()


class TestLikesAPI:
    USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
    USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

    def _register(self, client, user):
        return client.post("/auth/register", json=user)

    def _auth_headers(self, client, user):
        resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _create_post(self, client, headers, title="Hello", content="World"):
        return client.post("/posts", json={"title": title, "content": content}, headers=headers).json()

    # 1, 2, 3, 4: authenticated user can like a post; stored with correct
    # post_id and user_id
    def test_authenticated_user_can_like_post_and_it_is_stored_correctly(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/like", headers=headers_a)
        assert resp.status_code == 201
        body = resp.json()
        assert body["post_id"] == post["id"]
        assert body["user_id"] == user_a_id

        db_like = _get_like_from_db(session_factory, post["id"], user_a_id)
        assert db_like is not None
        assert db_like.post_id == post["id"]
        assert db_like.user_id == user_a_id

    # 5, 6, 7: duplicate like is rejected and no second record is created
    def test_duplicate_like_returns_409_and_creates_no_second_record(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        first = client.post(f"/posts/{post['id']}/like", headers=headers_a)
        assert first.status_code == 201

        second = client.post(f"/posts/{post['id']}/like", headers=headers_a)
        assert second.status_code == 409

        assert _count_likes_in_db(session_factory, post["id"]) == 1

    # 8, 9: user can unlike a post, removing the Like record
    def test_user_can_unlike_post_and_record_is_removed(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        client.post(f"/posts/{post['id']}/like", headers=headers_a)
        resp = client.delete(f"/posts/{post['id']}/like", headers=headers_a)
        assert resp.status_code == 204

        assert _get_like_from_db(session_factory, post["id"], user_a_id) is None

    # 10: unliking a post that was not liked returns 404
    def test_unliking_a_post_not_liked_returns_404(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.delete(f"/posts/{post['id']}/like", headers=headers_a)
        assert resp.status_code == 404

    # 11, 12: User A likes, User B independently likes the same post
    def test_user_a_and_user_b_can_independently_like_same_post(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_b = self._auth_headers(client, self.USER_B)

        post = self._create_post(client, headers_a)

        resp_a = client.post(f"/posts/{post['id']}/like", headers=headers_a)
        assert resp_a.status_code == 201
        resp_b = client.post(f"/posts/{post['id']}/like", headers=headers_b)
        assert resp_b.status_code == 201

        assert _get_like_from_db(session_factory, post["id"], user_a_id) is not None
        assert _get_like_from_db(session_factory, post["id"], user_b_id) is not None
        assert _count_likes_in_db(session_factory, post["id"]) == 2

    # 13: User B cannot remove User A's Like
    def test_user_b_unlike_does_not_remove_user_a_like(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_b = self._auth_headers(client, self.USER_B)

        post = self._create_post(client, headers_a)
        client.post(f"/posts/{post['id']}/like", headers=headers_a)
        client.post(f"/posts/{post['id']}/like", headers=headers_b)

        resp = client.delete(f"/posts/{post['id']}/like", headers=headers_b)
        assert resp.status_code == 204

        # User A's like must be untouched
        assert _get_like_from_db(session_factory, post["id"], user_a_id) is not None
        # User B's like is gone
        assert _get_like_from_db(session_factory, post["id"], user_b_id) is None
        assert _count_likes_in_db(session_factory, post["id"]) == 1

    # 14: nonexistent post returns 404 for both like and unlike
    def test_like_nonexistent_post_returns_404(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)

        resp = client.post("/posts/999999/like", headers=headers_a)
        assert resp.status_code == 404

    def test_unlike_nonexistent_post_returns_404(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)

        resp = client.delete("/posts/999999/like", headers=headers_a)
        assert resp.status_code == 404

    # 15, 16: unauthenticated like/unlike return 401
    def test_unauthenticated_like_returns_401(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(f"/posts/{post['id']}/like")
        assert resp.status_code == 401

    def test_unauthenticated_unlike_returns_401(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)
        client.post(f"/posts/{post['id']}/like", headers=headers_a)

        resp = client.delete(f"/posts/{post['id']}/like")
        assert resp.status_code == 401

    # 17: client cannot provide/override user_id (no request body is even
    # accepted by the endpoint, so any client-supplied id is a no-op)
    def test_client_cannot_override_user_id_via_body_or_query(self, security_client):
        client, session_factory = security_client
        register_a = self._register(client, self.USER_A)
        user_a_id = register_a.json()["id"]
        register_b = self._register(client, self.USER_B)
        user_b_id = register_b.json()["id"]
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        resp = client.post(
            f"/posts/{post['id']}/like?user_id={user_b_id}",
            json={"user_id": user_b_id},
            headers=headers_a,
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == user_a_id
        assert resp.json()["user_id"] != user_b_id

        db_like = _get_like_from_db(session_factory, post["id"], user_a_id)
        assert db_like is not None


class TestLikeDatabaseConstraint:
    """Confirms the (post_id, user_id) uniqueness is enforced at the
    database level itself, not only by the application's pre-check."""

    def test_unique_constraint_rejects_duplicate_even_bypassing_app_check(self, db_session):
        user = register_user(
            db_session, UserRegister(username="carl", email="carl@example.com", password="strongpass123")
        )
        post = models.Post(title="t", content="c", author_id=user.id)
        db_session.add(post)
        db_session.commit()
        db_session.refresh(post)

        db_session.add(models.Like(post_id=post.id, user_id=user.id))
        db_session.commit()

        # Insert a second, identical (post_id, user_id) row directly,
        # bypassing any application-level "already liked" check entirely.
        from sqlalchemy.exc import IntegrityError

        db_session.add(models.Like(post_id=post.id, user_id=user.id))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ---------------------------------------------------------------------------
# STEP 12 — Email notifications for comments and likes.
#
# Notification calls are mocked at the point of use (app.routers.comments /
# app.routers.likes), so no real SMTP connection is ever attempted and no
# real email is sent during tests, per the spec's requirement to use the
# project's test email mechanism rather than hitting a real mail server.
# ---------------------------------------------------------------------------


class TestCommentAndLikeNotifications:
    USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
    USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

    def _register(self, client, user):
        return client.post("/auth/register", json=user)

    def _auth_headers(self, client, user):
        resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _create_post(self, client, headers, title="My First Blog", content="Body"):
        return client.post("/posts", json={"title": title, "content": content}, headers=headers).json()

    # --- Comment notifications ---------------------------------------

    def test_comment_by_other_user_triggers_notification_to_post_owner(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)
        post = self._create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(
                f"/posts/{post['id']}/comments", json={"text": "Great post!"}, headers=headers_b
            )
        assert resp.status_code == 201

        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        assert kwargs["post_owner_email"] == self.USER_A["email"]
        assert kwargs["post_title"] == "My First Blog"
        assert kwargs["actor_username"] == self.USER_B["username"]

    def test_own_comment_on_own_post_does_not_trigger_notification(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(
                f"/posts/{post['id']}/comments", json={"text": "My own comment"}, headers=headers_a
            )
        assert resp.status_code == 201
        mock_notify.assert_not_called()

    def test_invalid_comment_triggers_no_notification_and_creates_nothing(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)
        post = self._create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(f"/posts/{post['id']}/comments", json={"text": ""}, headers=headers_b)
        assert resp.status_code == 422
        mock_notify.assert_not_called()

        db = session_factory()
        try:
            assert db.query(models.Comment).filter(models.Comment.post_id == post["id"]).count() == 0
        finally:
            db.close()

    def test_comment_on_nonexistent_post_triggers_no_notification(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post("/posts/999999/comments", json={"text": "orphan"}, headers=headers_a)
        assert resp.status_code == 404
        mock_notify.assert_not_called()

    def test_unauthenticated_comment_triggers_no_notification(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        with patch("app.routers.comments.send_comment_notification") as mock_notify:
            resp = client.post(f"/posts/{post['id']}/comments", json={"text": "no auth"})
        assert resp.status_code == 401
        mock_notify.assert_not_called()

    # --- Like notifications --------------------------------------------

    def test_like_by_other_user_triggers_notification_to_post_owner(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)
        post = self._create_post(client, headers_a)

        with patch("app.routers.likes.send_like_notification") as mock_notify:
            resp = client.post(f"/posts/{post['id']}/like", headers=headers_b)
        assert resp.status_code == 201

        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        assert kwargs["post_owner_email"] == self.USER_A["email"]
        assert kwargs["post_title"] == "My First Blog"
        assert kwargs["actor_username"] == self.USER_B["username"]

    def test_own_like_on_own_post_does_not_trigger_notification(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        with patch("app.routers.likes.send_like_notification") as mock_notify:
            resp = client.post(f"/posts/{post['id']}/like", headers=headers_a)
        assert resp.status_code == 201
        mock_notify.assert_not_called()

    def test_duplicate_like_triggers_no_second_notification(self, security_client):
        client, session_factory = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        self._register(client, self.USER_B)
        headers_b = self._auth_headers(client, self.USER_B)
        post = self._create_post(client, headers_a)

        with patch("app.routers.likes.send_like_notification") as mock_notify:
            first = client.post(f"/posts/{post['id']}/like", headers=headers_b)
            assert first.status_code == 201
            mock_notify.assert_called_once()

            second = client.post(f"/posts/{post['id']}/like", headers=headers_b)
            assert second.status_code == 409
            # still just the one call from the first, successful like
            mock_notify.assert_called_once()

        db = session_factory()
        try:
            assert db.query(models.Like).filter(models.Like.post_id == post["id"]).count() == 1
        finally:
            db.close()

    def test_like_on_nonexistent_post_triggers_no_notification(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)

        with patch("app.routers.likes.send_like_notification") as mock_notify:
            resp = client.post("/posts/999999/like", headers=headers_a)
        assert resp.status_code == 404
        mock_notify.assert_not_called()

    def test_unauthenticated_like_triggers_no_notification(self, security_client):
        client, _ = security_client
        self._register(client, self.USER_A)
        headers_a = self._auth_headers(client, self.USER_A)
        post = self._create_post(client, headers_a)

        with patch("app.routers.likes.send_like_notification") as mock_notify:
            resp = client.post(f"/posts/{post['id']}/like")
        assert resp.status_code == 401
        mock_notify.assert_not_called()


class TestNotificationServiceInternals:
    """Directly tests app.services.notifications.send_email's dev/test mode,
    real-SMTP path, and failure handling -- independent of the HTTP layer."""

    def test_dev_mode_never_touches_smtp(self):
        with patch.object(notifications_module, "EMAIL_ENABLED", False), patch("smtplib.SMTP") as mock_smtp:
            notifications_module.send_email("a@example.com", "Subject", "Body")
        mock_smtp.assert_not_called()

    def test_enabled_mode_sends_via_smtp_with_tls_and_login(self):
        with patch.object(notifications_module, "EMAIL_ENABLED", True), \
             patch.object(notifications_module, "MAIL_USE_TLS", True), \
             patch.object(notifications_module, "MAIL_USERNAME", "user"), \
             patch.object(notifications_module, "MAIL_PASSWORD", "pass"), \
             patch("smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value.__enter__.return_value = instance

            notifications_module.send_email("owner@example.com", "Hi", "Body text")

            instance.starttls.assert_called_once()
            instance.login.assert_called_once_with("user", "pass")
            instance.send_message.assert_called_once()

    def test_smtp_failure_is_caught_and_never_raised(self):
        with patch.object(notifications_module, "EMAIL_ENABLED", True), \
             patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            # must not raise
            notifications_module.send_email("owner@example.com", "Hi", "Body text")

    def test_send_comment_notification_builds_expected_subject_and_body(self):
        """Body must follow the exact required format:

            Post: "<title>"
            User: <username>
            Activity: Commented on your post
            Time: <YYYY-MM-DD HH:MM AM/PM>

        -- four lines, nothing else (no raw comment text)."""
        with patch.object(notifications_module, "send_email") as mock_send_email:
            notifications_module.send_comment_notification(
                post_owner_email="owner@example.com",
                post_title="FastAPI Best Practices",
                actor_username="John Doe",
            )
        mock_send_email.assert_called_once()
        args, _ = mock_send_email.call_args
        to_email, subject, body = args
        assert to_email == "owner@example.com"
        assert subject == "New comment on your blog post"

        lines = body.splitlines()
        assert lines[0] == 'Post: "FastAPI Best Practices"'
        assert lines[1] == "User: John Doe"
        assert lines[2] == "Activity: Commented on your post"
        assert lines[3].startswith("Time: ")
        # The timestamp itself must parse back as "YYYY-MM-DD HH:MM AM/PM".
        datetime.strptime(lines[3].removeprefix("Time: "), "%Y-%m-%d %I:%M %p")

    def test_send_like_notification_builds_expected_subject_and_body(self):
        with patch.object(notifications_module, "send_email") as mock_send_email:
            notifications_module.send_like_notification(
                post_owner_email="owner@example.com",
                post_title="FastAPI Best Practices",
                actor_username="John Doe",
            )
        mock_send_email.assert_called_once()
        args, _ = mock_send_email.call_args
        to_email, subject, body = args
        assert to_email == "owner@example.com"
        assert subject == "Someone liked your blog post"

        lines = body.splitlines()
        assert lines[0] == 'Post: "FastAPI Best Practices"'
        assert lines[1] == "User: John Doe"
        assert lines[2] == "Activity: Liked your post"
        assert lines[3].startswith("Time: ")
        datetime.strptime(lines[3].removeprefix("Time: "), "%Y-%m-%d %I:%M %p")
