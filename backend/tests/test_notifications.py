from app import bills, db, notify
from app.security import admin_csrf_token

from .conftest import ADMIN_AUTH, bill_row, queued_emails
from .test_web import submit, valid_form
from .trust_helpers import complete_payment, operators, verify_fixture_bill
from .trust_helpers import headers as staff_headers


def subjects() -> list[str]:
    return [email["subject"] for email in queued_emails()]


def test_requester_hears_about_every_step(client, make_user, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_live_bills", 5)
    submit(client, valid_form())
    with db.transaction() as conn:
        bill_id = str(conn.execute("select id from bills").fetchone()["id"])
        verify_fixture_bill(conn, bill_id, 5_000)
        reviewer_headers = staff_headers(operators(conn)["reviewer"])

    client.post(
        f"/admin/bills/{bill_id}/approve",
        data={"csrf": admin_csrf_token(), "amount_due": "50.00", "biller_name": "Kentucky Utilities",
              "story": "Edited story", "checked": "true"},
        headers=reviewer_headers,
    )
    headers, _ = make_user()
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 5_000}, headers=headers)
    complete_payment(bill_id, "KU-42")

    assert subjects() == [
        "We received your bill",
        "Your bill is live on Paylee",
        "Your bill is fully covered",
        "We paid Kentucky Utilities",
    ]
    paid = queued_emails()[-1]
    assert paid["to_address"] == "maria@example.com"
    assert paid["body"].startswith("Hi Maria,")
    assert "$50.00" in paid["body"] and "KU-42" in paid["body"]


def test_waitlisted_bills_get_approved_then_live_emails(make_bill, settings):
    blocking = make_bill()
    waiting = make_bill(status="submitted", amount_due_cents=5_000)
    with db.transaction() as conn:
        bills.approve(conn, waiting, amount_due_cents=5_000, biller_name="KU", story="Story", max_live=1, actor=operators(conn)["reviewer"])
    assert subjects() == ["Your bill was approved"]

    with db.transaction() as conn:
        bills.remove(conn, blocking, "test", max_live=1)

    assert subjects() == ["Your bill was approved", "Your bill was taken down", "Your bill is live on Paylee"]
    assert bill_row(waiting)["status"] == "live"


def test_rejection_email(client, make_bill):
    bill_id = make_bill(status="submitted")

    client.post(f"/admin/bills/{bill_id}/reject", data={"csrf": admin_csrf_token(), "note": "blurry"}, auth=ADMIN_AUTH)

    email = queued_emails()[0]
    assert email["subject"] == "We couldn't post your bill"
    assert "blurry" not in email["body"]  # review notes are internal


def test_outbox_sends_and_retries():
    with db.transaction() as conn:
        conn.execute("insert into emails (to_address, subject, body) values ('a@example.com', 'One', 'Body')")

    class FlakyMailer:
        def __init__(self):
            self.sent, self.fail = [], True

        def send(self, to, subject, body):
            if self.fail:
                raise OSError("SMTP down")
            self.sent.append((to, subject))

    mailer = FlakyMailer()
    assert notify.deliver_pending(mailer) == 0
    assert queued_emails()[0]["sent_at"] is None

    mailer.fail = False
    assert notify.deliver_pending(mailer) == 1
    assert mailer.sent == [("a@example.com", "One")]
    assert notify.deliver_pending(mailer) == 0


def test_giver_activity_feed(client, make_user, make_bill):
    headers, _ = make_user()
    other_headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=10_000)
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 4_000}, headers=headers)
    assert client.get("/api/me/activity", headers=headers).json() == {"events": [], "unread_count": 0}

    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 6_000}, headers=other_headers)
    complete_payment(bill_id, "CONF-1")

    activity = client.get("/api/me/activity", headers=headers).json()
    assert [(e["kind"], e["closed_by_me"], e["unread"]) for e in activity["events"]] == [
        ("paid", False, True), ("funded", False, True),
    ]
    assert activity["unread_count"] == 2

    assert client.post("/api/me/activity/seen", headers=headers).status_code == 204
    assert client.get("/api/me/activity", headers=headers).json()["unread_count"] == 0
    closer = client.get("/api/me/activity", headers=other_headers).json()["events"]
    assert all(e["closed_by_me"] for e in closer)
