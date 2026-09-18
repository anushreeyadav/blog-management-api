"""
Sub-Task 13 -- invoice PDF generation via ReportLab
(app/services/invoices.py), exercised both as a standalone service (unit
tests, no HTTP) and through the real POST /subscriptions/subscribe flow.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app as main_app
from app.services import invoices as invoices_module

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}


def _pdf_text(path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ---------------------------------------------------------------------------
# Standalone service tests -- no HTTP, no database required
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_invoices_dir_already_set(monkeypatch):
    # tests/conftest.py's autouse _isolated_invoices_dir fixture already
    # redirects invoices_module.INVOICES_DIR for every test in this file;
    # nothing extra to do here, kept only as documentation of that reliance.
    yield


class _FakeUser:
    username = "jane_doe"
    email = "jane@example.com"


class _FakePlan:
    name = "Premium"
    price = 9.99


class TestInvoicePdfIsGenerated:
    def test_generate_invoice_pdf_returns_a_media_url(self):
        url = invoices_module.generate_invoice_pdf(
            transaction_id="TXN-2026-TESTCASE01",
            user=_FakeUser(),
            plan=_FakePlan(),
            amount=9.99,
            status="paid",
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert url == "/media/invoices/invoice_TXN-2026-TESTCASE01.pdf"

    def test_filename_follows_the_invoice_underscore_transaction_id_convention(self):
        url = invoices_module.generate_invoice_pdf(
            transaction_id="TXN-2026-ABCDEF1234",
            user=_FakeUser(),
            plan=_FakePlan(),
            amount=4.99,
            status="paid",
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
        )
        filename = url.rsplit("/", 1)[-1]
        assert filename == "invoice_TXN-2026-ABCDEF1234.pdf"

    def test_directory_is_created_automatically_if_missing(self, tmp_path, monkeypatch):
        brand_new_dir = tmp_path / "does" / "not" / "exist" / "yet"
        assert not brand_new_dir.exists()
        monkeypatch.setattr(invoices_module, "INVOICES_DIR", brand_new_dir)
        brand_new_dir.mkdir(parents=True)  # mirrors what real import-time .mkdir() does

        invoices_module.generate_invoice_pdf(
            transaction_id="TXN-2026-NEWDIR0001",
            user=_FakeUser(),
            plan=_FakePlan(),
            amount=9.99,
            status="paid",
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert (brand_new_dir / "invoice_TXN-2026-NEWDIR0001.pdf").is_file()


class TestFileExistsAndIsValidPdf:
    def test_file_exists_on_disk(self):
        url = invoices_module.generate_invoice_pdf(
            transaction_id="TXN-2026-EXISTCHECK",
            user=_FakeUser(),
            plan=_FakePlan(),
            amount=9.99,
            status="paid",
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
        )
        filename = url.rsplit("/", 1)[-1]
        assert (invoices_module.INVOICES_DIR / filename).is_file()

    def test_file_is_a_valid_pdf(self):
        url = invoices_module.generate_invoice_pdf(
            transaction_id="TXN-2026-VALIDCHECK",
            user=_FakeUser(),
            plan=_FakePlan(),
            amount=9.99,
            status="paid",
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
        )
        filename = url.rsplit("/", 1)[-1]
        path = invoices_module.INVOICES_DIR / filename

        assert path.read_bytes().startswith(b"%PDF")
        reader = PdfReader(str(path))  # raises if malformed
        assert len(reader.pages) == 1


class TestInvoiceContent:
    def _generate(self, **overrides):
        defaults = dict(
            transaction_id="TXN-2026-CONTENTCHK",
            user=_FakeUser(),
            plan=_FakePlan(),
            amount=9.99,
            status="paid",
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 1, 31, tzinfo=timezone.utc),
        )
        defaults.update(overrides)
        url = invoices_module.generate_invoice_pdf(**defaults)
        filename = url.rsplit("/", 1)[-1]
        return _pdf_text(invoices_module.INVOICES_DIR / filename)

    def test_correct_transaction_id_appears(self):
        text = self._generate(transaction_id="TXN-2026-UNIQUE9999")
        assert "TXN-2026-UNIQUE9999" in text

    def test_correct_plan_appears(self):
        class _Pro:
            name = "Pro"
            price = 19.99

        text = self._generate(plan=_Pro(), amount=19.99)
        assert "Pro" in text

    def test_correct_price_appears(self):
        class _Plan:
            name = "Premium"
            price = 9.99

        text = self._generate(plan=_Plan(), amount=9.99)
        assert "9.99" in text

    def test_user_name_and_email_appear(self):
        class _User:
            username = "alice_wonder"
            email = "alice@example.com"

        text = self._generate(user=_User())
        assert "alice_wonder" in text
        assert "alice@example.com" in text

    def test_invoice_number_is_present_and_distinct_from_transaction_id(self):
        text = self._generate(transaction_id="TXN-2026-DISTINCT01")
        assert "INV-2026-DISTINCT01" in text
        assert "TXN-2026-DISTINCT01" in text

    def test_billing_status_and_dates_appear(self):
        text = self._generate(status="paid")
        assert "Paid" in text
        assert "2026-01-01" in text
        assert "2026-01-31" in text


# ---------------------------------------------------------------------------
# End-to-end: the real /subscriptions/subscribe flow
# ---------------------------------------------------------------------------


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
        yield test_client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient) -> dict:
    client.post("/auth/register", json=USER_A)
    resp = client.post("/auth/login", json={"username": USER_A["username"], "password": USER_A["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _plan_id(client: TestClient, slug: str) -> int:
    plans = client.get("/subscriptions/plans").json()
    return next(p["id"] for p in plans if p["slug"] == slug)


class TestSubscribeGeneratesARealInvoiceMatchingTheDatabase:
    def test_pdf_generated_with_correct_content_and_database_has_the_path(self, client):
        client, session_factory = client
        headers = _register_and_login(client)
        premium_id = _plan_id(client, "premium")

        resp = client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)
        assert resp.status_code == 201
        invoice = resp.json()["invoice"]

        # Database contains the invoice path.
        assert invoice["invoice_path"] is not None
        db = session_factory()
        try:
            row = db.query(models.BillingHistory).filter_by(transaction_id=invoice["transaction_id"]).one()
            assert row.invoice_path == invoice["invoice_path"]
        finally:
            db.close()

        # File exists, is a valid PDF, under media/invoices/.
        assert invoice["invoice_path"].startswith("/media/invoices/invoice_")
        filename = invoice["invoice_path"].rsplit("/", 1)[-1]
        pdf_file = invoices_module.INVOICES_DIR / filename
        assert pdf_file.is_file()

        # Content matches what was actually billed.
        text = _pdf_text(pdf_file)
        assert invoice["transaction_id"] in text
        assert "Premium" in text
        assert "999.00" in text
        assert USER_A["username"] in text
        assert USER_A["email"] in text

    def test_does_not_touch_existing_post_image_storage(self, client):
        """Sub-Task 13 must not change post-image storage behavior --
        confirm subscribing never writes into the posts media directory
        (already redirected to an isolated tmp dir by conftest.py's global
        _isolated_posts_media_dir fixture)."""
        from app.services import media as media_module

        client, _ = client
        headers = _register_and_login(client)
        premium_id = _plan_id(client, "premium")
        client.post("/subscriptions/subscribe", json={"plan_id": premium_id}, headers=headers)

        assert list(media_module.POSTS_MEDIA_DIR.iterdir()) == []
