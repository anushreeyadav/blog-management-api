"""
Post cover image upload tests: POST /posts/{post_id}/image.

Mirrors the security_client pattern from test_api.py (an isolated in-memory
SQLite database per test via a get_db override), plus an autouse fixture
that points app.services.media.POSTS_MEDIA_DIR at a temp directory so tests
never read or write the project's real media/posts folder.
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

# Minimal byte sequences that satisfy each format's magic-byte signature --
# they don't need to be structurally valid images since save_post_image only
# sniffs the header.
VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
VALID_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
INVALID_FILE = b"just some plain text, not an image at all"
OVERSIZED_JPEG = b"\xff\xd8\xff\xe0" + (b"\x00" * (media_module.MAX_IMAGE_SIZE + 1))


@pytest.fixture(autouse=True)
def _isolated_media_dir(tmp_path, monkeypatch):
    posts_dir = tmp_path / "media" / "posts"
    posts_dir.mkdir(parents=True)
    monkeypatch.setattr(media_module, "POSTS_MEDIA_DIR", posts_dir)
    yield posts_dir


@pytest.fixture()
def image_client():
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


USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}


def _register(client, user):
    return client.post("/auth/register", json=user)


def _auth_headers(client, user):
    resp = client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_post(client, headers, title="Hello", content="World"):
    return client.post("/posts", json={"title": title, "content": content}, headers=headers).json()


def _get_post_from_db(session_factory, post_id: int) -> models.Post:
    db = session_factory()
    try:
        post = db.query(models.Post).filter(models.Post.id == post_id).first()
        if post is not None:
            db.expunge(post)
        return post
    finally:
        db.close()


class TestCreatePostWithoutImage:
    # 1: existing JSON create flow is untouched and still works
    def test_create_post_without_image_succeeds(self, image_client):
        client, _ = image_client
        self._register_and_login(client)
        headers = _auth_headers(client, USER_A)
        resp = client.post("/posts", json={"title": "No image", "content": "Body"}, headers=headers)
        assert resp.status_code == 201
        assert resp.json()["image"] is None

    def _register_and_login(self, client):
        _register(client, USER_A)


class TestImageUploadValidation:
    # 2, 3: valid JPG / PNG succeed
    def test_upload_valid_jpeg_succeeds(self, image_client, _isolated_media_dir):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["image"].startswith("/media/posts/")
        assert body["image"].endswith(".jpg")

        stored_files = list(_isolated_media_dir.iterdir())
        assert len(stored_files) == 1

    def test_upload_valid_png_succeeds(self, image_client, _isolated_media_dir):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.png", io.BytesIO(VALID_PNG), "image/png")},
        )
        assert resp.status_code == 200
        assert resp.json()["image"].endswith(".png")

    def test_upload_valid_webp_succeeds(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.webp", io.BytesIO(VALID_WEBP), "image/webp")},
        )
        assert resp.status_code == 200
        assert resp.json()["image"].endswith(".webp")

    # 4: invalid file type rejected
    def test_upload_invalid_file_type_rejected(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("notes.txt", io.BytesIO(INVALID_FILE), "text/plain")},
        )
        assert resp.status_code == 400

    # A file whose extension/content-type CLAIM to be a JPEG but whose bytes
    # are not must still be rejected -- the filename/content-type are never
    # trusted, only the sniffed magic bytes are.
    def test_upload_spoofed_extension_rejected(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(INVALID_FILE), "image/jpeg")},
        )
        assert resp.status_code == 400

    # 5: oversized file rejected
    def test_upload_oversized_file_rejected(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(OVERSIZED_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 413

    def test_upload_requires_authentication(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 401

    def test_upload_to_nonexistent_post_returns_404(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)

        resp = client.post(
            "/posts/999999/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 404


class TestImageOwnershipAndReplacement:
    # 6: owner can upload a new image
    def test_owner_can_update_image(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 200
        assert resp.json()["image"] is not None

    # 7: updating the post's text without a new image leaves the image unchanged
    def test_post_text_update_leaves_existing_image_unchanged(self, image_client):
        client, session_factory = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)
        client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        image_after_upload = _get_post_from_db(session_factory, post["id"]).image

        resp = client.put(f"/posts/{post['id']}", json={"title": "Updated title"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["image"] == image_after_upload

    # replacing an image removes the old file and stores the new one
    def test_replacing_image_removes_old_file(self, image_client, _isolated_media_dir):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        first = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        first_image = first.json()["image"]

        second = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover2.png", io.BytesIO(VALID_PNG), "image/png")},
        )
        second_image = second.json()["image"]

        assert first_image != second_image
        stored_files = {f.name for f in _isolated_media_dir.iterdir()}
        assert second_image.rsplit("/", 1)[-1] in stored_files
        assert first_image.rsplit("/", 1)[-1] not in stored_files

    # 8: User B cannot attach an image to User A's post
    def test_non_owner_cannot_upload_image(self, image_client, _isolated_media_dir):
        client, _ = image_client
        _register(client, USER_A)
        headers_a = _auth_headers(client, USER_A)
        post = _create_post(client, headers_a)

        _register(client, USER_B)
        headers_b = _auth_headers(client, USER_B)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers_b,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        assert resp.status_code == 403
        assert list(_isolated_media_dir.iterdir()) == []


class TestImageInPostResponses:
    # 9: get post with image returns the image URL
    def test_get_post_with_image_returns_url(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)
        client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )

        resp = client.get(f"/posts/{post['id']}")
        assert resp.status_code == 200
        assert resp.json()["image"].startswith("/media/posts/")

    # 10: get post without image returns null
    def test_get_post_without_image_returns_null(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.get(f"/posts/{post['id']}")
        assert resp.status_code == 200
        assert resp.json()["image"] is None

    def test_list_posts_includes_image_field(self, image_client):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        _create_post(client, headers)

        resp = client.get("/posts")
        assert resp.status_code == 200
        assert "image" in resp.json()[0]


# 11: uploaded image is physically stored under media/posts/ (the isolated
# tmp-path equivalent of it, per the _isolated_media_dir fixture)
class TestImagePhysicalStorage:
    def test_uploaded_file_is_written_to_posts_media_dir(self, image_client, _isolated_media_dir):
        client, _ = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        resp = client.post(
            f"/posts/{post['id']}/image",
            headers=headers,
            files={"image": ("cover.jpg", io.BytesIO(VALID_JPEG), "image/jpeg")},
        )
        image_url = resp.json()["image"]
        filename = image_url.rsplit("/", 1)[-1]
        assert (_isolated_media_dir / filename).is_file()


# 12: existing posts created before this feature (image = NULL) keep working
class TestBackwardCompatibility:
    def test_existing_post_without_image_column_value_still_returns_200(self, image_client):
        client, session_factory = image_client
        _register(client, USER_A)
        headers = _auth_headers(client, USER_A)
        post = _create_post(client, headers)

        db = session_factory()
        try:
            db_post = db.query(models.Post).filter(models.Post.id == post["id"]).first()
            assert db_post.image is None
        finally:
            db.close()

        resp = client.get(f"/posts/{post['id']}")
        assert resp.status_code == 200
        assert resp.json()["image"] is None
