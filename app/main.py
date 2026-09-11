from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import models  # noqa: F401 - registers models on Base.metadata
from app.database import Base, engine
from app.routers import auth, comments, likes, posts


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Blog Management API", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(posts.router)
app.include_router(comments.router)
app.include_router(likes.router)


@app.get("/")
def read_root():
    return {"status": "ok"}
