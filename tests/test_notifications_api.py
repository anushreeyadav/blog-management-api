"""
HTTP-level tests for GET /notifications/, wired into app.main.app.

Notifications are only ever created as a side effect of other actions
(likes, comments, subscription events -- see app/routers/likes.py,
comments.py, subscriptions.py); there is no POST /notifications endpoint,
so these tests insert Notification rows directly via the test database
session to exercise the read side in isolation.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app


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
        yield test_client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient, username: str, email: str, password: str = "strongpass123") -> dict:
    client.post("/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _add_notification(
    session_factory,
    *,
    user_id: int,
    message: str,
    notification_type: str,
    is_read: bool = False,
    created_at: datetime | None = None,
) -> int:
    db = session_factory()
    try:
        notification = models.Notification(
            user_id=user_id,
            message=message,
            notification_type=notification_type,
            is_read=is_read,
        )
        db.add(notification)
        db.commit()
        if created_at is not None:
            db.refresh(notification)
            notification.created_at = created_at
            db.commit()
        db.refresh(notification)
        return notification.id
    finally:
        db.close()


class TestNotificationsAuth:
    def test_requires_authentication(self, auth_client):
        client, _ = auth_client
        resp = client.get("/notifications/")
        assert resp.status_code == 401

    def test_rejects_invalid_token(self, auth_client):
        client, _ = auth_client
        resp = client.get("/notifications/", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401


class TestNotificationsResponseShape:
    def test_empty_for_new_user(self, auth_client):
        client, _ = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")

        resp = client.get("/notifications/", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"notifications": [], "unread_count": 0}

    def test_returns_newest_first_with_correct_unread_count_and_fields(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()

        now = datetime.now(timezone.utc)
        _add_notification(
            session_factory,
            user_id=me["id"],
            message="oldest, read",
            notification_type="comment",
            is_read=True,
            created_at=now - timedelta(minutes=10),
        )
        _add_notification(
            session_factory,
            user_id=me["id"],
            message="middle, unread",
            notification_type="like",
            is_read=False,
            created_at=now - timedelta(minutes=5),
        )
        _add_notification(
            session_factory,
            user_id=me["id"],
            message="newest, unread",
            notification_type="subscription_activated",
            is_read=False,
            created_at=now,
        )

        resp = client.get("/notifications/", headers=headers)
        assert resp.status_code == 200
        body = resp.json()

        assert body["unread_count"] == 2
        assert [n["message"] for n in body["notifications"]] == [
            "newest, unread",
            "middle, unread",
            "oldest, read",
        ]

        first = body["notifications"][0]
        assert set(first.keys()) == {"id", "message", "notification_type", "is_read", "created_at"}
        assert first["notification_type"] == "subscription_activated"
        assert first["is_read"] is False
        assert isinstance(first["id"], int)


class TestNotificationsCrossUserIsolation:
    def test_only_returns_the_caller_own_notifications(self, auth_client):
        client, session_factory = auth_client
        headers_a = _register_and_login(client, "alice", "alice@example.com")
        headers_b = _register_and_login(client, "bob", "bob@example.com")
        me_a = client.get("/auth/me", headers=headers_a).json()
        me_b = client.get("/auth/me", headers=headers_b).json()

        _add_notification(session_factory, user_id=me_a["id"], message="for alice", notification_type="like")
        _add_notification(session_factory, user_id=me_b["id"], message="for bob (1)", notification_type="like")
        _add_notification(session_factory, user_id=me_b["id"], message="for bob (2)", notification_type="comment")

        resp_a = client.get("/notifications/", headers=headers_a)
        assert resp_a.status_code == 200
        body_a = resp_a.json()
        assert len(body_a["notifications"]) == 1
        assert body_a["notifications"][0]["message"] == "for alice"
        assert body_a["unread_count"] == 1

        resp_b = client.get("/notifications/", headers=headers_b)
        assert resp_b.status_code == 200
        body_b = resp_b.json()
        assert {n["message"] for n in body_b["notifications"]} == {"for bob (1)", "for bob (2)"}
        assert body_b["unread_count"] == 2

    def test_no_user_id_parameter_accepted(self, auth_client):
        """There is no /notifications/{user_id} route -- a query string id
        is silently ignored (FastAPI drops unrecognized params) rather than
        changing whose notifications come back."""
        client, session_factory = auth_client
        headers_a = _register_and_login(client, "alice", "alice@example.com")
        headers_b = _register_and_login(client, "bob", "bob@example.com")
        me_b = client.get("/auth/me", headers=headers_b).json()

        _add_notification(session_factory, user_id=me_b["id"], message="for bob", notification_type="like")

        resp = client.get(f"/notifications/?user_id={me_b['id']}", headers=headers_a)
        assert resp.status_code == 200
        assert resp.json() == {"notifications": [], "unread_count": 0}


class TestMarkNotificationRead:
    def test_marks_own_notification_as_read(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()
        notification_id = _add_notification(
            session_factory, user_id=me["id"], message="unread one", notification_type="like"
        )

        list_before = client.get("/notifications/", headers=headers).json()
        assert list_before["unread_count"] == 1
        assert list_before["notifications"][0]["is_read"] is False

        resp = client.patch(f"/notifications/{notification_id}/read", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == notification_id
        assert body["is_read"] is True
        assert body["message"] == "unread one"

        list_after = client.get("/notifications/", headers=headers).json()
        assert list_after["unread_count"] == 0
        assert list_after["notifications"][0]["is_read"] is True

    def test_marking_already_read_notification_is_idempotent(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()
        notification_id = _add_notification(
            session_factory, user_id=me["id"], message="already read", notification_type="like", is_read=True
        )

        resp = client.patch(f"/notifications/{notification_id}/read", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["is_read"] is True

    def test_returns_404_for_nonexistent_notification(self, auth_client):
        client, _ = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")

        resp = client.patch("/notifications/999999/read", headers=headers)
        assert resp.status_code == 404

    def test_cannot_mark_another_users_notification_as_read(self, auth_client):
        client, session_factory = auth_client
        headers_a = _register_and_login(client, "alice", "alice@example.com")
        headers_b = _register_and_login(client, "bob", "bob@example.com")
        me_b = client.get("/auth/me", headers=headers_b).json()
        notification_id = _add_notification(
            session_factory, user_id=me_b["id"], message="for bob only", notification_type="like"
        )

        # Alice tries to mark Bob's notification as read -- same 404 as a
        # nonexistent id, never a 403 that would confirm the id is real.
        resp = client.patch(f"/notifications/{notification_id}/read", headers=headers_a)
        assert resp.status_code == 404

        # Confirm it was left completely untouched.
        bob_list = client.get("/notifications/", headers=headers_b).json()
        assert bob_list["unread_count"] == 1
        assert bob_list["notifications"][0]["is_read"] is False

    def test_requires_authentication(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()
        notification_id = _add_notification(
            session_factory, user_id=me["id"], message="unread", notification_type="like"
        )

        resp = client.patch(f"/notifications/{notification_id}/read")
        assert resp.status_code == 401


class TestMarkNotificationUnread:
    def test_full_read_to_unread_to_read_flow(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()
        notification_id = _add_notification(
            session_factory, user_id=me["id"], message="flow test", notification_type="like"
        )

        # starts unread
        assert client.get("/notifications/", headers=headers).json()["unread_count"] == 1

        # -> read
        read_resp = client.patch(f"/notifications/{notification_id}/read", headers=headers)
        assert read_resp.status_code == 200
        assert read_resp.json()["is_read"] is True
        assert client.get("/notifications/", headers=headers).json()["unread_count"] == 0

        # -> unread
        unread_resp = client.patch(f"/notifications/{notification_id}/unread", headers=headers)
        assert unread_resp.status_code == 200
        assert unread_resp.json()["id"] == notification_id
        assert unread_resp.json()["is_read"] is False
        assert client.get("/notifications/", headers=headers).json()["unread_count"] == 1

        # -> read again
        read_again = client.patch(f"/notifications/{notification_id}/read", headers=headers)
        assert read_again.status_code == 200
        assert read_again.json()["is_read"] is True
        assert client.get("/notifications/", headers=headers).json()["unread_count"] == 0

    def test_marking_already_unread_notification_is_idempotent(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()
        notification_id = _add_notification(
            session_factory, user_id=me["id"], message="already unread", notification_type="like", is_read=False
        )

        resp = client.patch(f"/notifications/{notification_id}/unread", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["is_read"] is False

    def test_returns_404_for_nonexistent_notification(self, auth_client):
        client, _ = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")

        resp = client.patch("/notifications/999999/unread", headers=headers)
        assert resp.status_code == 404

    def test_cannot_mark_another_users_notification_as_unread(self, auth_client):
        client, session_factory = auth_client
        headers_a = _register_and_login(client, "alice", "alice@example.com")
        headers_b = _register_and_login(client, "bob", "bob@example.com")
        me_b = client.get("/auth/me", headers=headers_b).json()
        notification_id = _add_notification(
            session_factory, user_id=me_b["id"], message="for bob only", notification_type="like", is_read=True
        )

        resp = client.patch(f"/notifications/{notification_id}/unread", headers=headers_a)
        assert resp.status_code == 404

        # Confirm Bob's read notification was left completely untouched.
        bob_list = client.get("/notifications/", headers=headers_b).json()
        assert bob_list["unread_count"] == 0
        assert bob_list["notifications"][0]["is_read"] is True

    def test_requires_authentication(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()
        notification_id = _add_notification(
            session_factory, user_id=me["id"], message="read", notification_type="like", is_read=True
        )

        resp = client.patch(f"/notifications/{notification_id}/unread")
        assert resp.status_code == 401


class TestMarkAllNotificationsRead:
    def test_marks_all_unread_notifications_and_reports_count(self, auth_client):
        client, session_factory = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")
        me = client.get("/auth/me", headers=headers).json()

        _add_notification(session_factory, user_id=me["id"], message="one", notification_type="like")
        _add_notification(session_factory, user_id=me["id"], message="two", notification_type="comment")
        _add_notification(
            session_factory,
            user_id=me["id"],
            message="already read",
            notification_type="like",
            is_read=True,
        )

        before = client.get("/notifications/", headers=headers).json()
        assert before["unread_count"] == 2

        resp = client.patch("/notifications/read-all", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"message": "All notifications marked as read", "updated_count": 2}

        after = client.get("/notifications/", headers=headers).json()
        assert after["unread_count"] == 0
        assert all(n["is_read"] for n in after["notifications"])

    def test_zero_unread_returns_zero_updated_count(self, auth_client):
        client, _ = auth_client
        headers = _register_and_login(client, "alice", "alice@example.com")

        resp = client.patch("/notifications/read-all", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"message": "All notifications marked as read", "updated_count": 0}

    def test_does_not_modify_another_users_notifications(self, auth_client):
        client, session_factory = auth_client
        headers_a = _register_and_login(client, "alice", "alice@example.com")
        headers_b = _register_and_login(client, "bob", "bob@example.com")
        me_a = client.get("/auth/me", headers=headers_a).json()
        me_b = client.get("/auth/me", headers=headers_b).json()

        _add_notification(session_factory, user_id=me_a["id"], message="alice's", notification_type="like")
        _add_notification(session_factory, user_id=me_b["id"], message="bob's", notification_type="like")

        resp = client.patch("/notifications/read-all", headers=headers_a)
        assert resp.status_code == 200
        assert resp.json()["updated_count"] == 1

        # Alice's is now read, but Bob's own unread notification is untouched.
        bob_list = client.get("/notifications/", headers=headers_b).json()
        assert bob_list["unread_count"] == 1
        assert bob_list["notifications"][0]["is_read"] is False

    def test_requires_authentication(self, auth_client):
        client, _ = auth_client
        resp = client.patch("/notifications/read-all")
        assert resp.status_code == 401
