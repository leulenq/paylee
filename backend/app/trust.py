"""Closed biller network and evidence-bound manual payment operations.

No function accepts an applicant's payout account. The only executable MVP rail is
an operator using the independently approved biller portal. Recording an instruction
does not move money; settlement and posting need separate evidence and reconciliation.
"""

import ipaddress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from . import jobs, ledger, notify, staff, vault
from .config import get_settings


class TrustError(ValueError):
    pass


def audit(conn, actor, action, object_id, **details):
    conn.execute("insert into audit_events (actor_id, action, object_id, details) values (%s, %s, %s, %s)",
                 (actor["id"] if actor else None, action, str(object_id), Jsonb(details)))


def evidence(conn, evidence_id, kind, subject, *, amount=None, max_age=None):
    row = conn.execute(
        """select e.*, d.scan_status, d.scan_provider from evidence e
           join documents d on d.id = e.document_id where e.id = %s""", (evidence_id,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    if not row or row["kind"] != kind or row["subject"] != str(subject) or row["scan_status"] != "clean":
        raise TrustError("Matching evidence backed by a clean document is required")
    if get_settings().environment == "production" and row["scan_provider"] != "clamav":
        raise TrustError("Production evidence requires an actual malware scan")
    if row["observed_at"] > now + timedelta(seconds=5):
        raise TrustError("Evidence cannot be dated in the future")
    if max_age and now - row["observed_at"] > max_age:
        raise TrustError("Evidence is stale; obtain a fresh confirmation")
    if amount is not None and row["amount_cents"] != amount:
        raise TrustError("Evidence amount does not match this operation")
    return row


def record_evidence(conn, actor, *, kind, document_id, reference, source, subject, observed_at, amount_cents=None):
    roles = {"funding": ("treasury",), "settlement": ("treasury",), "posting": ("treasury",),
             "return": ("treasury",), "non_execution": ("treasury",), "submission": ("payments",)}.get(kind, ("reviewer",))
    staff.require_role(actor, *roles)
    document = conn.execute("select * from documents where id = %s", (document_id,)).fetchone()
    if not document or document["scan_status"] != "clean":
        raise TrustError("Scan the supporting document before recording evidence")
    if observed_at.tzinfo is None or observed_at > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise TrustError("Evidence requires a valid timezone-aware observation time")
    if kind != "biller" and amount_cents is None:
        raise TrustError("Financial evidence must record the observed amount")
    row = conn.execute(
        """insert into evidence (kind, document_id, reference, source, subject, observed_at, amount_cents, recorded_by)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (kind, document_id, reference, source, str(subject), observed_at, amount_cents, actor["id"]),
    ).fetchone()
    audit(conn, actor, "evidence.recorded", row["id"], kind=kind)
    return row


def valid_portal(url, domain):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise TrustError("Invalid portal URL") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443) or parts.fragment:
        raise TrustError("Use an HTTPS biller portal without embedded credentials")
    if not domain or domain != domain.lower() or "/" in domain or ":" in domain or "." not in domain:
        raise TrustError("An independently verified official domain is required")
    if host != domain and not host.endswith("." + domain):
        raise TrustError("The portal must belong to the independently verified biller domain")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host.endswith((".local", ".localhost", ".internal")):
            raise TrustError("Private destinations are not biller portals")
    else:
        raise TrustError("IP addresses are not accepted as biller portals")


def create_biller(conn, actor, *, legal_name, category, official_domain, portal_url, evidence_id):
    staff.require_role(actor, "reviewer")
    valid_portal(portal_url, official_domain)
    evidence(conn, evidence_id, "biller", official_domain)
    biller = conn.execute(
        """insert into billers (legal_name, category, official_domain, refund_policy, evidence_id, created_by)
           values (%s, %s, %s, 'return_to_origin', %s, %s) returning *""",
        (legal_name, category, official_domain, evidence_id, actor["id"]),
    ).fetchone()
    destination = conn.execute(
        """insert into payment_destinations (biller_id, version, rail, portal_url, evidence_id, created_by)
           values (%s, 1, 'manual_portal', %s, %s, %s) returning *""",
        (biller["id"], portal_url, evidence_id, actor["id"]),
    ).fetchone()
    conn.execute("update billers set active_destination_id = %s where id = %s", (destination["id"], biller["id"]))
    audit(conn, actor, "biller.created", biller["id"])
    return {**biller, "active_destination_id": destination["id"]}


def approve_biller(conn, actor, biller_id):
    staff.require_role(actor, "payments")
    row = conn.execute("select * from billers where id = %s for update", (biller_id,)).fetchone()
    if not row or row["status"] != "pending":
        raise TrustError("Biller is not pending approval")
    if row["created_by"] == actor["id"]:
        raise TrustError("A second person must approve the biller")
    evidence(conn, row["evidence_id"], "biller", row["official_domain"])
    conn.execute("update billers set status = 'approved', approved_by = %s, approved_at = now() where id = %s",
                 (actor["id"], biller_id))
    audit(conn, actor, "biller.approved", biller_id)


def suspend_biller(conn, actor, biller_id):
    staff.require_role(actor, "reviewer", "payments", "treasury")
    row = conn.execute("select id from billers where id = %s for update", (biller_id,)).fetchone()
    if not row:
        raise TrustError("Biller not found")
    conn.execute("update billers set status = 'suspended' where id = %s", (biller_id,))
    audit(conn, actor, "biller.suspended", biller_id)


def replace_destination(conn, actor, biller_id, *, portal_url, evidence_id):
    staff.require_role(actor, "reviewer")
    biller = conn.execute("select * from billers where id = %s for update", (biller_id,)).fetchone()
    if not biller:
        raise TrustError("Biller not found")
    valid_portal(portal_url, biller["official_domain"])
    evidence(conn, evidence_id, "biller", biller["official_domain"])
    version = conn.execute("select coalesce(max(version), 0) + 1 as n from payment_destinations where biller_id = %s",
                           (biller_id,)).fetchone()["n"]
    dest = conn.execute(
        """insert into payment_destinations (biller_id, version, rail, portal_url, evidence_id, created_by)
           values (%s, %s, 'manual_portal', %s, %s, %s) returning *""",
        (biller_id, version, portal_url, evidence_id, actor["id"]),
    ).fetchone()
    conn.execute("""update billers set active_destination_id = %s, evidence_id = %s, status = 'pending',
                    created_by = %s, approved_by = null, approved_at = null where id = %s""",
                 (dest["id"], evidence_id, actor["id"], biller_id))
    audit(conn, actor, "biller.destination_changed", biller_id, version=version)
    return dest


def verify_bill(conn, actor, bill_id, *, biller_id, account_reference, period_label, verified_balance_cents, evidence_id):
    staff.require_role(actor, "reviewer")
    bill = conn.execute("select * from bills where id = %s for update", (bill_id,)).fetchone()
    if not bill or bill["status"] in ("paid", "removed", "rejected"):
        raise TrustError("Bill cannot be verified in its current state")
    document = conn.execute("select * from documents where id = %s", (bill["document_id"],)).fetchone()
    if not document or document["scan_status"] != "clean" or not bill["consented_at"]:
        raise TrustError("A scanned original bill and requester authorization are required")
    if get_settings().environment == "production" and document["scan_provider"] != "clamav":
        raise TrustError("An actual malware scan of the original bill is required")
    biller = conn.execute("select * from billers where id = %s for share", (biller_id,)).fetchone()
    if not biller or biller["status"] != "approved" or biller["category"] != bill["category"]:
        raise TrustError("Select an approved biller in the matching category")
    proof = evidence(conn, evidence_id, "debt", bill_id, amount=verified_balance_cents,
                     max_age=timedelta(hours=get_settings().verification_hours))
    if verified_balance_cents < bill["amount_raised_cents"]:
        raise TrustError("Balance is below collected contributions; resolve excess funding before reverification")
    account = account_reference.strip()
    period = period_label.strip()
    if not 4 <= len(account) <= 100 or not 3 <= len(period) <= 100:
        raise TrustError("Account reference and invoice/billing period are required")
    account_fp = vault.fingerprint(str(biller_id), account)
    invoice_fp = vault.fingerprint(account_fp, period.casefold())
    if bill["verification_id"] and bill["amount_raised_cents"] > 0:
        previous = conn.execute("select account_fingerprint, invoice_fingerprint from bill_verifications where id = %s",
                                (bill["verification_id"],)).fetchone()
        if previous["account_fingerprint"] != account_fp or previous["invoice_fingerprint"] != invoice_fp:
            raise TrustError("Collected funds cannot be reassigned to a different account or debt")
    conn.execute("insert into debt_claims (fingerprint, bill_id) values (%s, %s) on conflict do nothing",
                 (invoice_fp, bill_id))
    claim = conn.execute("select bill_id from debt_claims where fingerprint = %s", (invoice_fp,)).fetchone()
    if str(claim["bill_id"]) != str(bill_id):
        raise TrustError("This debt has already been submitted under another bill")
    row = conn.execute(
        """insert into bill_verifications
           (bill_id, biller_id, destination_id, account_ciphertext, account_fingerprint, account_last4,
            invoice_fingerprint, period_label, verified_balance_cents, evidence_id, verified_by, expires_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (bill_id, biller_id, biller["active_destination_id"], vault.encrypt(account), account_fp, account[-4:],
         invoice_fp, period, verified_balance_cents, evidence_id, actor["id"],
         proof["observed_at"] + timedelta(hours=get_settings().verification_hours)),
    ).fetchone()
    conn.execute("update bills set verification_id = %s where id = %s", (row["id"], bill_id))
    audit(conn, actor, "bill.verified", bill_id, verification_id=str(row["id"]))
    return row


def eligible_verification(conn, bill_id, *, amount=None, lock=False, require_review=True):
    row = conn.execute(
        """select v.*, br.status as biller_status, br.active_destination_id,
                  d.portal_url, br.legal_name
           from bills b join bill_verifications v on v.id = b.verification_id
           join billers br on br.id = v.biller_id join payment_destinations d on d.id = v.destination_id
           where b.id = %s""" + (" for share of br" if lock else ""), (bill_id,),
    ).fetchone()
    if not row or str(row["bill_id"]) != str(bill_id) or row["biller_status"] != "approved" or row["destination_id"] != row["active_destination_id"]:
        raise TrustError("Bill needs verification against an approved current biller destination")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise TrustError("Bill verification has expired")
    evidence(conn, row["evidence_id"], "debt", bill_id, amount=row["verified_balance_cents"])
    origin = conn.execute("select d.scan_provider, d.scan_status from bills b join documents d on d.id = b.document_id where b.id = %s",
                          (bill_id,)).fetchone()
    if not origin or origin["scan_status"] != "clean":
        raise TrustError("Original bill must pass malware scanning")
    if get_settings().environment == "production" and origin["scan_provider"] != "clamav":
        raise TrustError("Original bill must have a production malware scan")
    if require_review and not conn.execute("select 1 from review_decisions where verification_id = %s and decision = 'approved'",
                                          (row["id"],)).fetchone():
        raise TrustError("The current verification needs a human publication review")
    if amount is not None and amount > row["verified_balance_cents"]:
        raise TrustError("Amount exceeds the independently verified balance")
    return row


def reconcile_funding(conn, actor, contribution_id, evidence_id):
    staff.require_role(actor, "treasury")
    bill = conn.execute("select bill_id from contributions where id = %s", (contribution_id,)).fetchone()
    if not bill:
        raise TrustError("Contribution not found")
    conn.execute("select id from bills where id = %s for update", (bill["bill_id"],))
    row = conn.execute("select * from contributions where id = %s for update", (contribution_id,)).fetchone()
    if row["funds_reconciled_at"] and not row["funds_reverification_required"]:
        return
    if row["status"] != "succeeded" or not row["received_amount_cents"]:
        raise TrustError("Only collected, eligible contributions can be reconciled")
    proof = evidence(conn, evidence_id, "funding", contribution_id, amount=row["received_amount_cents"])
    dispute = conn.execute("select max(updated_at) as at from funding_disputes where contribution_id = %s", (contribution_id,)).fetchone()
    if dispute["at"] and proof["observed_at"] < dispute["at"]:
        raise TrustError("Funding must be rechecked after the latest dispute update")
    _no_disputes(conn, row["bill_id"])
    ledger.post(conn, key=f"funding:{contribution_id}", bill_id=row["bill_id"], contribution_id=contribution_id,
                debit="program_cash", credit="collection_receivable", amount=row["received_amount_cents"])
    conn.execute("update contributions set funds_evidence_id = %s, funds_reconciled_at = now(), funds_reverification_required = false where id = %s",
                 (evidence_id, contribution_id))
    audit(conn, actor, "funding.reconciled", contribution_id, evidence_id=str(evidence_id))


def import_legacy_collection(conn, actor, contribution_id, evidence_id, received_amount_cents):
    """Explicit evidence-backed opening record. Never invent accounting for legacy paid bills."""
    staff.require_role(actor, "treasury")
    row = conn.execute("select bill_id from contributions where id = %s", (contribution_id,)).fetchone()
    if not row:
        raise TrustError("Contribution not found")
    bill = conn.execute("select * from bills where id = %s for update", (row["bill_id"],)).fetchone()
    contribution = conn.execute("select * from contributions where id = %s for update", (contribution_id,)).fetchone()
    if contribution["received_amount_cents"]:
        raise TrustError("Collection is already accounted for")
    if bill["status"] == "paid" or contribution["status"] not in ("succeeded", "refund_pending") or not contribution["payment_ref"]:
        raise TrustError("Only independently confirmed, unspent legacy collections can be imported")
    evidence(conn, evidence_id, "funding", contribution_id, amount=received_amount_cents)
    if contribution["status"] == "succeeded" and received_amount_cents != contribution["amount_cents"]:
        raise TrustError("Legacy funding mismatch must be investigated before import")
    ledger.collect(conn, contribution_id, bill["id"], received_amount_cents)
    conn.execute("update contributions set received_amount_cents = %s where id = %s", (received_amount_cents, contribution_id))
    if contribution["status"] == "refund_pending":
        jobs.enqueue(conn, "refund", contribution_id)
    audit(conn, actor, "funding.legacy_imported", contribution_id, evidence_id=str(evidence_id))


def prepare_payment(conn, actor, bill_id):
    staff.require_role(actor, "reviewer", "payments")
    bill = conn.execute("select * from bills where id = %s for update", (bill_id,)).fetchone()
    if not bill or bill["status"] not in ("funded", "expired") or bill["amount_raised_cents"] <= 0:
        raise TrustError("Only funded bills or eligible expired partial bills can be prepared")
    if conn.execute("select id from payment_orders where bill_id = %s and status not in ('canceled', 'returned')", (bill_id,)).fetchone():
        raise TrustError("A payment instruction already exists for this bill")
    v = eligible_verification(conn, bill_id, amount=bill["amount_raised_cents"], lock=True)
    funded = bill["amount_raised_cents"]
    _no_disputes(conn, bill_id)
    _ready_funding(conn, bill_id, funded)
    row = conn.execute(
        """insert into payment_orders (bill_id, verification_id, destination_id, amount_cents, prepared_by)
           values (%s, %s, %s, %s, %s) returning *""",
        (bill_id, v["id"], v["destination_id"], funded, actor["id"]),
    ).fetchone()
    audit(conn, actor, "payment.prepared", row["id"], amount_cents=funded)
    return row


def _order(conn, order_id):
    # Consistent locking: bill, order, then biller. No external calls inside these transactions.
    row = conn.execute("select bill_id from payment_orders where id = %s", (order_id,)).fetchone()
    if not row:
        raise TrustError("Payment instruction not found")
    conn.execute("select id from bills where id = %s for update", (row["bill_id"],))
    return conn.execute("select * from payment_orders where id = %s for update", (order_id,)).fetchone()


def release_payment(conn, actor, order_id, balance_evidence_id):
    staff.require_role(actor, "payments")
    order = _order(conn, order_id)
    if order["status"] != "prepared":
        raise TrustError("Payment is not awaiting release")
    v = eligible_verification(conn, order["bill_id"], amount=order["amount_cents"], lock=True)
    if order["verification_id"] != v["id"] or order["destination_id"] != v["destination_id"]:
        raise TrustError("Verification or destination changed; cancel and prepare a new instruction")
    if actor["id"] in (order["prepared_by"], v["verified_by"]):
        raise TrustError("A second person must release the payment")
    _no_disputes(conn, order["bill_id"])
    _ready_funding(conn, order["bill_id"], order["amount_cents"])
    proof = evidence(conn, balance_evidence_id, "balance", order["bill_id"],
                     max_age=timedelta(minutes=get_settings().release_balance_minutes))
    if not proof["amount_cents"] or not order["amount_cents"] <= proof["amount_cents"] <= v["verified_balance_cents"]:
        raise TrustError("Fresh outstanding balance does not support the payment")
    conn.execute("""update payment_orders set status = 'released', balance_evidence_id = %s,
                    released_by = %s, released_at = now() where id = %s""",
                 (balance_evidence_id, actor["id"], order_id))
    audit(conn, actor, "payment.released", order_id)


def _no_disputes(conn, bill_id):
    if conn.execute("""select 1 from funding_disputes d join contributions c on c.id = d.contribution_id
                       where c.bill_id = %s and d.status <> 'won'""", (bill_id,)).fetchone():
        raise TrustError("Disputed funding must be resolved before payment execution")


def _ready_funding(conn, bill_id, amount):
    funded = conn.execute("""select coalesce(sum(amount_cents), 0) as n from contributions
                            where bill_id = %s and status = 'succeeded' and funds_reconciled_at is not null
                              and not funds_reverification_required""", (bill_id,)).fetchone()["n"]
    if funded != amount or ledger.balances(conn, bill_id).get("program_cash", 0) < amount:
        raise TrustError("Reconcile all funding and available program cash before preparing payment")


def claim_execution(conn, actor, order_id):
    staff.require_role(actor, "payments")
    order = _order(conn, order_id)
    if order["status"] != "released":
        raise TrustError("Execution was already claimed or this payment has not been released")
    proof = evidence(conn, order["balance_evidence_id"], "balance", order["bill_id"],
                     max_age=timedelta(minutes=get_settings().release_balance_minutes))
    v = eligible_verification(conn, order["bill_id"], amount=order["amount_cents"], lock=True)
    if v["id"] != order["verification_id"] or v["destination_id"] != order["destination_id"]:
        raise TrustError("Instruction is stale; no payment may be executed")
    _no_disputes(conn, order["bill_id"])
    _ready_funding(conn, order["bill_id"], order["amount_cents"])
    conn.execute("""update payment_orders set status = 'executing', execution_claimed_by = %s,
                    execution_claimed_at = now() where id = %s""", (actor["id"], order_id))
    audit(conn, actor, "payment.execution_claimed", order_id)
    return {"payment_id": order["id"], "idempotency_key": order["idempotency_key"],
            "amount_cents": order["amount_cents"], "currency": order["currency"],
            "biller": v["legal_name"], "portal_url": v["portal_url"],
            "account_reference": vault.decrypt(v["account_ciphertext"]), "period": v["period_label"],
            "execute_before": proof["observed_at"] + timedelta(minutes=get_settings().release_balance_minutes)}


def record_submission(conn, actor, order_id, evidence_id, external_ref):
    staff.require_role(actor, "payments")
    order = _order(conn, order_id)
    if order["status"] != "executing" or order["execution_claimed_by"] != actor["id"]:
        raise TrustError("The assigned executor must record the claimed payment")
    proof = evidence(conn, evidence_id, "submission", order_id, amount=order["amount_cents"])
    if proof["observed_at"] < order["execution_claimed_at"]:
        raise TrustError("Submission evidence predates the execution claim")
    if ledger.balances(conn, order["bill_id"]).get("program_cash", 0) < order["amount_cents"]:
        raise TrustError("Insufficient reconciled program cash")
    ledger.post(conn, key=f"payout:{order_id}", bill_id=order["bill_id"], order_id=order_id,
                debit="payout_in_transit", credit="program_cash", amount=order["amount_cents"])
    conn.execute("""update payment_orders set status = 'submitted', external_ref = %s, executed_by = %s,
                    submission_evidence_id = %s, submitted_at = now() where id = %s""",
                 (external_ref, actor["id"], evidence_id, order_id))
    audit(conn, actor, "payment.submitted", order_id)


def record_settlement(conn, actor, order_id, evidence_id):
    staff.require_role(actor, "treasury")
    order = _order(conn, order_id)
    if order["status"] != "submitted" or order["executed_by"] == actor["id"]:
        raise TrustError("Settlement requires a submitted payment and an independent treasury reviewer")
    proof = evidence(conn, evidence_id, "settlement", order_id, amount=order["amount_cents"])
    if proof["observed_at"] < order["execution_claimed_at"]:
        raise TrustError("Settlement evidence predates the payment")
    conn.execute("update payment_orders set status = 'settled', settlement_evidence_id = %s, settled_at = now() where id = %s",
                 (evidence_id, order_id))
    audit(conn, actor, "payment.settled", order_id)


def record_posting(conn, actor, order_id, evidence_id):
    staff.require_role(actor, "treasury")
    order = _order(conn, order_id)
    if order["status"] != "settled" or order["executed_by"] == actor["id"]:
        raise TrustError("Posting requires settlement and an independent treasury reviewer")
    proof = evidence(conn, evidence_id, "posting", order_id, amount=order["amount_cents"])
    if proof["observed_at"] < order["execution_claimed_at"]:
        raise TrustError("Posting evidence predates the payment")
    ledger.post(conn, key=f"applied:{order_id}", bill_id=order["bill_id"], order_id=order_id,
                debit="restricted_payable", credit="payout_in_transit", amount=order["amount_cents"])
    conn.execute("update payment_orders set status = 'applied', posting_evidence_id = %s, applied_at = now() where id = %s",
                 (evidence_id, order_id))
    conn.execute("update bills set status = 'paid', paid_at = now(), payout_confirmation = %s where id = %s",
                 (proof["reference"], order["bill_id"]))
    notify.bill_event(conn, order["bill_id"], "paid")
    notify.givers_of_bill(conn, order["bill_id"], "paid")
    audit(conn, actor, "payment.applied", order_id)


def cancel_payment(conn, actor, order_id):
    staff.require_role(actor, "payments")
    order = _order(conn, order_id)
    # Released instructions may already have been executed in a portal. Never auto-reissue those.
    if order["status"] != "prepared":
        raise TrustError("Only un-released instructions can be canceled; resolve other outcomes with treasury")
    conn.execute("update payment_orders set status = 'canceled' where id = %s", (order_id,))
    audit(conn, actor, "payment.canceled", order_id)


def confirm_not_executed(conn, actor, order_id, evidence_id):
    staff.require_role(actor, "treasury")
    order = _order(conn, order_id)
    if order["status"] not in ("released", "executing"):
        raise TrustError("Only an unsubmitted released or claimed instruction can be resolved this way")
    if actor["id"] in (order["released_by"], order["execution_claimed_by"]):
        raise TrustError("Independent treasury must establish that no payment or pending instruction exists")
    proof = evidence(conn, evidence_id, "non_execution", order_id, amount=order["amount_cents"])
    if proof["observed_at"] < (order["execution_claimed_at"] or order["released_at"]):
        raise TrustError("Non-execution evidence predates the released instruction")
    conn.execute("update payment_orders set status = 'canceled' where id = %s", (order_id,))
    audit(conn, actor, "payment.non_execution_confirmed", order_id, evidence_id=str(evidence_id))


def record_return(conn, actor, order_id, evidence_id):
    staff.require_role(actor, "treasury")
    order = _order(conn, order_id)
    if order["status"] not in ("submitted", "settled", "applied") or actor["id"] == order["executed_by"]:
        raise TrustError("A full return requires a submitted payment and independent treasury confirmation")
    proof = evidence(conn, evidence_id, "return", order_id, amount=order["amount_cents"])
    if proof["observed_at"] < order["execution_claimed_at"]:
        raise TrustError("Return evidence predates the payment")
    ledger.post(conn, key=f"return:{order_id}", bill_id=order["bill_id"], order_id=order_id,
                debit="program_cash", credit="restricted_payable" if order["status"] == "applied" else "payout_in_transit",
                amount=order["amount_cents"])
    conn.execute("update payment_orders set status = 'returned', return_evidence_id = %s, returned_at = now() where id = %s",
                 (evidence_id, order_id))
    conn.execute("update bills set status = 'removed', review_note = 'Biller returned payment' where id = %s", (order["bill_id"],))
    refunds = conn.execute("update contributions set status = 'refund_pending' where bill_id = %s and status = 'succeeded' returning id",
                           (order["bill_id"],)).fetchall()
    for refund in refunds:
        jobs.enqueue(conn, "refund", refund["id"])
    audit(conn, actor, "payment.returned", order_id)


def receipt(conn, contribution_id, user_id):
    # Deliberate public projection: never return internal evidence, portals, account plaintext, or staff notes.
    return conn.execute(
        """select c.id as contribution_id, coalesce(c.received_amount_cents, c.amount_cents) as contribution_cents, c.status as funding_status,
                  c.refunded_at, b.biller_name, b.category, p.id as payment_id,
                  p.status as payment_status, p.amount_cents as bill_payment_cents, p.currency,
                  p.submitted_at, p.settled_at, p.applied_at, p.returned_at,
                  v.account_last4 as account_ending
           from contributions c join bills b on b.id = c.bill_id
           left join lateral (select * from payment_orders where bill_id = b.id
                              order by created_at desc limit 1) p on true
           left join bill_verifications v on v.id = p.verification_id
           where c.id = %s and c.user_id = %s""", (contribution_id, user_id),
    ).fetchone()
