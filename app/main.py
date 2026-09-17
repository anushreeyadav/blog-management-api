from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import models  # noqa: F401 - registers models on Base.metadata
from app.database import Base, SessionLocal, engine, ensure_post_image_column
from app.routers import admin, auth, comments, likes, posts, subscriptions
from app.services.media import MEDIA_ROOT
from app.services.plans import seed_default_plans


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_post_image_column()
    db = SessionLocal()
    try:
        seed_default_plans(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Blog Management API", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(posts.router)
app.include_router(comments.router)
app.include_router(likes.router)
app.include_router(subscriptions.router)
app.include_router(admin.router)

app.mount("/media", StaticFiles(directory=str(MEDIA_ROOT)), name="media")


@app.get("/")
def read_root():
    return {"status": "ok"}
