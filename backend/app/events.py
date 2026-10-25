"""Durable verified provider event inbox. Payloads contain normalized payment fields only."""

import hashlib
import uuid
from dataclasses import asdict

from psycopg.types.json import Jsonb

from . import bills, db, jobs, trust
from .config import get_settings


def receive(conn, provider, event, payload):
    if not event.event_id:
        raise trust.TrustError("Provider event has no stable ID")
    row = conn.execute(
        """insert into provider_events (provider, external_id, payload_sha256, event)
           values (%s, %s, %s, %s) on conflict (provider, external_id) do nothing returning id""",
        (provider, event.event_id, hashlib.sha256(payload).hexdigest(), Jsonb(asdict(event))),
    ).fetchone()
    if not row:
        existing = conn.execute("select id, event from provider_events where provider = %s and external_id = %s",
                                (provider, event.event_id)).fetchone()
        if existing["event"] != asdict(event):
            raise trust.TrustError("Provider event ID was reused with different payment data")
        return existing["id"]
    jobs.enqueue(conn, "provider_event", row["id"])
    return row["id"]


def process(provider, event_id):
    # Resolve external charge references outside the accounting transaction.
    with db.transaction() as conn:
        initial = conn.execute("select provider, event, processed_at from provider_events where id = %s", (event_id,)).fetchone()
    if not initial or initial["processed_at"]:
        return
    if initial["provider"] != provider.name:
        raise trust.TrustError("Worker provider does not match the durable event")
    resolved_payment_ref = initial["event"]["payment_ref"]
    if initial["event"]["kind"] == "dispute" and not resolved_payment_ref and initial["event"]["provider_ref"]:
        resolved_payment_ref = provider.client.v1.charges.retrieve(initial["event"]["provider_ref"]).payment_intent
    with db.transaction() as conn:
        # Serialize competing synchronous/worker deliveries without holding a row before bill locks.
        key = int.from_bytes(uuid.UUID(str(event_id)).bytes[:8], "big", signed=True)
        conn.execute("select pg_advisory_xact_lock(%s)", (key,))
        inbox = conn.execute("select * from provider_events where id = %s", (event_id,)).fetchone()
        if not inbox or inbox["processed_at"]:
            return
        event = inbox["event"]
        if event["kind"] == "dispute":
            _dispute(conn, provider, {**event, "payment_ref": resolved_payment_ref})
        else:
            contribution_id = uuid.UUID(event["contribution_id"])
            contribution = conn.execute("select * from contributions where id = %s", (contribution_id,)).fetchone()
            if not contribution:
                raise trust.TrustError("Provider references an unknown contribution")
            if contribution["provider"] != provider.name:
                raise trust.TrustError("Payment provider does not match the contribution")
            if contribution["provider_ref"] and contribution["provider_ref"] != event["provider_ref"]:
                raise trust.TrustError("Checkout session does not match the contribution")
            if event["kind"] == "paid":
                if event["currency"] != "usd" or not event["payment_ref"] or not event["amount_cents"]:
                    raise trust.TrustError("Invalid collection currency, reference, or amount")
                bills.confirm_payment(conn, contribution_id, payment_ref=event["payment_ref"],
                                      paid_amount_cents=event["amount_cents"], max_live=get_settings().max_live_bills)
            elif event["kind"] == "expired":
                bills.cancel_pending(conn, contribution_id)
        conn.execute("update provider_events set processed_at = now() where id = %s", (event_id,))


def _dispute(conn, provider, event):
    payment_ref = event["payment_ref"]
    contribution = conn.execute("select * from contributions where provider = %s and payment_ref = %s",
                                (provider.name, payment_ref)).fetchone()
    if not contribution:
        raise trust.TrustError("Dispute requires reconciliation to a known collection")
    conn.execute("select id from bills where id = %s for update", (contribution["bill_id"],))
    status = {"won": "won", "lost": "lost"}.get(event["dispute_status"], "open")
    existing = conn.execute("select status from funding_disputes where external_id = %s for update", (event["dispute_id"],)).fetchone()
    if existing and existing["status"] in ("won", "lost"):
        if status == "open" or status == existing["status"]:
            return  # Out-of-order/duplicate events cannot reopen a closed dispute.
        raise trust.TrustError("Conflicting terminal dispute outcomes require investigation")
    conn.execute("""insert into funding_disputes (external_id, contribution_id, status, amount_cents)
                    values (%s, %s, %s, %s) on conflict (external_id) do update
                    set status = excluded.status, updated_at = now()""",
                 (event["dispute_id"], contribution["id"], status, event["amount_cents"]))
    conn.execute("update contributions set funds_reverification_required = true where id = %s", (contribution["id"],))
    trust.audit(conn, None, "funding.disputed", contribution["id"], status=status)
