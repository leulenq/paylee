"""Test fixtures use explicit synthetic evidence; none of these shortcuts exists in app code."""

from datetime import datetime, timezone

from app import db, documents, staff, trust
from app.security import admin_csrf_token

PASSWORD = "test-staff-password"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def operators(conn):
    result = {}
    for name, roles in (("reviewer", ["reviewer"]), ("payer", ["payments"]), ("treasury", ["treasury"])):
        row = conn.execute("select * from staff where username = %s", (name,)).fetchone()
        if not row:
            row = conn.execute("insert into staff (username, password_hash, roles) values (%s, %s, %s) returning *",
                               (name, staff.password_hash(PASSWORD), roles)).fetchone()
        result[name] = row
    return result


def headers(actor):
    return {"Cookie": f"{staff.COOKIE}={staff.issue_token(actor)}", "X-CSRF-Token": admin_csrf_token(actor["id"])}


def clean_document(conn, purpose="evidence"):
    row = documents.store(conn, PNG, "image/png", purpose)
    conn.execute("update documents set scan_status = 'clean', scan_provider = 'development-unverified', extraction_status = 'manual' where id = %s",
                 (row["id"],))
    conn.execute("update work_jobs set status = 'done' where kind = 'document' and object_id = %s", (row["id"],))
    return row["id"]


def proof(conn, kind, subject, amount=None, reference="TEST-CONFIRMATION", observed_at=None):
    actors = operators(conn)
    actor = actors["treasury"] if kind in ("funding", "settlement", "posting", "return", "non_execution") else actors["payer"] if kind == "submission" else actors["reviewer"]
    return trust.record_evidence(conn, actor, kind=kind, document_id=clean_document(conn), reference=reference,
                                 source="Synthetic independently verified fixture", subject=str(subject), amount_cents=amount,
                                 observed_at=observed_at or datetime.now(timezone.utc))["id"]


def verify_fixture_bill(conn, bill_id, amount=None):
    bill = conn.execute("select * from bills where id = %s", (bill_id,)).fetchone()
    if bill["status"] in ("paid", "removed", "rejected"):
        conn.execute("update bills set status = 'submitted' where id = %s", (bill_id,))
    actors = operators(conn)
    doc_id = bill["document_id"] or clean_document(conn, "bill")
    conn.execute("update documents set scan_status = 'clean', scan_provider = 'development-unverified' where id = %s", (doc_id,))
    conn.execute("update bills set document_id = %s, consented_at = now() where id = %s", (doc_id, bill_id))
    biller = conn.execute("select * from billers where legal_name = %s and category = %s and status = 'approved'",
                          (bill["biller_name"], bill["category"])).fetchone()
    if not biller:
        domain = "biller.example"
        biller = trust.create_biller(conn, actors["reviewer"], legal_name=bill["biller_name"], category=bill["category"],
                                     official_domain=domain, portal_url=f"https://{domain}/pay",
                                     evidence_id=proof(conn, "biller", domain))
        trust.approve_biller(conn, actors["payer"], biller["id"])
    amount = amount or bill["amount_due_cents"] or bill["claimed_amount_cents"]
    verification = trust.verify_bill(conn, actors["reviewer"], bill_id, biller_id=biller["id"],
                                     account_reference=f"fixture-account-{bill_id}", period_label="2026-10",
                                     verified_balance_cents=amount, evidence_id=proof(conn, "debt", bill_id, amount))
    if bill["status"] != "submitted":
        conn.execute("update bills set status = %s where id = %s", (bill["status"], bill_id))
        conn.execute("insert into review_decisions (bill_id, verification_id, actor_id, decision) values (%s, %s, %s, 'approved')",
                     (bill_id, verification["id"], actors["reviewer"]["id"]))
    return verification


def complete_payment(bill_id, reference="CONF-1"):
    with db.transaction() as conn:
        actors = operators(conn)
        contributions = conn.execute("select * from contributions where bill_id = %s and status = 'succeeded'", (bill_id,)).fetchall()
        for contribution in contributions:
            trust.reconcile_funding(conn, actors["treasury"], contribution["id"],
                                     proof(conn, "funding", contribution["id"], contribution["amount_cents"]))
        order = trust.prepare_payment(conn, actors["reviewer"], bill_id)
        trust.release_payment(conn, actors["payer"], order["id"], proof(conn, "balance", bill_id, order["amount_cents"]))
        trust.claim_execution(conn, actors["payer"], order["id"])
        trust.record_submission(conn, actors["payer"], order["id"], proof(conn, "submission", order["id"], order["amount_cents"]), f"external-{order['id']}")
        trust.record_settlement(conn, actors["treasury"], order["id"], proof(conn, "settlement", order["id"], order["amount_cents"]))
        trust.record_posting(conn, actors["treasury"], order["id"], proof(conn, "posting", order["id"], order["amount_cents"], reference))
    return order
