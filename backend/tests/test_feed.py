from datetime import date, timedelta

from app import bills, db

from .conftest import bill_row
from .trust_helpers import operators

PRIVATE_FIELDS = {"requester_email", "upload_path", "upload_content_type", "review_note", "claimed_amount_cents",
                  "closer_user_id", "payout_confirmation", "times_shown"}


def feed_ids(client, headers):
    response = client.get("/api/feed", headers=headers)
    assert response.status_code == 200, response.text
    return [b["id"] for b in response.json()["bills"]]


def test_feed_shows_only_live_bills(client, make_user, make_bill, settings, monkeypatch):
    # A cap of 1 keeps the queued bill waiting; otherwise the feed would promote it into a free slot.
    monkeypatch.setattr(settings, "max_live_bills", 1)
    headers, _ = make_user()
    live = make_bill()
    for status in ("submitted", "queued", "funded", "paid", "rejected", "removed"):
        make_bill(status=status)

    assert feed_ids(client, headers) == [live]


def test_feed_never_exposes_private_fields(client, make_user, make_bill):
    headers, _ = make_user()
    make_bill()

    bill = client.get("/api/feed", headers=headers).json()["bills"][0]

    assert PRIVATE_FIELDS.isdisjoint(bill)
    assert bill["requester_first_name"] == "Maria"


def test_passed_bills_rest_out_of_the_feed(client, make_user, make_bill):
    headers, _ = make_user()
    other_headers, _ = make_user()
    bill_id = make_bill()

    assert client.post(f"/api/bills/{bill_id}/pass", headers=headers).status_code == 204

    assert feed_ids(client, headers) == []
    assert feed_ids(client, other_headers) == [bill_id]


def test_pass_cooldown_ends(client, make_user, make_bill):
    headers, user = make_user()
    bill_id = make_bill()
    client.post(f"/api/bills/{bill_id}/pass", headers=headers)
    with db.transaction() as conn:
        conn.execute("update passes set created_at = now() - interval '4 days' where user_id = %s", (user["id"],))

    assert feed_ids(client, headers) == [bill_id]


def test_bills_you_already_gave_to_leave_your_feed(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=10_000)
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 1_000}, headers=headers)

    assert feed_ids(client, headers) == []


def test_least_shown_bills_come_first(client, make_user, make_bill):
    headers, _ = make_user()
    often_shown = make_bill(due_date=date.today() + timedelta(days=1))
    rarely_shown = make_bill(due_date=date.today() + timedelta(days=20))
    with db.transaction() as conn:
        conn.execute("update bills set times_shown = 50 where id = %s", (often_shown,))

    assert feed_ids(client, headers) == [rarely_shown, often_shown]
    assert bill_row(rarely_shown)["times_shown"] == 1


def test_overdue_bills_expire_after_the_grace_period(client, make_user, make_bill, settings):
    headers, _ = make_user()
    stale = make_bill(due_date=date.today() - timedelta(days=settings.expiry_grace_days + 1))
    recent = make_bill(due_date=date.today() - timedelta(days=1))

    assert feed_ids(client, headers) == [recent]
    assert bill_row(stale)["status"] == "expired"


def test_live_cap_holds_approved_bills_in_a_queue(make_bill, settings):
    first = make_bill(status="submitted", amount_due_cents=5_000)
    second = make_bill(status="submitted", amount_due_cents=5_000)
    with db.transaction() as conn:
        for bill_id in (first, second):
            bills.approve(conn, bill_id, amount_due_cents=5_000, biller_name="KU", story="Story text", max_live=1, actor=operators(conn)["reviewer"])

    assert bill_row(first)["status"] == "live"
    assert bill_row(second)["status"] == "queued"


def test_reporting_a_bill(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill()

    first = client.post(f"/api/bills/{bill_id}/report", json={"reason": "personal_info"}, headers=headers)
    repeat = client.post(f"/api/bills/{bill_id}/report", json={"reason": "other"}, headers=headers)
    invalid = client.post(f"/api/bills/{bill_id}/report", json={"reason": "ugly"}, headers=headers)

    assert (first.status_code, repeat.status_code, invalid.status_code) == (204, 204, 422)
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from reports").fetchone()["n"] == 1


def test_unreviewed_bills_are_not_viewable(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(status="submitted")

    assert client.get(f"/api/bills/{bill_id}", headers=headers).status_code == 404
    assert client.post(f"/api/bills/{bill_id}/pass", headers=headers).status_code == 404
