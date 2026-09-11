import pytest

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
