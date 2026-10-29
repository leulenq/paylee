"""Requester-owned projection and lifecycle operations. Never expose donor or review data."""

from fastapi import HTTPException

from . import bills, eligibility, notify, trust, vault
from .config import get_settings
from .money import format_dollars

WITHDRAWABLE = ("submitted", "in_review", "needs_info", "queued", "live")
UPDATABLE = (*WITHDRAWABLE, "funded", "expired")

# Explicit projection prevents private staff notes, donor identities, and feed metrics from leaking.
PROJECTION = """
    select b.id, b.status, b.category, b.biller_name, b.claimed_amount_cents,
           b.amount_due_cents as verified_amount_cents, b.amount_raised_cents, b.due_date, b.story,
           b.requester_first_name as first_name,
           case when b.state is not null and right(b.city, 4) = ', ' || b.state
                then left(b.city, length(b.city) - 4) else b.city end as city,
           b.state, b.created_at as submitted_at, b.live_at, b.funded_at, b.paid_at,
           v.account_last4 as account_ending, b.requester_status_note as status_note,
           (b.status = any(%(withdrawable)s::text[]) and not exists (
                select 1 from payment_orders p where p.bill_id = b.id
                  and p.status not in ('prepared', 'canceled', 'returned')
           )) as can_withdraw,
           (b.status = any(%(updatable)s::text[])) as can_update_balance
    from bills b left join bill_verifications v on v.id = b.verification_id
    where b.requester_user_id = %(user)s
"""


def get(conn, user_id, bill_id=None):
    params = {"user": user_id, "withdrawable": list(WITHDRAWABLE), "updatable": list(UPDATABLE)}
    if bill_id is None:
        return conn.execute(PROJECTION + " order by b.created_at desc, b.id", params).fetchall()
    params["bill"] = bill_id
    row = conn.execute(PROJECTION + " and b.id = %(bill)s", params).fetchone()
    if row is None:
        raise HTTPException(404, "Request not found")
    return row


def _owned(conn, user_id, bill_id):
    row = conn.execute("select * from bills where id = %s and requester_user_id = %s for update",
                       (bill_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Request not found")
    return row


def withdraw(conn, user_id, bill_id):
    bill = _owned(conn, user_id, bill_id)
    if bill["status"] == "withdrawn":
        return get(conn, user_id, bill_id)
    if bill["status"] not in WITHDRAWABLE:
        raise trust.TrustError("This request can no longer be withdrawn; contact Paylee about its payment status")
    eligibility.on_withdraw(conn, bill_id)
    bills.remove(conn, bill_id, "Requester withdrew their own request", get_settings().max_live_bills, withdrawn=True)
    conn.execute("update bills set requester_status_note = %s where id = %s",
                 ("You withdrew this bill. Any eligible gifts are being returned to their original payment methods.", bill_id))
    trust.audit(conn, None, "request.withdrawn", bill_id, requester_user_id=str(user_id))
    return get(conn, user_id, bill_id)


def report_balance(conn, user_id, bill_id, amount_cents, note):
    bill = _owned(conn, user_id, bill_id)
    if bill["status"] not in UPDATABLE:
        raise trust.TrustError("This request can no longer accept balance updates; contact Paylee")
    current = bill["amount_due_cents"] or bill["claimed_amount_cents"]
    if bill["verification_paused"] and bill["reported_balance_cents"] is not None:
        current = min(current, bill["reported_balance_cents"])
    if amount_cents > current:
        raise trust.TrustError("Report a lower outstanding balance; additional charges need a separately reviewed bill")
    if amount_cents == current:
        return get(conn, user_id, bill_id)
    report = conn.execute("""insert into balance_reports (bill_id, requester_user_id, amount_cents, note_ciphertext)
                              values (%s, %s, %s, %s) returning id, created_at""",
                          (bill_id, user_id, amount_cents, vault.encrypt(note))).fetchone()
    conn.execute("""update bills set status = 'in_review', verification_paused = true,
                    reported_balance_cents = %s, balance_reported_at = %s, requester_status_note = %s where id = %s""",
                 (amount_cents, report["created_at"],
                  f"You reported {format_dollars(amount_cents)} outstanding. New gifts and payment release are paused while we confirm it with the company.", bill_id))
    # Never reissue an uncertain external payment. Only preparations are safe to cancel automatically.
    canceled = conn.execute("update payment_orders set status = 'canceled' where bill_id = %s and status = 'prepared' returning id",
                            (bill_id,)).fetchall()
    for order in canceled:
        trust.audit(conn, None, "payment.canceled_for_balance_review", order["id"], report_id=str(report["id"]))
    conn.execute("update contributions set status = 'canceled' where bill_id = %s and status = 'pending'", (bill_id,))
    trust.audit(conn, None, "request.balance_reported", bill_id, requester_user_id=str(user_id),
                report_id=str(report["id"]), amount_cents=amount_cents)
    notify.bill_event(conn, bill_id, "balance_reported")
    bills.promote_queued(conn, get_settings().max_live_bills)
    return get(conn, user_id, bill_id)
