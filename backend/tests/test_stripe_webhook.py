import hashlib
import hmac
import json
import time

import pytest

from app import bills, db
from app.payments import StripeProvider

from .conftest import bill_row

WEBHOOK_SECRET = "whsec_test_secret"


@pytest.fixture
def stripe_provider(client):
    provider = StripeProvider("sk_test_unused", WEBHOOK_SECRET, "http://testserver")
    provider.refunded = []
    provider.refund = provider.refunded.append
    original = client.app.state.payments
    client.app.state.payments = provider
    yield provider
    client.app.state.payments = original


def signed(payload: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={timestamp},v1={signature}", "content-type": "application/json"}


def session_event(event_type: str, contribution_id, **session_fields) -> dict:
    session = {"id": "cs_test_1", "object": "checkout.session", "client_reference_id": str(contribution_id), "currency": "usd"}
    session.update(session_fields)
    return {"id": "evt_1", "object": "event", "type": event_type, "data": {"object": session}}


def hold(bill_id, user_id, amount_cents, settings):
    with db.transaction() as conn:
        return bills.reserve_contribution(
            conn, bill_id=bill_id, user_id=user_id, amount_cents=amount_cents,
            provider_name="stripe", settings=settings,
        )


def contribution_status(contribution_id) -> str:
    with db.transaction() as conn:
        return conn.execute(
            "select status from contributions where id = %s", (contribution_id,)
        ).fetchone()["status"]


def test_completed_checkout_applies_the_payment_once(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=10_000)
    _, user = make_user()
    held = hold(bill_id, user["id"], 2_500, settings)
    body, headers = signed(session_event(
        "checkout.session.completed", held["id"], payment_status="paid", payment_intent="pi_1", amount_total=2_500,
    ))

    first = client.post("/api/stripe/webhook", content=body, headers=headers)
    replay = client.post("/api/stripe/webhook", content=body, headers=headers)

    assert (first.status_code, replay.status_code) == (200, 200)
    assert contribution_status(held["id"]) == "succeeded"
    assert bill_row(bill_id)["amount_raised_cents"] == 2_500


def test_unsigned_webhooks_are_rejected(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill()
    _, user = make_user()
    held = hold(bill_id, user["id"], 2_500, settings)
    body, headers = signed(
        session_event("checkout.session.completed", held["id"], payment_status="paid", amount_total=2_500),
        secret="whsec_attacker",
    )

    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 400
    assert contribution_status(held["id"]) == "pending"


def test_expired_checkout_releases_the_hold(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill()
    _, user = make_user()
    held = hold(bill_id, user["id"], 2_500, settings)
    body, headers = signed(session_event("checkout.session.expired", held["id"]))

    client.post("/api/stripe/webhook", content=body, headers=headers)

    assert contribution_status(held["id"]) == "canceled"


def test_mismatched_amount_is_refunded(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill()
    _, user = make_user()
    held = hold(bill_id, user["id"], 2_500, settings)
    body, headers = signed(session_event(
        "checkout.session.completed", held["id"], payment_status="paid", payment_intent="pi_9", amount_total=100,
    ))

    client.post("/api/stripe/webhook", content=body, headers=headers)

    assert stripe_provider.refunded == ["pi_9"]
    assert contribution_status(held["id"]) == "refunded"
    assert bill_row(bill_id)["amount_raised_cents"] == 0


def test_webhook_is_off_with_the_fake_provider(client):
    body, headers = signed({"type": "checkout.session.completed"})
    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 404
