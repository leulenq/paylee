
import pytest

from app import db, events, trust
from app.payments import PaymentEvent, StripeProvider

from .test_stripe_webhook import hold, session_event, signed
from .trust_helpers import operators, proof


@pytest.fixture(name="stripe_provider")
def _stripe_provider(client):
    provider = StripeProvider("sk_test_unused", "whsec_test_secret", "http://testserver")
    provider.refunded = []
    provider.refund = provider.refunded.append
    original = client.app.state.payments
    client.app.state.payments = provider
    yield provider
    client.app.state.payments = original


def test_failed_processing_is_durable_and_replay_recovers(client, stripe_provider, make_user, make_bill, settings, monkeypatch):
    bill_id = make_bill()
    _, user = make_user()
    contribution = hold(bill_id, user["id"], 2500, settings)
    body, headers = signed(session_event("checkout.session.completed", contribution["id"],
                                          payment_status="paid", payment_intent="pi_durable", amount_total=2500))
    actual = events.process
    monkeypatch.setattr(events, "process", lambda *a: (_ for _ in ()).throw(ConnectionError("Temporary DB failure")))
    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 200
    with db.transaction() as conn:
        inbox = conn.execute("select * from provider_events").fetchone()
        assert inbox["processed_at"] is None
        assert conn.execute("select status from work_jobs where kind = 'provider_event'").fetchone()["status"] == "pending"
        assert conn.execute("select amount_raised_cents from bills where id = %s", (bill_id,)).fetchone()["amount_raised_cents"] == 0
    monkeypatch.setattr(events, "process", actual)
    events.process(stripe_provider, inbox["id"])
    events.process(stripe_provider, inbox["id"])
    with db.transaction() as conn:
        assert conn.execute("select amount_raised_cents from bills where id = %s", (bill_id,)).fetchone()["amount_raised_cents"] == 2500
        assert conn.execute("select count(*) as n from ledger_journals").fetchone()["n"] == 1


def test_reused_event_id_cannot_change_the_amount(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill()
    _, user = make_user()
    contribution = hold(bill_id, user["id"], 2500, settings)
    payload = session_event("checkout.session.completed", contribution["id"],
                            payment_status="paid", payment_intent="pi_reuse", amount_total=2500)
    body, headers = signed(payload)
    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 200
    payload["data"]["object"]["amount_total"] = 9999
    body, headers = signed(payload)
    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 409
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from provider_events").fetchone()["n"] == 1


def test_wrong_currency_is_held_for_reconciliation(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill()
    _, user = make_user()
    contribution = hold(bill_id, user["id"], 2500, settings)
    body, headers = signed(session_event("checkout.session.completed", contribution["id"], currency="eur",
                                          payment_status="paid", payment_intent="pi_eur", amount_total=2500))
    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 200
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from ledger_journals").fetchone()["n"] == 0
        assert conn.execute("select processed_at from provider_events").fetchone()["processed_at"] is None


def test_async_success_uses_the_same_collection_path(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill()
    _, user = make_user()
    contribution = hold(bill_id, user["id"], 2500, settings)
    body, headers = signed(session_event("checkout.session.async_payment_succeeded", contribution["id"],
                                          payment_status="paid", payment_intent="pi_async", amount_total=2500))
    assert client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 200
    with db.transaction() as conn:
        assert conn.execute("select status from contributions where id = %s", (contribution["id"],)).fetchone()["status"] == "succeeded"


def test_dispute_blocks_disbursement_and_out_of_order_creation_cannot_reopen_it(client, stripe_provider, make_user, make_bill, settings):
    bill_id = make_bill(amount_due_cents=5000)
    _, user = make_user()
    contribution = hold(bill_id, user["id"], 5000, settings)
    body, headers = signed(session_event("checkout.session.completed", contribution["id"],
                                          payment_status="paid", payment_intent="pi_dispute", amount_total=5000))
    client.post("/api/stripe/webhook", content=body, headers=headers)
    with db.transaction() as conn:
        ops = operators(conn)
        trust.reconcile_funding(conn, ops["treasury"], contribution["id"], proof(conn, "funding", contribution["id"], 5000))
        event = PaymentEvent(kind="dispute", contribution_id="", event_id="evt_dispute", payment_ref="pi_dispute",
                             amount_cents=5000, dispute_id="dp_test", dispute_status="open")
        event_id = events.receive(conn, "stripe", event, b"signed-event")
    events.process(stripe_provider, event_id)
    with db.transaction() as conn:
        with pytest.raises(trust.TrustError, match="Disputed funding"):
            trust.prepare_payment(conn, ops["reviewer"], bill_id)
        won = PaymentEvent(kind="dispute", contribution_id="", event_id="evt_dispute_won", payment_ref="pi_dispute",
                           amount_cents=5000, dispute_id="dp_test", dispute_status="won")
        won_id = events.receive(conn, "stripe", won, b"signed-won")
    events.process(stripe_provider, won_id)
    with db.transaction() as conn:
        old = PaymentEvent(kind="dispute", contribution_id="", event_id="evt_dispute_late", payment_ref="pi_dispute",
                           amount_cents=5000, dispute_id="dp_test", dispute_status="open")
        old_id = events.receive(conn, "stripe", old, b"signed-late")
    events.process(stripe_provider, old_id)
    with db.transaction() as conn:
        assert conn.execute("select status from funding_disputes").fetchone()["status"] == "won"
        with pytest.raises(trust.TrustError, match="Reconcile"):
            trust.prepare_payment(conn, ops["reviewer"], bill_id)
        trust.reconcile_funding(conn, ops["treasury"], contribution["id"], proof(conn, "funding", contribution["id"], 5000))
        assert trust.prepare_payment(conn, ops["reviewer"], bill_id)["status"] == "prepared"


def test_refund_recovers_existing_provider_request_without_creating_another():
    from types import SimpleNamespace
    provider = StripeProvider("sk_test_unused", "whsec_unused", "http://test")
    calls = []
    existing = SimpleNamespace(id="re_existing", status="succeeded", amount=5000)
    service = SimpleNamespace(list=lambda *a, **k: SimpleNamespace(data=[existing]),
                              create=lambda *a, **k: calls.append("created"))
    provider.client = SimpleNamespace(v1=SimpleNamespace(refunds=service))
    result = provider.refund("pi_original")
    assert result.reference == "re_existing" and calls == []
