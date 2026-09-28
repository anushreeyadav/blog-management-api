"""
Automatic publishing of scheduled posts.

The project has no task queue or scheduler (no Celery/Beat, APScheduler,
cron config or Django management commands), so this is the simplest thing
that works reliably without adding one:

- run_once(): one publishing pass -- app/services/post_publishing.py's
  publish_due_posts in its own session/transaction, committed, with any
  database error rolled back and logged rather than raised.
- A background asyncio task started from app/main.py's lifespan, calling
  run_once() every SCHEDULED_PUBLISHING_INTERVAL_SECONDS (default 60). The
  first pass runs immediately at startup, so posts that fell due while the
  app was down are published as soon as it's back.
- `python -m app.services.scheduled_publishing` runs a single pass and
  exits, for driving it from cron / Windows Task Scheduler instead (set
  SCHEDULED_PUBLISHING_ENABLED=false to turn the in-app loop off then).

Safe to run more than once at a time: publish_due_posts is a single
conditional UPDATE, so no post can be published twice. On PostgreSQL a
transaction-level advisory lock additionally makes overlapping passes
(e.g. several uvicorn workers, or the loop plus a cron run) skip rather
than queue behind each other; it's released automatically on commit,
rollback or a crashed connection, so a restart can never leave it held.
All state lives in the posts table itself -- nothing to recover on restart.
"""
import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.database import SessionLocal
from app.services import post_publishing

logger = logging.getLogger(__name__)

# Same env-flag convention as EMAIL_ENABLED (app/services/notifications.py).
SCHEDULED_PUBLISHING_ENABLED = os.getenv("SCHEDULED_PUBLISHING_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
SCHEDULED_PUBLISHING_INTERVAL_SECONDS = max(5, int(os.getenv("SCHEDULED_PUBLISHING_INTERVAL_SECONDS", "60")))

# Arbitrary fixed key identifying this job's PostgreSQL advisory lock.
_ADVISORY_LOCK_KEY = 7_340_021_501

_task: asyncio.Task | None = None


def _acquire_run_lock(db: Session) -> bool:
    """True if this pass may run. Always True on non-PostgreSQL databases,
    where the conditional UPDATE alone already prevents double publishing."""
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}).scalar())


def run_once(session_factory: sessionmaker = SessionLocal) -> list[int]:
    """One publishing pass. Returns the ids published (empty if none were
    due, another pass held the lock, or the database failed)."""
    db = session_factory()
    try:
        if not _acquire_run_lock(db):
            db.rollback()
            logger.debug("Scheduled publishing: another pass is running, skipping")
            return []
        published_ids = post_publishing.publish_due_posts(db)
        db.commit()
        if published_ids:
            logger.info("Scheduled publishing: published %d post(s): %s", len(published_ids), published_ids)
        return published_ids
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Scheduled publishing: database error; will retry on the next pass")
        return []
    finally:
        db.close()


async def _run_forever(interval_seconds: int) -> None:
    while True:
        try:
            # Sync SQLAlchemy -- keep it off the event loop.
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad pass (e.g. the database being unreachable
            # before a session could even be created) stop the loop.
            logger.exception("Scheduled publishing: unexpected error; will retry on the next pass")
        await asyncio.sleep(interval_seconds)


def start() -> asyncio.Task | None:
    """Starts the background loop, once per process: calling it again while
    it's running returns the existing task instead of starting a second."""
    global _task
    if not SCHEDULED_PUBLISHING_ENABLED:
        logger.info("Scheduled publishing loop disabled (SCHEDULED_PUBLISHING_ENABLED=false)")
        return None
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(
        _run_forever(SCHEDULED_PUBLISHING_INTERVAL_SECONDS), name="scheduled-post-publishing"
    )
    logger.info("Scheduled publishing loop started (every %ds)", SCHEDULED_PUBLISHING_INTERVAL_SECONDS)
    return _task


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ids = run_once()
    print(f"Published {len(ids)} scheduled post(s){': ' + ', '.join(map(str, ids)) if ids else ''}")
