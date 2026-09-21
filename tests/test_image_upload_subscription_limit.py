"""
Sub-Task 8 -- image upload subscription limits, exercised through the real
POST /posts/{post_id}/image endpoint with the real seeded Basic / Premium /
Pro plans: authenticate -> resolve active plan -> count images already on
*this* post -> compare against max_images -> allow or reject with the
standard message.
"""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app as main_app
from app.services import media as media_module

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
VALID_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}

LIMIT_MESSAGE = "You’ve reached your plan limit. Kindly upgrade your plan to continue."


@pytest.fixture(autouse=True)
def _isolated_media_dir(tmp_path, monkeypatch):
    posts_dir = tmp_path / "media" / "posts"
    posts_dir.mkdir(parents=True)
    monkeypatch.setattr(media_module, "POSTS_MEDIA_DIR", posts_dir)
    yield posts_dir


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


def _register_and_login(client: TestClient) -> dict:
    client.post("/auth/register", json=USER_A)
    resp = client.post("/auth/login", json={"username": USER_A["username"], "password": USER_A["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()["plans"]
    return next(p["id"] for p in plans if p["slug"] == slug)


def _create_post(client: TestClient, headers: dict, title: str = "target"):
    return client.post("/posts", json={"title": title, "content": "body"}, headers=headers).json()


def _upload_image(client: TestClient, headers: dict, post_id: int, filename="cover.jpg", content=VALID_JPEG, ctype="image/jpeg"):
    return client.post(
        f"/posts/{post_id}/image",
        headers=headers,
        files={"image": (filename, io.BytesIO(content), ctype)},
    )


class TestBasicUserImageLimit:
    def test_basic_user_uploads_first_image_successfully(self, client):
        headers = _register_and_login(client)  # registration defaults to Basic
        post = _create_post(client, headers)

        resp = _upload_image(client, headers, post["id"])

        assert resp.status_code == 200
        body = resp.json()
        assert body["image"].startswith("/media/posts/")
        assert body["images"] == [body["image"]]

    def test_basic_user_second_image_is_rejected(self, client):
        headers = _register_and_login(client)
        post = _create_post(client, headers)
        assert _upload_image(client, headers, post["id"]).status_code == 200

        resp = _upload_image(client, headers, post["id"], filename="cover2.png", content=VALID_PNG, ctype="image/png")

        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE

        # The rejected upload must not have replaced or added anything.
        get_resp = client.get(f"/posts/{post['id']}")
        assert len(get_resp.json()["images"]) == 1


class TestPremiumUserImageLimit:
    def _subscribe_to_premium(self, client, headers):
        plan_id = _plan_id(client, "premium")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers).status_code == 201

    def test_premium_user_uploads_first_and_second_image(self, client):
        headers = _register_and_login(client)
        self._subscribe_to_premium(client, headers)
        post = _create_post(client, headers)

        first = _upload_image(client, headers, post["id"], filename="cover1.jpg", content=VALID_JPEG, ctype="image/jpeg")
        second = _upload_image(client, headers, post["id"], filename="cover2.png", content=VALID_PNG, ctype="image/png")

        assert first.status_code == 200
        assert second.status_code == 200
        assert len(second.json()["images"]) == 2

    def test_premium_user_third_image_is_rejected(self, client):
        headers = _register_and_login(client)
        self._subscribe_to_premium(client, headers)
        post = _create_post(client, headers)
        _upload_image(client, headers, post["id"], filename="cover1.jpg", content=VALID_JPEG, ctype="image/jpeg")
        _upload_image(client, headers, post["id"], filename="cover2.png", content=VALID_PNG, ctype="image/png")

        resp = _upload_image(client, headers, post["id"], filename="cover3.webp", content=VALID_WEBP, ctype="image/webp")

        assert resp.status_code == 403
        assert resp.json()["detail"] == LIMIT_MESSAGE


class TestProUserImageLimit:
    def test_pro_user_uploads_many_images_without_an_artificial_limit(self, client):
        headers = _register_and_login(client)
        plan_id = _plan_id(client, "pro")
        assert client.post("/subscriptions/subscribe", json={"plan_id": plan_id}, headers=headers).status_code == 201
        post = _create_post(client, headers)

        for i in range(5):
            resp = _upload_image(client, headers, post["id"], filename=f"cover{i}.jpg", content=VALID_JPEG, ctype="image/jpeg")
            assert resp.status_code == 200

        final = client.get(f"/posts/{post['id']}")
        assert len(final.json()["images"]) == 5


class TestExistingImageFunctionalityUnchanged:
    """Everything else about image uploads -- validation, storage,
    URL generation, ownership, response shape -- must be exactly as before."""

    def test_invalid_file_type_still_rejected(self, client):
        headers = _register_and_login(client)
        post = _create_post(client, headers)

        resp = _upload_image(client, headers, post["id"], filename="notes.txt", content=b"not an image", ctype="text/plain")
        assert resp.status_code == 400

    def test_upload_requires_authentication(self, client):
        post_owner_headers = _register_and_login(client)
        post = _create_post(client, post_owner_headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 401

    def test_non_owner_cannot_upload(self, client):
        headers_a = _register_and_login(client)
        post = _create_post(client, headers_a)

        client.post("/auth/register", json={"username": "user_b", "email": "userb@example.com", "password": "Password123"})
        login_b = client.post("/auth/login", json={"username": "user_b", "password": "Password123"})
        headers_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

        resp = _upload_image(client, headers_b, post["id"])
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to modify this post"

    def test_stored_under_media_posts_with_generated_url(self, client, _isolated_media_dir):
        headers = _register_and_login(client)
        post = _create_post(client, headers)

        resp = _upload_image(client, headers, post["id"])

        image_url = resp.json()["image"]
        assert image_url.startswith("/media/posts/")
        filename = image_url.rsplit("/", 1)[-1]
        assert (_isolated_media_dir / filename).is_file()

    def test_get_post_still_returns_the_image_field(self, client):
        headers = _register_and_login(client)
        post = _create_post(client, headers)
        _upload_image(client, headers, post["id"])

        resp = client.get(f"/posts/{post['id']}")
        assert resp.status_code == 200
        assert resp.json()["image"] is not None

    def test_post_without_any_image_has_empty_gallery(self, client):
        headers = _register_and_login(client)
        post = _create_post(client, headers)

        resp = client.get(f"/posts/{post['id']}")
        assert resp.json()["image"] is None
        assert resp.json()["images"] == []
