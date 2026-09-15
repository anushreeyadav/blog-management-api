"""
Post cover image storage.

Uploaded files are validated by sniffing their magic bytes (never trusting
the client-supplied filename or Content-Type), saved under POSTS_MEDIA_DIR
with a generated UUID filename, and referenced from the database only by
their relative /media/posts/<file> URL -- never as binary data in SQLite.
"""

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEDIA_ROOT = PROJECT_ROOT / "media"
POSTS_MEDIA_DIR = MEDIA_ROOT / "posts"
POSTS_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB


def _sniff_extension(header: bytes) -> str:
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported image type. Only JPEG, PNG, and WEBP images are allowed.",
    )


async def save_post_image(upload: UploadFile) -> str:
    """Validate and persist an uploaded post image; returns its public URL path."""
    content = await upload.read(MAX_IMAGE_SIZE + 1)
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Image exceeds the maximum allowed size of {MAX_IMAGE_SIZE // (1024 * 1024)} MB.",
        )
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    extension = _sniff_extension(content[:16])
    filename = f"{uuid.uuid4().hex}{extension}"
    # POSTS_MEDIA_DIR is read fresh here (not captured at def time) so tests
    # can point it at an isolated tmp directory via monkeypatch.
    destination = POSTS_MEDIA_DIR / filename
    destination.write_bytes(content)
    await upload.close()
    return f"/media/posts/{filename}"


def delete_post_image(image_path: str | None) -> None:
    """Best-effort removal of a previously stored image. Never raises."""
    if not image_path:
        return
    # Only the basename is trusted, so a stored path can never escape
    # POSTS_MEDIA_DIR via traversal segments.
    filename = Path(image_path).name
    file_path = POSTS_MEDIA_DIR / filename
    try:
        if file_path.is_file():
            file_path.unlink()
    except OSError:
        pass
