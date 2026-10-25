import threading

import psycopg
import pytest

from app import bills, db
from app.api import _settle
from app.payments import FakeProvider

from .conftest import bill_row


def contribute(client, headers, bill_id, amount_cents):
    return client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": amount_cents}, headers=headers)


def reserve(bill_id, user_id, amount_cents, settings):
    with db.transaction() as conn:
        return bills.reserve_contribution(
            conn, bill_id=bill_id, user_id=user_id, amount_cents=amount_cents, provider_name="fake", settings=settings
        )


def test_contribution_counts_toward_bill(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=10_000)

    response = contribute(client, headers, bill_id, 2_500)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["contribution"]["status"] == "succeeded"
    assert body["closed_bill"] is False
    assert body["checkout_url"] is None
    assert body["bill"]["amount_raised_cents"] == 2_500


def test_contribution_over_remaining_balance_is_rejected(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=10_000, amount_raised_cents=9_000)

    response = contribute(client, headers, bill_id, 1_500)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "over_balance",
        "message": "Only $10.00 is left on this bill.",
        "available_cents": 1_000,
    }
    assert bill_row(bill_id)["amount_raised_cents"] == 9_000


def test_minimum_applies_unless_amount_closes_the_bill(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=10_000, amount_raised_cents=9_700)

    too_small = contribute(client, headers, bill_id, 100)
    exact_close = contribute(client, headers, bill_id, 300)

    assert too_small.status_code == 409
    assert too_small.json()["detail"]["code"] == "below_minimum"
    assert exact_close.status_code == 201
    assert exact_close.json()["closed_bill"] is True


def test_closing_a_bill_funds_it_credits_the_closer_and_promotes_the_waitlist(
    client, make_user, make_bill, settings, monkeypatch
):
    monkeypatch.setattr(settings, "max_live_bills", 1)
    headers, user = make_user()
    bill_id = make_bill(amount_due_cents=5_000)
    waiting_id = make_bill(status="queued")

    response = contribute(client, headers, bill_id, 5_000)

    assert response.json()["closed_bill"] is True
    funded = bill_row(bill_id)
    assert funded["status"] == "funded"
    assert str(funded["closer_user_id"]) == user["id"]
    assert bill_row(waiting_id)["status"] == "live"


def test_closed_bill_takes_no_more_money(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=5_000)
    contribute(client, headers, bill_id, 5_000)

    response = contribute(client, headers, bill_id, 500)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_open"


def test_concurrent_givers_cannot_overshoot_the_balance(make_user, make_bill, settings):
    """Two givers try to cover the same last $50 at the same instant. Exactly one may succeed."""
    bill_id = make_bill(amount_due_cents=5_000)
    user_ids = [make_user()[1]["id"] for _ in range(2)]
    start = threading.Barrier(len(user_ids))
    outcomes: list[str] = []

    def give(user_id):
        start.wait()
        try:
            reserve(bill_id, user_id, 5_000, settings)
            outcomes.append("reserved")
        except bills.ContributionRejected as rejection:
            outcomes.append(rejection.code)

    threads = [threading.Thread(target=give, args=(uid,)) for uid in user_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["fully_reserved", "reserved"]


def test_pending_holds_reduce_what_others_can_give(client, make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=10_000)
    _, first = make_user()
    reserve(bill_id, first["id"], 7_000, settings)
    headers, _ = make_user()

    response = contribute(client, headers, bill_id, 5_000)

    assert response.status_code == 409
    assert response.json()["detail"]["available_cents"] == 3_000


def test_expired_holds_release_the_balance(client, make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=10_000)
    _, first = make_user()
    held = reserve(bill_id, first["id"], 7_000, settings)
    with db.transaction() as conn:
        conn.execute("update contributions set expires_at = now() - interval '1 minute' where id = %s", (held["id"],))
    headers, _ = make_user()

    assert contribute(client, headers, bill_id, 10_000).status_code == 201


def test_confirming_the_same_payment_twice_counts_it_once(make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=10_000)
    _, user = make_user()
    held = reserve(bill_id, user["id"], 2_000, settings)

    first = _settle(FakeProvider(), held["id"], payment_ref="pi_1", paid_amount_cents=2_000)
    second = _settle(FakeProvider(), held["id"], payment_ref="pi_1", paid_amount_cents=2_000)

    assert (first.outcome, second.outcome) == ("succeeded", "already_processed")
    assert bill_row(bill_id)["amount_raised_cents"] == 2_000


def test_late_payment_for_a_filled_bill_is_refunded_not_applied(client, make_user, make_bill, settings):
    """A giver's hold lapses, someone else closes the bill, then the first giver's payment lands."""
    bill_id = make_bill(amount_due_cents=5_000)
    _, slow = make_user()
    held = reserve(bill_id, slow["id"], 5_000, settings)
    with db.transaction() as conn:
        conn.execute("update contributions set expires_at = now() - interval '1 minute' where id = %s", (held["id"],))
    headers, _ = make_user()
    contribute(client, headers, bill_id, 5_000)

    refunded = []

    class RecordingProvider(FakeProvider):
        def refund(self, payment_ref):
            refunded.append(payment_ref)

    outcome = _settle(RecordingProvider(), held["id"], payment_ref="pi_late", paid_amount_cents=5_000)

    assert outcome.outcome == "refund"
    assert refunded == ["pi_late"]
    assert bill_row(bill_id)["amount_raised_cents"] == 5_000
    with db.transaction() as conn:
        status = conn.execute("select status from contributions where id = %s", (held["id"],)).fetchone()["status"]
    assert status == "refunded"


def test_paid_amount_that_differs_from_the_hold_is_refunded(make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=10_000)
    _, user = make_user()
    held = reserve(bill_id, user["id"], 2_000, settings)

    outcome = _settle(FakeProvider(), held["id"], payment_ref="pi_odd", paid_amount_cents=9_999)

    assert outcome.outcome == "refund"
    assert bill_row(bill_id)["amount_raised_cents"] == 0


def test_failed_refund_stays_visible_for_retry(make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=10_000)
    _, user = make_user()
    held = reserve(bill_id, user["id"], 2_000, settings)

    class BrokenProvider(FakeProvider):
        def refund(self, payment_ref):
            raise RuntimeError("provider down")

    _settle(BrokenProvider(), held["id"], payment_ref="pi_x", paid_amount_cents=1)

    with db.transaction() as conn:
        status = conn.execute("select status from contributions where id = %s", (held["id"],)).fetchone()["status"]
    assert status == "refund_pending"


def test_contribution_rate_limit(client, make_user, make_bill, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_contributions_per_hour", 2)
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=100_000)

    codes = [contribute(client, headers, bill_id, 500).status_code for _ in range(3)]

    assert codes == [201, 201, 429]


def test_failed_checkout_start_releases_the_hold(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=5_000)

    class DownProvider(FakeProvider):
        def start_checkout(self, **kwargs):
            raise RuntimeError("provider down")

    original = client.app.state.payments
    client.app.state.payments = DownProvider()
    try:
        failed = contribute(client, headers, bill_id, 5_000)
    finally:
        client.app.state.payments = original

    assert failed.status_code == 502
    assert contribute(client, headers, bill_id, 5_000).status_code == 201


def test_canceling_checkout_releases_the_hold(client, make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=5_000)
    headers, user = make_user()
    held = reserve(bill_id, user["id"], 5_000, settings)
    other_headers, _ = make_user()
    assert contribute(client, other_headers, bill_id, 5_000).status_code == 409

    assert client.post(f"/api/contributions/{held['id']}/cancel", headers=headers).status_code == 204

    assert contribute(client, other_headers, bill_id, 5_000).status_code == 201


def test_only_the_giver_can_cancel_their_checkout(client, make_user, make_bill, settings):
    bill_id = make_bill()
    _, owner = make_user()
    held = reserve(bill_id, owner["id"], 1_000, settings)
    stranger_headers, _ = make_user()

    assert client.post(f"/api/contributions/{held['id']}/cancel", headers=stranger_headers).status_code == 404


def test_database_refuses_an_overpaid_bill(make_bill):
    bill_id = make_bill(amount_due_cents=5_000)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction() as conn:
            conn.execute("update bills set amount_raised_cents = 5001 where id = %s", (bill_id,))


def test_contributing_requires_sign_in(client, make_bill):
    bill_id = make_bill()
    assert client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 500}).status_code == 401
