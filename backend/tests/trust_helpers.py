"""Test fixtures use explicit synthetic evidence; none of these shortcuts exists in app code."""

from datetime import date, datetime, timezone

from app import db, documents, eligibility, staff, trust, vault
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


def verify_fixture_bill(conn, bill_id, amount=None, eligible=True):
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
    if eligible:
        eligible_fixture(conn, bill_id)
    if bill["status"] != "submitted":
        conn.execute("update bills set status = %s where id = %s", (bill["status"], bill_id))
        if eligible:
            eligibility.publish(conn, actors["reviewer"], bill_id, amount)
        conn.execute("insert into review_decisions (bill_id, verification_id, actor_id, decision) values (%s, %s, %s, 'approved')",
                     (bill_id, verification["id"], actors["reviewer"]["id"]))
    return verification


HOLDER = "Maria Garcia"


def claim_fixture(conn, bill_id, holder=HOLDER, street=None, zip_code="40508", user_id=None):
    """Inserts a requester claim directly so fixtures can attach one to bills in any state."""
    street = street or f"{abs(hash(str(bill_id))) % 9000 + 100} Main Street"
    return conn.execute(
        """insert into eligibility_claims (bill_id, requester_user_id, holder_name_ciphertext, service_address_ciphertext,
                                           household_fingerprint, hardship_cause, signature_ciphertext)
           values (%s, %s, %s, %s, %s, 'reduced_income', %s) returning *""",
        (bill_id, user_id, vault.encrypt(holder), vault.encrypt(f"{street}\n{zip_code}"),
         eligibility.household_fingerprint(street, zip_code), vault.encrypt(holder)),
    ).fetchone()


def bind_fixture(conn, bill_id, holder=HOLDER, street=None, zip_code="40508"):
    reviewer = operators(conn)["reviewer"]
    if street is None:
        street = f"{abs(hash(str(bill_id))) % 9000 + 100} Main Street"
    return eligibility.record_account_holder(conn, reviewer, bill_id, evidence_id=proof(conn, "account_holder", bill_id),
                                             holder_name=holder, service_street=street, service_zip=zip_code)


def subject(conn, bill_id):
    bill = conn.execute("select id, requester_user_id from bills where id = %s", (bill_id,)).fetchone()
    return str(bill["requester_user_id"] or bill["id"])


def identity_fixture(conn, bill_id, legal_name=HOLDER, dob=date(1985, 4, 2)):
    return eligibility.record_identity(conn, operators(conn)["reviewer"], bill_id,
                                       evidence_id=proof(conn, "identity", subject(conn, bill_id)),
                                       legal_name=legal_name, date_of_birth=dob)


def hardship_fixture(conn, bill_id, method="benefits", legal_name=HOLDER):
    target = str(bill_id) if method == "biller_program" else subject(conn, bill_id)
    return eligibility.record_hardship(conn, operators(conn)["reviewer"], bill_id, method=method,
                                       evidence_id=proof(conn, "hardship", target),
                                       legal_name=None if method == "biller_program" else legal_name)


def eligible_fixture(conn, bill_id):
    """Full identity and benefits evidence, so fixture bills publish at any amount and are never audited."""
    claim_fixture(conn, bill_id)
    bind_fixture(conn, bill_id)
    identity_fixture(conn, bill_id)
    hardship_fixture(conn, bill_id)


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
