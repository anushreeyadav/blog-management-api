import pytest

from app.services import invoices as invoices_module
from app.services import media as media_module
from app.services import notifications as notifications_module


@pytest.fixture(autouse=True)
def _disable_real_email_sending(monkeypatch):
    """
    Automated tests must never depend on, or trigger, real email delivery --
    regardless of whatever EMAIL_ENABLED/SMTP credentials are configured in
    the local .env for manual testing. This forces the notification service
    into its safe dev/test (logging-only) mode for every test.
    """
    monkeypatch.setattr(notifications_module, "EMAIL_ENABLED", False)


@pytest.fixture(autouse=True)
def _isolated_invoices_dir(tmp_path, monkeypatch):
    """
    Every /subscriptions/subscribe call generates a real invoice PDF (see
    app/services/invoices.py). Tests must never write those into the
    project's real media/invoices/ directory, so this redirects every test
    to an isolated tmp directory -- applied globally since invoice
    generation is reachable from almost any test file that subscribes a
    user, not just the billing-focused ones.
    """
    invoices_dir = tmp_path / "invoices"
    invoices_dir.mkdir(parents=True)
    monkeypatch.setattr(invoices_module, "INVOICES_DIR", invoices_dir)


@pytest.fixture(autouse=True)
def _isolated_posts_media_dir(tmp_path, monkeypatch):
    """
    Every successful image upload writes a real file (see
    app/services/media.py). This used to be isolated per-file (a handful
    of test files each declared their own _isolated_media_dir fixture),
    but several newer test files that also upload images -- added in
    later sub-tasks -- never did, and were silently writing real files
    into the project's actual media/posts/ directory on every test run
    (discovered during Sub-Task 20's final review: ~150 stray files had
    accumulated there). Applying this globally, the same way
    _isolated_invoices_dir already is, closes that gap for every test
    file, present and future, not just the ones that remember to opt in.
    """
    posts_dir = tmp_path / "posts"
    posts_dir.mkdir(parents=True)
    monkeypatch.setattr(media_module, "POSTS_MEDIA_DIR", posts_dir)
