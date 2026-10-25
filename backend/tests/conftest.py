import os
import tempfile
import uuid
from datetime import date, timedelta

# Point the app at the test database before anything imports its settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://swipebills:swipebills@127.0.0.1:5433/swipebills_test"
)
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="paylee-uploads-")
os.environ["PAYMENT_PROVIDER"] = "fake"
os.environ["ALLOW_DEV_LOGIN"] = "true"
os.environ["ADMIN_PASSWORD"] = "test-admin"
os.environ["ENVIRONMENT"] = "test"
os.environ["EMAIL_PROVIDER"] = "console"
# Tests deliver the outbox explicitly; a background thread would race them.
os.environ["EMAIL_WORKER_ENABLED"] = "false"
os.environ["MALWARE_SCANNER"] = "development"
os.environ["STORAGE_PROVIDER"] = "local"
os.environ["OCR_PROVIDER"] = "manual"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, ratelimit  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

from .trust_helpers import verify_fixture_bill  # noqa: E402

ADMIN_AUTH = ("admin", "test-admin")


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(client):
    with db.transaction() as conn:
        conn.execute("truncate work_jobs, provider_events, pushes, device_tokens, emails, reports, passes, contributions, bills, users, documents, staff cascade")
    client.cookies.clear()
    ratelimit.reset_all()
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def make_user(client):
    def _make_user() -> tuple[dict, dict]:
        response = client.post("/api/auth/dev", json={"device_id": f"test-{uuid.uuid4()}"})
        assert response.status_code == 200, response.text
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}"}, body["user"]

    return _make_user


@pytest.fixture
def make_bill():
    def _make_bill(
        *,
        amount_due_cents: int = 10_000,
        amount_raised_cents: int = 0,
        status: str = "live",
        due_date: date | None = None,
        biller_name: str = "Kentucky Utilities",
        category: str = "utilities",
        state: str = "KY",
    ) -> str:
        with db.transaction() as conn:
            row = conn.execute(
                """
                insert into bills (status, category, biller_name, requester_first_name, requester_email,
                                   city, state, story, claimed_amount_cents, amount_due_cents, amount_raised_cents,
                                   due_date, upload_path, upload_content_type, live_at)
                values (%s, %s, %s, 'Maria', 'maria@example.com', 'Lexington, ' || %s, %s,
                        'My hours got cut and I fell behind on the electric bill this month.',
                        %s, %s, %s, %s, 'missing.png', 'image/png',
                        case when %s = 'live' then now() end)
                returning id
                """,
                (status, category, biller_name, state, state, amount_due_cents, amount_due_cents, amount_raised_cents,
                 due_date or date.today() + timedelta(days=7), status),
            ).fetchone()
            verify_fixture_bill(conn, row["id"], amount_due_cents)
        return str(row["id"])

    return _make_bill


def bill_row(bill_id: str) -> dict:
    with db.transaction() as conn:
        return conn.execute("select * from bills where id = %s", (bill_id,)).fetchone()


def queued_emails() -> list[dict]:
    with db.transaction() as conn:
        return conn.execute("select to_address, subject, body, sent_at from emails order by created_at").fetchall()
