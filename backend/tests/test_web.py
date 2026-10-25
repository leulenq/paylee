import re
from datetime import date, timedelta
from pathlib import Path

from app import db
from app.security import admin_csrf_token

from .conftest import ADMIN_AUTH, bill_row
from .trust_helpers import complete_payment, operators, verify_fixture_bill
from .trust_helpers import headers as staff_headers

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def valid_form(**overrides):
    form = {
        "first_name": "Maria",
        "email": "maria@example.com",
        "city": "Lexington",
        "state": "KY",
        "category": "utilities",
        "biller_name": "Kentucky Utilities",
        "amount": "$212.50",
        "due_date": (date.today() + timedelta(days=5)).isoformat(),
        "story": "My hours got cut this fall and I fell behind. I have a shutoff notice for next week.",
        "consent": "true",
    }
    form.update(overrides)
    return form


def submit(client, form, file=("bill.png", PNG_BYTES, "image/png")):
    return client.post("/submit", data=form, files={"bill_file": file})


def test_submission_is_stored_for_review(client, settings):
    response = submit(client, valid_form())

    assert response.status_code == 200
    assert "in review" in response.text
    with db.transaction() as conn:
        bill = conn.execute("select * from bills").fetchone()
    assert bill["status"] == "submitted"
    assert bill["claimed_amount_cents"] == 21_250
    assert bill["amount_due_cents"] is None
    assert bill["upload_content_type"] == "image/png"
    assert (bill["city"], bill["state"]) == ("Lexington, KY", "KY")
    from app import documents
    with db.transaction() as conn:
        document = conn.execute("select * from documents where id = %s", (bill["document_id"],)).fetchone()
    assert document["scan_status"] == "pending"
    assert PNG_BYTES not in (Path(settings.upload_dir) / "private" / bill["upload_path"]).read_bytes()
    assert documents.read(document) == PNG_BYTES


def test_submission_rejects_files_that_are_not_bills(client):
    response = submit(client, valid_form(), file=("bill.png", b"<script>alert(1)</script>", "image/png"))

    assert response.status_code == 400
    assert "Upload a PDF, JPG, PNG, or HEIC file." in response.text


def test_submission_requires_consent_and_keeps_typed_values(client):
    form = valid_form()
    del form["consent"]

    response = submit(client, form)

    assert response.status_code == 400
    assert "Confirm the bill is in your name." in response.text
    assert 'value="Kentucky Utilities"' in response.text


def test_submission_escapes_html(client):
    response = submit(client, valid_form(first_name="<b>x</b>", email="nope"))

    assert response.status_code == 400
    assert "<b>x</b>" not in response.text
    assert "&lt;b&gt;x&lt;/b&gt;" in response.text


def test_admin_requires_password(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert client.get("/admin", auth=ADMIN_AUTH).status_code == 200


def test_admin_forms_require_csrf_token(client, make_bill):
    bill_id = make_bill(status="submitted")

    response = client.post(
        f"/admin/bills/{bill_id}/reject", data={"csrf": "forged", "note": "x"}, auth=ADMIN_AUTH
    )

    assert response.status_code == 403
    assert bill_row(bill_id)["status"] == "submitted"


def test_review_approve_payout_flow(client, make_user):
    submit(client, valid_form())
    with db.transaction() as conn:
        bill_id = str(conn.execute("select id from bills").fetchone()["id"])
        verify_fixture_bill(conn, bill_id, 20_000)
        reviewer_headers = staff_headers(operators(conn)["reviewer"])

    page = client.get("/admin", auth=ADMIN_AUTH)
    assert re.search(rf"/admin/uploads/{bill_id}", page.text)
    upload = client.get(f"/admin/uploads/{bill_id}", auth=ADMIN_AUTH)
    assert upload.content == PNG_BYTES
    assert upload.headers["x-content-type-options"] == "nosniff"
    from .trust_helpers import PASSWORD
    assert client.post("/admin/login", data={"username": "reviewer", "password": PASSWORD}, follow_redirects=False).status_code == 303

    unchecked = client.post(
        f"/admin/bills/{bill_id}/approve",
        data={"csrf": admin_csrf_token(), "amount_due": "200.00", "biller_name": "KU", "story": "Edited story"},
        headers=reviewer_headers,
    )
    assert "confirm you checked" in unchecked.text
    assert bill_row(bill_id)["status"] == "submitted"

    client.post(
        f"/admin/bills/{bill_id}/approve",
        data={"csrf": admin_csrf_token(), "amount_due": "200.00", "biller_name": "KU",
              "story": "Edited story", "checked": "true"},
        headers=reviewer_headers,
    )
    approved = bill_row(bill_id)
    assert (approved["status"], approved["amount_due_cents"], approved["story"]) == ("live", 20_000, "Edited story")

    headers, _ = make_user()
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 20_000}, headers=headers)
    assert bill_row(bill_id)["status"] == "funded"

    blocked = client.post(
        f"/admin/bills/{bill_id}/paid", data={"csrf": admin_csrf_token(), "confirmation": "KU-889"}, headers=reviewer_headers
    )
    assert blocked.status_code == 409
    complete_payment(bill_id, "KU-889")
    paid = bill_row(bill_id)
    assert (paid["status"], paid["payout_confirmation"]) == ("paid", "KU-889")


def test_removing_a_bill_refunds_its_givers(client, make_user, make_bill):
    bill_id = make_bill(amount_due_cents=10_000)
    headers, _ = make_user()
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 3_000}, headers=headers)

    response = client.post(
        f"/admin/bills/{bill_id}/remove", data={"csrf": admin_csrf_token(), "note": "fake"}, auth=ADMIN_AUTH
    )

    assert "Refunded 1 contribution" in response.text
    assert bill_row(bill_id)["status"] == "removed"
    with db.transaction() as conn:
        status = conn.execute("select status from contributions").fetchone()["status"]
    assert status == "refunded"


def test_public_bill_page_shows_only_public_details(client, make_bill):
    bill_id = make_bill(amount_due_cents=21_250, amount_raised_cents=8_000)

    page = client.get(f"/b/{bill_id}")

    assert page.status_code == 200
    assert '<meta property="og:title" content="$132.50 left on Maria&#39;s Kentucky Utilities bill">' in page.text
    assert "maria@example.com" not in page.text
    assert f"paylee://bill/{bill_id}" in page.text


def test_public_bill_page_hides_unreviewed_bills(client, make_bill):
    assert client.get(f"/b/{make_bill(status='submitted')}").status_code == 404


def test_paid_off_bill_page_has_no_give_button(client, make_bill):
    bill_id = make_bill(amount_due_cents=5_000, amount_raised_cents=5_000, status="funded")

    page = client.get(f"/b/{bill_id}")

    assert "Fully funded" in page.text
    assert "Chip in on Paylee" not in page.text


def test_intake_form_is_rate_limited(client):
    codes = [submit(client, valid_form()).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]


def test_admin_locks_out_repeated_wrong_passwords(client):
    for _ in range(10):
        assert client.get("/admin", auth=("admin", "guess")).status_code == 401
    assert client.get("/admin", auth=ADMIN_AUTH).status_code == 429


def test_web_fonts_are_served(client):
    response = client.get("/static/fonts/InstrumentSerif-Regular.ttf")
    assert response.status_code == 200
    assert len(response.content) > 10_000


def test_checkout_return_hands_off_to_the_app(client):
    contribution_id = "6f1c1a4e-8a51-4d55-9b0a-1f2d3c4b5a69"

    response = client.get(
        f"/checkout/return?contribution_id={contribution_id}&result=success", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"paylee://checkout?contribution_id={contribution_id}&result=success"
