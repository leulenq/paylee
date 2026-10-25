"""Bill lifecycle and the money rules. Every function takes a connection and runs inside the caller's transaction."""

from dataclasses import dataclass
from datetime import date, datetime, timezone

import psycopg

from . import jobs, ledger, notify, staff, trust
from .config import Settings
from .money import format_dollars

CATEGORIES = ("rent", "utilities", "medical", "phone", "internet", "other")

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Status tiers by bills helped all the way to paid off. Counting bills, not dollars, keeps every tier
# reachable by small givers.
TIERS = (("Member", 0), ("Gold", 3), ("Platinum", 10), ("Black", 25))
REPORT_REASONS = ("fake_or_altered", "not_their_bill", "offensive", "personal_info", "other")

# Columns safe to show givers. Requester email, uploads, and review notes never leave the server.
PUBLIC_COLUMNS = """
    b.id, b.category, b.biller_name, b.requester_first_name, b.city, b.story,
    b.amount_due_cents, b.amount_raised_cents, b.due_date, b.status,
    (select v.created_at from bill_verifications v where v.id = b.verification_id) as verified_at,
    (select v.expires_at from bill_verifications v where v.id = b.verification_id) as verification_expires_at
"""

# Advisory lock id that serializes queue promotion, so two promoters can't both fill the last live slot.
QUEUE_LOCK_KEY = 7_201_001

# Same admission rule for queue promotion, feed visibility, and public active bill pages.
ELIGIBLE_SQL = """exists (
    select 1 from bill_verifications v join billers br on br.id = v.biller_id
    where v.id = b.verification_id and v.expires_at > now() and br.status = 'approved'
      and br.active_destination_id = v.destination_id
      and exists (select 1 from review_decisions r where r.verification_id = v.id and r.decision = 'approved')
)"""


class ContributionRejected(Exception):
    def __init__(self, code: str, message: str, available_cents: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.available_cents = available_cents


@dataclass
class Confirmation:
    outcome: str  # "succeeded", "already_processed", or "refund"
    closed_bill: bool = False


# --- Intake and review -------------------------------------------------------


def create_submission(
    conn: psycopg.Connection,
    *,
    first_name: str,
    email: str,
    city: str,
    state: str,
    category: str,
    biller_name: str,
    claimed_amount_cents: int,
    due_date: date,
    story: str,
    upload_path: str,
    upload_content_type: str,
) -> dict:
    return conn.execute(
        """
        insert into bills (requester_first_name, requester_email, city, state, category, biller_name,
                           claimed_amount_cents, due_date, story, upload_path, upload_content_type)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (first_name, email, f"{city}, {state}", state, category, biller_name, claimed_amount_cents, due_date,
         story, upload_path, upload_content_type),
    ).fetchone()


def approve(
    conn: psycopg.Connection, bill_id, *, amount_due_cents: int, biller_name: str, story: str, max_live: int, actor: dict
) -> bool:
    staff.require_role(actor, "reviewer")
    bill = conn.execute("select status, amount_due_cents from bills where id = %s for update", (bill_id,)).fetchone()
    if not bill or bill["status"] in ("paid", "removed", "rejected"):
        return False
    verification = trust.eligible_verification(conn, bill_id, amount=amount_due_cents, lock=True, require_review=False)
    if amount_due_cents != verification["verified_balance_cents"]:
        raise trust.TrustError("Published amount must equal the independently verified eligible balance")
    if conn.execute("select 1 from review_decisions where verification_id = %s and decision = 'approved'", (verification["id"],)).fetchone():
        return False
    if bill["status"] != "submitted" and bill["amount_due_cents"] != amount_due_cents:
        raise trust.TrustError("Resolve changed balances and refund excess funding before changing a published amount")
    biller_name = verification["legal_name"]
    conn.execute("""insert into review_decisions (bill_id, verification_id, actor_id, decision)
                    values (%s, %s, %s, 'approved')""", (bill_id, verification["id"], actor["id"]))
    if bill["status"] != "submitted":
        conn.execute("update bills set story = %s, biller_name = %s where id = %s", (story, biller_name, bill_id))
        trust.audit(conn, actor, "bill.reapproved", bill_id, verification_id=str(verification["id"]))
        return True
    row = conn.execute(
        """
        update bills
        set status = 'queued', amount_due_cents = %s, biller_name = %s, story = %s
        where id = %s and status = 'submitted'
        returning id
        """,
        (amount_due_cents, biller_name, story, bill_id),
    ).fetchone()
    if row is None:
        return False
    trust.audit(conn, actor, "bill.approved", bill_id, amount_cents=amount_due_cents)
    promote_queued(conn, max_live)
    # A bill promoted straight to live already got the "live" email from promote_queued.
    status = conn.execute("select status from bills where id = %s", (bill_id,)).fetchone()["status"]
    if status == "queued":
        notify.bill_event(conn, bill_id, "queued")
    return True


def reject(conn: psycopg.Connection, bill_id, note: str) -> bool:
    row = conn.execute(
        "update bills set status = 'rejected', review_note = %s where id = %s and status = 'submitted' returning id",
        (note, bill_id),
    ).fetchone()
    if row:
        notify.bill_event(conn, bill_id, "rejected")
    return row is not None


def remove(conn: psycopg.Connection, bill_id, note: str, max_live: int) -> list[dict]:
    """Takes a bill down. Returns the contributions whose money must be refunded."""
    conn.execute("select id from bills where id = %s for update", (bill_id,))
    if conn.execute("select id from payment_orders where bill_id = %s and status not in ('prepared', 'canceled', 'returned')",
                    (bill_id,)).fetchone():
        raise trust.TrustError("Payment may already be in progress; treasury must resolve it before refunding")
    conn.execute("update payment_orders set status = 'canceled' where bill_id = %s and status = 'prepared'", (bill_id,))
    row = conn.execute(
        """
        update bills set status = 'removed', review_note = %s
        where id = %s and status in ('queued', 'live', 'funded', 'expired')
        returning id
        """,
        (note, bill_id),
    ).fetchone()
    if row is None:
        return []
    conn.execute(
        "update contributions set status = 'canceled' where bill_id = %s and status = 'pending'", (bill_id,)
    )
    refunds = conn.execute(
        """
        update contributions set status = 'refund_pending'
        where bill_id = %s and status = 'succeeded'
        returning id, payment_ref
        """,
        (bill_id,),
    ).fetchall()
    for refund in refunds:
        jobs.enqueue(conn, "refund", refund["id"])
    conn.execute("update reports set resolved_at = now() where bill_id = %s and resolved_at is null", (bill_id,))
    notify.bill_event(conn, bill_id, "removed")
    promote_queued(conn, max_live)
    return refunds


def mark_paid(conn: psycopg.Connection, bill_id, confirmation: str) -> bool:
    raise trust.TrustError("Use the evidence-backed settlement and posting workflow; a confirmation string is insufficient")


def promote_queued(conn: psycopg.Connection, max_live: int) -> int:
    conn.execute("select pg_advisory_xact_lock(%s)", (QUEUE_LOCK_KEY,))
    live = conn.execute("select count(*) as n from bills where status = 'live'").fetchone()["n"]
    open_slots = max_live - live
    if open_slots <= 0:
        return 0
    promoted = conn.execute(
        f"""
        update bills set status = 'live', live_at = now()
        where id in (
            select b.id from bills b where status = 'queued' and {ELIGIBLE_SQL}
            order by created_at limit %s for update skip locked
        )
        returning id
        """,
        (open_slots,),
    ).fetchall()
    for bill in promoted:
        notify.bill_event(conn, bill["id"], "live")
    return len(promoted)


def expire_stale(conn: psycopg.Connection, settings: Settings) -> None:
    expired = conn.execute(
        """
        update bills set status = 'expired'
        where status in ('queued', 'live') and due_date < current_date - %s::int
        returning id
        """,
        (settings.expiry_grace_days,),
    ).fetchall()
    for bill in expired:
        notify.bill_event(conn, bill["id"], "expired")
    promote_queued(conn, settings.max_live_bills)
    notify.queue_open_bill_notices(conn)


# Shared by the deck and the one finishable card pulled onto the first page.
_FEED_WHERE = f"""
    b.status = 'live'
      and {ELIGIBLE_SQL}
      and (%(categories)s::text[] is null or b.category = any(%(categories)s::text[]))
      and (%(state)s::text is null or b.state = %(state)s::text)
      and (%(max_remaining)s::int is null or b.amount_due_cents - b.amount_raised_cents <= %(max_remaining)s::int)
      and not (b.id = any(%(exclude)s::uuid[]))
      and not exists (
          select 1 from passes p
          where p.user_id = %(user)s and p.bill_id = b.id
            and p.created_at > now() - make_interval(days => %(cooldown)s::int)
      )
      and not exists (
          select 1 from contributions c
          where c.user_id = %(user)s and c.bill_id = b.id and c.status = 'succeeded'
      )
"""


def _place_climax(conn: psycopg.Connection, rows: list[dict], params: dict) -> list[dict]:
    """Moves one nearly-finished bill up so the session has something a single gift can end.

    A payer's first session opens on it. After that it sits a couple of cards in, and fair rotation
    still leads.
    """
    climax = conn.execute(
        f"""
        select {PUBLIC_COLUMNS}
        from bills b
        where {_FEED_WHERE}
          and b.amount_raised_cents < b.amount_due_cents
          and (
            (b.amount_due_cents - b.amount_raised_cents) * 4 <= b.amount_due_cents
            or b.amount_due_cents - b.amount_raised_cents <= 7500
          )
        order by (b.amount_due_cents - b.amount_raised_cents), b.due_date
        limit 1
        """,
        params,
    ).fetchone()
    if climax is None:
        return rows
    rows = [row for row in rows if row["id"] != climax["id"]]
    first_session = conn.execute(
        "select not exists (select 1 from contributions where user_id = %s and status = 'succeeded') as first",
        (params["user"],),
    ).fetchone()["first"]
    index = 0 if first_session else min(2, len(rows))
    rows.insert(index, climax)
    return rows


# --- Giver feed --------------------------------------------------------------


def feed(
    conn: psycopg.Connection,
    user_id,
    *,
    limit: int,
    settings: Settings,
    categories: list[str] | None = None,
    state: str | None = None,
    max_remaining_cents: int | None = None,
    exclude_ids: list | None = None,
) -> list[dict]:
    """Bills for one giver's deck, optionally narrowed by their discovery preferences.

    `exclude_ids` lists bills already in the giver's deck, so loading more never repeats a card.
    """
    # Least-shown bills first, so popular-looking bills can't crowd out the rest; due date breaks ties.
    params = {
        "user": user_id,
        "cooldown": settings.pass_cooldown_days,
        "limit": limit,
        "categories": categories or None,
        "state": state,
        "max_remaining": max_remaining_cents,
        "exclude": exclude_ids or [],
    }
    rows = list(conn.execute(
        f"""
        select {PUBLIC_COLUMNS}
        from bills b
        where {_FEED_WHERE}
        order by b.times_shown, b.due_date, b.live_at
        limit %(limit)s
        """,
        params,
    ).fetchall())
    # The first page keeps one bill a single gift can finish, so a session has a climax. Later
    # pages stay in fair rotation and don't pull that same bill back in.
    if not exclude_ids:
        rows = _place_climax(conn, rows, params)
    if rows:
        conn.execute(
            "update bills set times_shown = times_shown + 1 where id = any(%s)", ([r["id"] for r in rows],)
        )
    return rows


def get_public_bill(conn: psycopg.Connection, bill_id) -> dict | None:
    return conn.execute(
        f"""select {PUBLIC_COLUMNS} from bills b where b.id = %s and b.status in ('live', 'funded', 'paid', 'expired')
            and (b.status <> 'live' or {ELIGIBLE_SQL})""",
        (bill_id,),
    ).fetchone()


def record_pass(conn: psycopg.Connection, user_id, bill_id) -> None:
    conn.execute(
        """
        insert into passes (user_id, bill_id) values (%s, %s)
        on conflict (user_id, bill_id) do update set created_at = now()
        """,
        (user_id, bill_id),
    )


def undo_pass(conn: psycopg.Connection, user_id, bill_id) -> None:
    conn.execute("delete from passes where user_id = %s and bill_id = %s", (user_id, bill_id))


def report(conn: psycopg.Connection, user_id, bill_id, reason: str) -> None:
    conn.execute(
        "insert into reports (bill_id, user_id, reason) values (%s, %s, %s) on conflict do nothing",
        (bill_id, user_id, reason),
    )


# --- Contributions -----------------------------------------------------------


def reserve_contribution(
    conn: psycopg.Connection, *, bill_id, user_id, amount_cents: int, provider_name: str, settings: Settings
) -> dict:
    """Holds amount_cents of the bill's remaining balance for this giver while they pay."""
    recent = conn.execute(
        "select count(*) as n from contributions where user_id = %s and created_at > now() - interval '1 hour'",
        (user_id,),
    ).fetchone()["n"]
    if recent >= settings.max_contributions_per_hour:
        raise ContributionRejected("rate_limited", "You've given a lot in the last hour. Try again a bit later.")

    # The row lock makes concurrent givers on one bill take turns, so each one sees the others' holds.
    bill = conn.execute(
        "select status, amount_due_cents, amount_raised_cents from bills where id = %s for update",
        (bill_id,),
    ).fetchone()
    if bill is None or bill["status"] != "live":
        raise ContributionRejected("not_open", "This bill isn't taking contributions anymore.")
    try:
        trust.eligible_verification(conn, bill_id, amount=bill["amount_due_cents"], lock=True)
        trust._no_disputes(conn, bill_id)
        if not conn.execute("""select 1 from bills b join review_decisions r on r.verification_id = b.verification_id
                               where b.id = %s and r.decision = 'approved'""", (bill_id,)).fetchone():
            raise trust.TrustError("Review is required")
    except trust.TrustError:
        raise ContributionRejected("verification_required", "This bill is being reverified before accepting gifts.") from None

    reserved = conn.execute(
        """
        select coalesce(sum(amount_cents), 0) as n from contributions
        where bill_id = %s and status = 'pending' and expires_at > now()
        """,
        (bill_id,),
    ).fetchone()["n"]
    available = bill["amount_due_cents"] - bill["amount_raised_cents"] - reserved
    if available <= 0:
        raise ContributionRejected("fully_reserved", "Other givers are covering the rest of this bill right now.")
    if amount_cents > available:
        raise ContributionRejected(
            "over_balance", f"Only {format_dollars(available)} is left on this bill.", available
        )
    # The minimum doesn't apply when the amount exactly closes the bill.
    if amount_cents < settings.min_contribution_cents and amount_cents != available:
        raise ContributionRejected(
            "below_minimum", f"The minimum is {format_dollars(settings.min_contribution_cents)}.", available
        )

    return conn.execute(
        """
        insert into contributions (bill_id, user_id, amount_cents, provider, expires_at)
        values (%s, %s, %s, %s, now() + make_interval(mins => %s::int))
        returning id, bill_id, amount_cents, status, expires_at
        """,
        (bill_id, user_id, amount_cents, provider_name, settings.reservation_minutes),
    ).fetchone()


def attach_checkout(conn: psycopg.Connection, contribution_id, provider_ref: str) -> None:
    conn.execute("update contributions set provider_ref = %s where id = %s", (provider_ref, contribution_id))


def cancel_pending(conn: psycopg.Connection, contribution_id) -> None:
    conn.execute(
        "update contributions set status = 'canceled' where id = %s and status = 'pending'", (contribution_id,)
    )


def confirm_payment(
    conn: psycopg.Connection,
    contribution_id,
    *,
    payment_ref: str | None,
    paid_amount_cents: int | None,
    max_live: int,
) -> Confirmation:
    """Applies a finished payment to its bill. Safe to call more than once for the same payment."""
    row = conn.execute("select bill_id from contributions where id = %s", (contribution_id,)).fetchone()
    if row is None:
        raise LookupError(f"No contribution {contribution_id}")

    # Lock order is always bill, then contribution, matching reserve_contribution, so the two can't deadlock.
    bill = conn.execute(
        "select status, amount_due_cents, amount_raised_cents from bills where id = %s for update",
        (row["bill_id"],),
    ).fetchone()
    contribution = conn.execute(
        "select user_id, amount_cents, status from contributions where id = %s for update", (contribution_id,)
    ).fetchone()
    if contribution["status"] in ("succeeded", "refund_pending", "refunded"):
        return Confirmation("already_processed")

    amount = contribution["amount_cents"]
    received = paid_amount_cents if paid_amount_cents is not None else amount
    if received <= 0 or not payment_ref:
        raise trust.TrustError("A collection needs a positive actual amount and provider payment reference")
    ledger.collect(conn, contribution_id, row["bill_id"], received)
    conn.execute("update contributions set received_amount_cents = %s where id = %s", (received, contribution_id))
    remaining = bill["amount_due_cents"] - bill["amount_raised_cents"]
    amount_mismatch = paid_amount_cents is not None and paid_amount_cents != amount
    try:
        trust.eligible_verification(conn, row["bill_id"], amount=bill["amount_due_cents"], lock=True)
        trust._no_disputes(conn, row["bill_id"])
        eligible = True
    except trust.TrustError:
        eligible = False
    if bill["status"] != "live" or amount > remaining or amount_mismatch or not eligible:
        # Money arrived for a bill that can't take it: closed, removed, or the hold lapsed and others
        # filled it. Applying it would overpay the biller, so it goes back to the giver instead.
        conn.execute(
            """
            update contributions set status = 'refund_pending', payment_ref = %s, completed_at = now()
            where id = %s
            """,
            (payment_ref, contribution_id),
        )
        jobs.enqueue(conn, "refund", contribution_id)
        return Confirmation("refund")

    conn.execute(
        "update contributions set status = 'succeeded', payment_ref = %s, completed_at = now() where id = %s",
        (payment_ref, contribution_id),
    )
    conn.execute(
        "update bills set amount_raised_cents = amount_raised_cents + %s where id = %s", (amount, row["bill_id"])
    )
    closed = amount == remaining
    if closed:
        conn.execute(
            "update bills set status = 'funded', funded_at = now(), closer_user_id = %s where id = %s",
            (contribution["user_id"], row["bill_id"]),
        )
        notify.bill_event(conn, row["bill_id"], "funded")
        notify.givers_of_bill(conn, row["bill_id"], "funded")
        promote_queued(conn, max_live)
    return Confirmation("succeeded", closed_bill=closed)


def mark_refunded(conn: psycopg.Connection, contribution_id) -> None:
    bill = conn.execute("select bill_id from contributions where id = %s", (contribution_id,)).fetchone()
    if not bill:
        return
    conn.execute("select id from bills where id = %s for update", (bill["bill_id"],))
    contribution = conn.execute("select * from contributions where id = %s for update", (contribution_id,)).fetchone()
    if contribution["status"] != "refund_pending":
        return
    ledger.refund(conn, contribution)
    row = conn.execute(
        "update contributions set status = 'refunded', refunded_at = now() where id = %s and status = 'refund_pending' returning id",
        (contribution_id,),
    ).fetchone()
    if row:
        conn.execute("""update work_jobs set status = 'done', lease_until = null, lease_token = null, last_error = null
                        where kind = 'refund' and object_id = %s""", (contribution_id,))
        notify.giver_refunded(conn, contribution_id)


def get_contribution(conn: psycopg.Connection, contribution_id, user_id) -> dict | None:
    return conn.execute(
        """
        select id, bill_id, case when status in ('refund_pending', 'refunded')
                   then coalesce(received_amount_cents, amount_cents) else amount_cents end as amount_cents,
               status, created_at, completed_at
        from contributions where id = %s and user_id = %s
        """,
        (contribution_id, user_id),
    ).fetchone()


# --- Impact and leaderboard --------------------------------------------------


def impact(conn: psycopg.Connection, user_id) -> dict:
    totals = conn.execute(
        """
        select
            coalesce(sum(c.amount_cents) filter (where c.status = 'succeeded'), 0) as total_given_cents,
            count(distinct c.bill_id) filter (where c.status = 'succeeded') as bills_helped,
            count(distinct c.bill_id) filter (
                where c.status = 'succeeded' and b.status = 'paid' and b.amount_raised_cents = b.amount_due_cents
            ) as bills_completed
        from contributions c join bills b on b.id = c.bill_id
        where c.user_id = %s
        """,
        (user_id,),
    ).fetchone()
    closed = conn.execute(
        "select count(*) as n from bills where closer_user_id = %s and status in ('funded', 'paid')", (user_id,)
    ).fetchone()["n"]
    return {**totals, "bills_closed": closed}


def tier(bills_completed: int) -> dict:
    current_index = max(i for i, (_, threshold) in enumerate(TIERS) if bills_completed >= threshold)
    name, threshold = TIERS[current_index]
    if current_index + 1 < len(TIERS):
        next_name, next_threshold = TIERS[current_index + 1]
        progress = (bills_completed - threshold) / (next_threshold - threshold)
    else:
        next_name, next_threshold, progress = None, None, 1.0
    return {"name": name, "next_name": next_name, "next_at": next_threshold, "progress": round(progress, 3)}


def badges(stats: dict) -> list[dict]:
    # Badges reward finishing bills, not dollar totals, so a $20 giver can earn every one of them.
    milestones = [
        ("first_bill", "First bill", "Helped with your first bill", stats["bills_helped"] >= 1),
        ("closer", "Closer", "Paid the last dollar on a bill", stats["bills_closed"] >= 1),
        ("five_complete", "Five paid off", "Helped 5 bills get fully paid off", stats["bills_completed"] >= 5),
        ("finisher", "Finisher", "Closed 10 bills", stats["bills_closed"] >= 10),
    ]
    return [
        {"id": badge_id, "title": title, "detail": detail, "earned": earned}
        for badge_id, title, detail, earned in milestones
    ]


def my_contributions(conn: psycopg.Connection, user_id) -> list[dict]:
    return conn.execute(
        """
        select c.id, case when c.status in ('refund_pending', 'refunded')
                          then coalesce(c.received_amount_cents, c.amount_cents) else c.amount_cents end as amount_cents,
               c.status, c.created_at,
               b.id as bill_id, b.biller_name, b.category, b.requester_first_name,
               b.status as bill_status, b.amount_due_cents, b.amount_raised_cents,
               coalesce(b.closer_user_id = c.user_id, false) as closed_by_me
        from contributions c join bills b on b.id = c.bill_id
        where c.user_id = %s and c.status in ('succeeded', 'refund_pending', 'refunded')
        order by c.created_at desc
        limit 100
        """,
        (user_id,),
    ).fetchall()


def activity(conn: psycopg.Connection, user_id, limit: int = 30) -> dict:
    """What happened to the bills this giver helped: covered, paid to the biller, or refunded."""
    events = conn.execute(
        """
        with helped as (
            select distinct bill_id from contributions where user_id = %(user)s and status = 'succeeded'
        )
        select * from (
            select 'funded' as kind, b.funded_at as at, b.id as bill_id, b.biller_name, b.category,
                   b.requester_first_name, coalesce(b.closer_user_id = %(user)s, false) as closed_by_me,
                   b.amount_raised_cents as amount_cents
            from bills b join helped h on h.bill_id = b.id
            where b.funded_at is not null
            union all
            select 'paid', b.paid_at, b.id, b.biller_name, b.category, b.requester_first_name,
                   coalesce(b.closer_user_id = %(user)s, false), b.amount_raised_cents
            from bills b join helped h on h.bill_id = b.id
            where b.paid_at is not null
            union all
            select 'refunded', c.refunded_at, b.id, b.biller_name, b.category, b.requester_first_name,
                   false, coalesce(c.received_amount_cents, c.amount_cents)
            from contributions c join bills b on b.id = c.bill_id
            where c.user_id = %(user)s and c.status = 'refunded' and c.refunded_at is not null
        ) events
        order by at desc
        limit %(limit)s
        """,
        {"user": user_id, "limit": limit},
    ).fetchall()
    seen_at = conn.execute("select activity_seen_at from users where id = %s", (user_id,)).fetchone()["activity_seen_at"]
    for event in events:
        event["unread"] = seen_at is None or event["at"] > seen_at
    return {"events": events, "unread_count": sum(e["unread"] for e in events)}


def mark_activity_seen(conn: psycopg.Connection, user_id) -> None:
    conn.execute("update users set activity_seen_at = now() where id = %s", (user_id,))


def leaderboard(conn: psycopg.Connection, user_id, *, period: str, limit: int = 25, public_only: bool = True) -> dict:
    since = None
    if period == "month":
        since = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Hidden payers stay in the private ranking and drop out of the public one, which is then re-ranked.
    visibility = "and u.show_on_leaderboards" if public_only else ""
    # Ranked by bills helped to completion, then bills closed. Dollar totals are deliberately not shown.
    rows = conn.execute(
        f"""
        with completed as (
            select c.user_id, count(distinct c.bill_id) as bills_completed
            from contributions c join bills b on b.id = c.bill_id
            where c.status = 'succeeded' and b.status = 'paid' and b.amount_raised_cents = b.amount_due_cents
              and (%(since)s::timestamptz is null or b.funded_at >= %(since)s)
            group by c.user_id
        ), closed as (
            select closer_user_id as user_id, count(*) as bills_closed
            from bills
            where closer_user_id is not null and status in ('funded', 'paid')
              and (%(since)s::timestamptz is null or funded_at >= %(since)s)
            group by closer_user_id
        )
        select u.id, u.handle, d.bills_completed, coalesce(c.bills_closed, 0) as bills_closed,
               rank() over (order by d.bills_completed desc, coalesce(c.bills_closed, 0) desc) as rank
        from completed d
        join users u on u.id = d.user_id and u.deleted_at is null {visibility}
        left join closed c on c.user_id = d.user_id
        order by rank, u.handle
        """,
        {"since": since},
    ).fetchall()
    entries = [
        {"rank": r["rank"], "handle": r["handle"], "bills_completed": r["bills_completed"],
         "bills_closed": r["bills_closed"], "is_me": r["id"] == user_id}
        for r in rows
    ]
    me = next((e for e in entries if e["is_me"]), None)
    return {"period": period, "entries": entries[:limit], "me": me, "of": len(entries)}


# --- Admin views -------------------------------------------------------------


def admin_overview(conn: psycopg.Connection) -> dict:
    counts = {
        r["status"]: r["n"]
        for r in conn.execute("select status, count(*) as n from bills group by status").fetchall()
    }
    review = conn.execute(
        "select * from bills where status = 'submitted' order by created_at"
    ).fetchall()
    active = conn.execute(
        """
        select b.*, (select count(*) from reports r where r.bill_id = b.id and r.resolved_at is null) as open_reports
        from bills b where b.status in ('live', 'queued')
        order by b.status, b.due_date
        """
    ).fetchall()
    payouts = conn.execute(
        """
        select * from bills
        where status = 'funded' or (status = 'expired' and amount_raised_cents > 0)
        order by funded_at nulls last, due_date
        """
    ).fetchall()
    refunds = conn.execute(
        """
        select c.id, c.amount_cents, c.payment_ref, c.completed_at, b.biller_name
        from contributions c join bills b on b.id = c.bill_id
        where c.status = 'refund_pending' order by c.completed_at
        """
    ).fetchall()
    reports = conn.execute(
        """
        select r.id, r.reason, r.created_at, b.id as bill_id, b.biller_name, b.requester_first_name, b.status
        from reports r join bills b on b.id = r.bill_id
        where r.resolved_at is null order by r.created_at
        """
    ).fetchall()
    return {
        "counts": counts, "review": review, "active": active,
        "payouts": payouts, "refunds": refunds, "reports": reports,
    }


def get_upload(conn: psycopg.Connection, bill_id) -> dict | None:
    return conn.execute(
        "select upload_path, upload_content_type from bills where id = %s", (bill_id,)
    ).fetchone()


def resolve_report(conn: psycopg.Connection, report_id) -> None:
    conn.execute("update reports set resolved_at = now() where id = %s", (report_id,))
