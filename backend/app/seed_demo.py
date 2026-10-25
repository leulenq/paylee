"""Repeatable, fake-money-only development account fixtures.

Usage: python -m app.seed_demo --device-id device-...
Synthetic evidence is explicitly marked development-unverified; no real biller is contacted.
"""

import argparse
import secrets
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from . import accounts, bills, db, documents, staff, trust
from .config import get_settings
from .seed import PLACEHOLDER_PNG, SAMPLES

SOURCE = "Synthetic development fixture. No real debt, creditor verification, agreement, or payment."


def require_demo_environment(settings):
    if (settings.environment not in ("development", "test") or settings.payment_provider != "fake"
            or not settings.allow_dev_login or settings.storage_provider != "local"
            or settings.email_provider != "console" or settings.push_provider != "log"):
        raise ValueError("Demo seeding requires development/test, fake payments, dev login, local storage, console email, and log pushes")


def _document(conn, purpose="evidence"):
    document = documents.store(conn, PLACEHOLDER_PNG, "image/png", purpose)
    conn.execute("""update documents set scan_status = 'clean', scan_provider = 'development-unverified',
                    extraction_status = 'manual' where id = %s""", (document["id"],))
    conn.execute("update work_jobs set status = 'done' where kind = 'document' and object_id = %s", (document["id"],))
    return document


def _operators(conn):
    result = {}
    for name, role in (("review", "reviewer"), ("pay", "payments"), ("reconcile", "treasury")):
        username = f"__demo_{name}__"
        actor = conn.execute("select * from staff where username = %s", (username,)).fetchone()
        if actor is None:
            actor = conn.execute("""insert into staff (username, password_hash, roles, disabled_at)
                values (%s, %s, %s, now()) returning *""",
                (username, staff.password_hash(secrets.token_urlsafe(40)), [role])).fetchone()
        if actor["disabled_at"] is None:
            raise ValueError("Demo actors must remain disabled for interactive sign-in")
        result[role] = actor
    return result


def _proof(conn, ops, kind, subject, amount=None):
    role = "treasury" if kind in ("funding", "settlement", "posting", "return") else "payments" if kind == "submission" else "reviewer"
    return trust.record_evidence(conn, ops[role], kind=kind, document_id=_document(conn)["id"],
        reference=f"DEMO-{kind}-{subject}", source=SOURCE, subject=str(subject),
        amount_cents=amount, observed_at=datetime.now(timezone.utc))["id"]


def _bill(conn, ops, owner_id, slug, *, category, company, name, city, amount, days, story):
    bill_id = uuid5(NAMESPACE_URL, f"paylee:development:v1:{owner_id}:{slug}")
    existing = conn.execute("select id from bills where id = %s", (bill_id,)).fetchone()
    if existing:
        return bill_id, False
    document = _document(conn, "bill")
    legal_name = f"Demo {company}"
    creditor = conn.execute("select * from billers where legal_name = %s and category = %s and status = 'approved'",
                            (legal_name, category)).fetchone()
    domain = "paylee-demo.invalid"
    if creditor is None:
        creditor = trust.create_biller(conn, ops["reviewer"], legal_name=legal_name, category=category,
            official_domain=domain, portal_url=f"https://{domain}/no-payments",
            evidence_id=_proof(conn, ops, "biller", domain))
        trust.approve_biller(conn, ops["payments"], creditor["id"])
    conn.execute("""insert into bills
        (id, category, biller_name, requester_first_name, requester_email, city, state, story,
         claimed_amount_cents, due_date, upload_path, upload_content_type, document_id, consented_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, current_date + %s::int, %s, 'image/png', %s, now())""",
        (bill_id, category, legal_name, name, f"demo-{slug}@example.invalid", city, city[-2:],
         f"Demo scenario: {story}", amount, days, document["storage_key"], document["id"]))
    trust.verify_bill(conn, ops["reviewer"], bill_id, biller_id=creditor["id"],
        account_reference=f"demo-account-{bill_id}", period_label=f"development-{slug}",
        verified_balance_cents=amount, evidence_id=_proof(conn, ops, "debt", bill_id, amount))
    bills.approve(conn, bill_id, amount_due_cents=amount, biller_name=legal_name,
        story=f"Demo scenario: {story}", max_live=get_settings().max_live_bills, actor=ops["reviewer"])
    return bill_id, True


def _give(conn, user_id, bill_id, amount):
    settings = get_settings()
    contribution = bills.reserve_contribution(conn, bill_id=bill_id, user_id=user_id,
        amount_cents=amount, provider_name="fake", settings=settings)
    bills.confirm_payment(conn, contribution["id"], payment_ref=f"fake_demo_{contribution['id']}",
        paid_amount_cents=amount, max_live=settings.max_live_bills)
    return contribution["id"]


def _apply(conn, ops, bill_id):
    for contribution in conn.execute("select * from contributions where bill_id = %s and status = 'succeeded'", (bill_id,)).fetchall():
        trust.reconcile_funding(conn, ops["treasury"], contribution["id"],
            _proof(conn, ops, "funding", contribution["id"], contribution["amount_cents"]))
    order = trust.prepare_payment(conn, ops["reviewer"], bill_id)
    trust.release_payment(conn, ops["payments"], order["id"], _proof(conn, ops, "balance", bill_id, order["amount_cents"]))
    trust.claim_execution(conn, ops["payments"], order["id"])
    trust.record_submission(conn, ops["payments"], order["id"],
        _proof(conn, ops, "submission", order["id"], order["amount_cents"]), f"DEMO-NO-MONEY-{order['id']}")
    trust.record_settlement(conn, ops["treasury"], order["id"], _proof(conn, ops, "settlement", order["id"], order["amount_cents"]))
    trust.record_posting(conn, ops["treasury"], order["id"], _proof(conn, ops, "posting", order["id"], order["amount_cents"]))


def seed(conn, device_id):
    require_demo_environment(get_settings())
    if not 8 <= len(device_id) <= 100:
        raise ValueError("Use the development device ID stored by the app")
    conn.execute("select pg_advisory_xact_lock(7201077)")
    user = accounts.get_or_create_user(conn, f"dev:{device_id}")
    conn.execute("update users set role = 'payer' where id = %s and role is null", (user["id"],))
    ops = _operators(conn)
    neighbors = [accounts.get_or_create_user(conn, f"dev:paylee-demo-neighbor-{n}") for n in range(3)]
    created = 0
    for index, (category, company, name, city, amount, raised, days, story) in enumerate(SAMPLES):
        bill_id, new = _bill(conn, ops, user["id"], f"feed-{index}", category=category,
            company=company, name=name, city=city, amount=amount, days=days, story=story)
        created += int(new)
        if new and raised:
            _give(conn, neighbors[index % 3]["id"], bill_id, raised)
    history = [
        ("open-electric", "utilities", "Bluegrass Electric", "Ana", "Lexington, KY", 10000, 2500, 3000, 3, "open"),
        ("open-internet", "internet", "Neighborhood Internet", "Sam", "Louisville, KY", 8000, 1500, 5500, 2, "open"),
        ("paid-water", "utilities", "City Water", "Maya", "Lexington, KY", 6200, 6200, 0, 7, "paid"),
        ("paid-phone", "phone", "Neighborhood Mobile", "Eli", "Columbus, OH", 9800, 9800, 0, 8, "paid"),
        ("paid-clinic", "medical", "Community Clinic", "Jo", "Louisville, KY", 12000, 4500, 7500, 12, "paid"),
        ("funded-gas", "utilities", "Community Gas", "Noah", "Bowling Green, KY", 3100, 3100, 0, 5, "funded"),
        ("refunded-bill", "internet", "Town Internet", "Alex", "Cincinnati, OH", 9000, 2700, 0, 6, "refunded"),
    ]
    for index, (slug, category, company, name, city, amount, mine, others, days, stage) in enumerate(history):
        bill_id, new = _bill(conn, ops, user["id"], slug, category=category, company=company,
            name=name, city=city, amount=amount, days=days,
            story="A fictional household needs help with an essential bill. This is local sample data; no money moves.")
        created += int(new)
        if not new:
            continue
        _give(conn, user["id"], bill_id, mine)
        if others:
            _give(conn, neighbors[index % 3]["id"], bill_id, others)
        if stage == "paid":
            _apply(conn, ops, bill_id)
        elif stage == "refunded":
            for contribution in bills.remove(conn, bill_id, "Synthetic demo refund", get_settings().max_live_bills):
                bills.mark_refunded(conn, contribution["id"])
    # Keep the sample feed available; completed history is historical, without backdating evidence.
    conn.execute("delete from passes where user_id = %s and bill_id = any(%s)",
        (user["id"], [uuid5(NAMESPACE_URL, f"paylee:development:v1:{user['id']}:feed-{n}") for n in range(6)]))
    trust.audit(conn, None, "development.account_seeded", user["id"], created_bills=created)
    return {"user_id": str(user["id"]), "handle": user["handle"], "new_bills": created,
            "swipe_bills": 6, "open_bills": 2, "applied_payments": 3, "funded_bills": 1, "refunds": 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    args = parser.parse_args()
    require_demo_environment(get_settings())
    db.open_pool(get_settings().database_url)
    try:
        with db.transaction() as conn:
            result = seed(conn, args.device_id)
        print(result)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
