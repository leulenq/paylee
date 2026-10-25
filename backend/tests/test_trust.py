import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import bills, db, ledger, staff, trust, vault
from app.api import _settle
from app.payments import FakeProvider, RefundResult
from app.refunds import process as refund

from .conftest import ADMIN_AUTH, bill_row
from .test_web import submit, valid_form
from .trust_helpers import (
    PASSWORD,
    complete_payment,
    headers,
    operators,
    proof,
)


def collected(client, make_user, make_bill, amount=5000):
    bill_id = make_bill(amount_due_cents=amount)
    auth, user = make_user()
    response = client.post(f"/api/bills/{bill_id}/contributions", headers=auth, json={"amount_cents": amount})
    assert response.status_code == 201, response.text
    return bill_id, response.json()["contribution"]["id"], auth, user


def prepared(client, make_user, make_bill):
    bill_id, contribution_id, auth, user = collected(client, make_user, make_bill)
    with db.transaction() as conn:
        ops = operators(conn)
        trust.reconcile_funding(conn, ops["treasury"], contribution_id, proof(conn, "funding", contribution_id, 5000))
        order = trust.prepare_payment(conn, ops["reviewer"], bill_id)
    return bill_id, contribution_id, auth, user, order, ops


def test_unverified_upload_cannot_be_published_or_funded(client, make_user):
    submit(client, valid_form())
    with db.transaction() as conn:
        bill = conn.execute("select * from bills").fetchone()
        reviewer = operators(conn)["reviewer"]
    response = client.post(f"/admin/ops/bills/{bill['id']}/approve", headers=headers(reviewer), json={
        "story": "A public story without personal information", "amount_due_cents": 21250,
        "private_information_removed": True,
    })
    assert response.status_code == 409
    with db.transaction() as conn:
        conn.execute("update bills set status = 'live', amount_due_cents = 21250 where id = %s", (bill["id"],))
    giver, _ = make_user()
    assert client.get("/api/feed", headers=giver).json()["bills"] == []
    assert client.post(f"/api/bills/{bill['id']}/contributions", headers=giver, json={"amount_cents": 500}).status_code == 409
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from contributions").fetchone()["n"] == 0


def test_shared_admin_cannot_access_trust_operations(client):
    assert client.get("/admin/ops/state", auth=ADMIN_AUTH).status_code == 403


def test_staff_console_and_login_are_usable_and_not_cached(client):
    with db.transaction() as conn:
        ops = operators(conn)
    login = client.post("/admin/login", data={"username": "reviewer", "password": PASSWORD}, follow_redirects=False)
    assert login.status_code == 303
    assert "httponly" in login.headers["set-cookie"].lower()
    page = client.get("/admin/ops")
    assert page.status_code == 200 and "Verify and publish a bill" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/static/operations.js").status_code == 200
    assert client.get("/admin/ops/state").status_code == 200
    response = client.post("/admin/ops/billers/00000000-0000-0000-0000-000000000000/suspend", json={})
    assert response.status_code == 403  # Missing per-operator CSRF token.
    token = headers(ops["reviewer"])["X-CSRF-Token"]
    assert client.post("/admin/logout", headers={"X-CSRF-Token": token}, follow_redirects=False).status_code == 303
    assert client.get("/admin/ops/state").status_code == 401


def test_staff_mfa_rejects_replay(client, monkeypatch):
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    with db.transaction() as conn:
        reviewer = operators(conn)["reviewer"]
        conn.execute("update staff set totp_ciphertext = %s where id = %s", (vault.encrypt(secret), reviewer["id"]))
    monkeypatch.setattr(staff.time, "time", lambda: 1234567890)
    code = staff.totp(secret, 1234567890 // 30)
    first = client.post("/admin/login", data={"username": "reviewer", "password": PASSWORD, "code": code}, follow_redirects=False)
    second = client.post("/admin/login", data={"username": "reviewer", "password": PASSWORD, "code": code}, follow_redirects=False)
    assert (first.status_code, second.status_code) == (303, 401)


def test_biller_requires_second_person(client):
    with db.transaction() as conn:
        ops = operators(conn)
        biller = trust.create_biller(conn, ops["reviewer"], legal_name="Example Utility", category="utilities",
                                     official_domain="utility.example", portal_url="https://utility.example/pay",
                                     evidence_id=proof(conn, "biller", "utility.example"))
        same_person = {**ops["reviewer"], "roles": ["payments"]}
        with pytest.raises(trust.TrustError, match="second person"):
            trust.approve_biller(conn, same_person, biller["id"])
        trust.approve_biller(conn, ops["payer"], biller["id"])


@pytest.mark.parametrize("url", ["http://utility.example/pay", "https://evil.example/pay", "https://utility.example.evil.com/pay",
                                  "https://user:password@utility.example/pay", "https://utility.example:bad/pay"])
def test_untrusted_payment_urls_are_rejected(url):
    with pytest.raises(trust.TrustError):
        trust.valid_portal(url, "utility.example")


def test_duplicate_debt_is_blocked_across_different_submissions(make_bill):
    first = make_bill(status="submitted")
    second = make_bill(status="submitted")
    with db.transaction() as conn:
        ops = operators(conn)
        v = conn.execute("select * from bill_verifications where bill_id = %s", (first,)).fetchone()
        evidence_id = proof(conn, "debt", second, 10000)
        with pytest.raises(trust.TrustError, match="already been submitted"):
            trust.verify_bill(conn, ops["reviewer"], second, biller_id=v["biller_id"],
                              account_reference=vault.decrypt(v["account_ciphertext"]), period_label=v["period_label"],
                              verified_balance_cents=10000, evidence_id=evidence_id)


def test_account_cannot_change_after_a_gift(client, make_user, make_bill):
    bill_id, _, _, _ = collected(client, make_user, make_bill)
    with db.transaction() as conn:
        v = conn.execute("select * from bill_verifications where bill_id = %s", (bill_id,)).fetchone()
        evidence_id = proof(conn, "debt", bill_id, 5000)
        with pytest.raises(trust.TrustError, match="cannot be reassigned"):
            trust.verify_bill(conn, operators(conn)["reviewer"], bill_id, biller_id=v["biller_id"],
                              account_reference="different-account", period_label=v["period_label"],
                              verified_balance_cents=5000, evidence_id=evidence_id)


def test_full_payment_has_independent_proof_and_balanced_ledger(client, make_user, make_bill):
    bill_id, contribution_id, auth, _ = collected(client, make_user, make_bill)
    receipt = client.get(f"/api/contributions/{contribution_id}/receipt", headers=auth).json()
    assert receipt["payment_status"] is None
    assert client.get("/api/me", headers=auth).json()["impact"]["bills_completed"] == 0
    order = complete_payment(bill_id)
    receipt = client.get(f"/api/contributions/{contribution_id}/receipt", headers=auth).json()
    assert receipt["payment_status"] == "applied"
    assert receipt["bill_payment_cents"] == 5000
    assert receipt["contribution_cents"] == 5000
    assert not {"account_reference", "portal_url", "evidence_id", "source", "requester_email"}.intersection(receipt)
    with db.transaction() as conn:
        assert all(value == 0 for value in ledger.balances(conn, bill_id).values())
        assert conn.execute("select count(*) as n from ledger_journals where bill_id = %s", (bill_id,)).fetchone()["n"] == 4
        with pytest.raises(trust.TrustError):
            trust.record_posting(conn, operators(conn)["treasury"], order["id"], proof(conn, "posting", order["id"], 5000))
    stranger, _ = make_user()
    assert client.get(f"/api/contributions/{contribution_id}/receipt", headers=stranger).status_code == 404


def test_collection_does_not_make_funds_available(client, make_user, make_bill):
    bill_id, _, _, _ = collected(client, make_user, make_bill)
    with db.transaction() as conn:
        with pytest.raises(trust.TrustError, match="Reconcile"):
            trust.prepare_payment(conn, operators(conn)["reviewer"], bill_id)
        assert ledger.balances(conn, bill_id)["collection_receivable"] == 5000


def test_only_treasury_can_reconcile_and_evidence_must_match(client, make_user, make_bill):
    _, contribution_id, _, _ = collected(client, make_user, make_bill)
    with db.transaction() as conn:
        ops = operators(conn)
        bad = proof(conn, "funding", contribution_id, 4999)
    response = client.post(f"/admin/ops/contributions/{contribution_id}/reconcile", headers=headers(ops["reviewer"]), json={"evidence_id": str(bad)})
    assert response.status_code == 403
    response = client.post(f"/admin/ops/contributions/{contribution_id}/reconcile", headers=headers(ops["treasury"]), json={"evidence_id": str(bad)})
    assert response.status_code == 409


def test_self_release_and_stale_balance_are_rejected(client, make_user, make_bill):
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    with db.transaction() as conn:
        fresh = proof(conn, "balance", bill_id, 5000)
        with pytest.raises(trust.TrustError, match="second person"):
            trust.release_payment(conn, {**ops["reviewer"], "roles": ["payments"]}, order["id"], fresh)
        stale = proof(conn, "balance", bill_id, 5000, observed_at=datetime.now(timezone.utc) - timedelta(minutes=16))
        with pytest.raises(trust.TrustError, match="stale"):
            trust.release_payment(conn, ops["payer"], order["id"], stale)


def test_destination_change_invalidates_prepared_instruction(client, make_user, make_bill):
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    with db.transaction() as conn:
        v = conn.execute("select * from bill_verifications where id = %s", (order["verification_id"],)).fetchone()
        evidence_id = proof(conn, "biller", "biller.example")
        trust.replace_destination(conn, ops["reviewer"], v["biller_id"], portal_url="https://biller.example/new-pay", evidence_id=evidence_id)
        trust.approve_biller(conn, ops["payer"], v["biller_id"])
        with pytest.raises(trust.TrustError, match="current biller destination"):
            trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 5000))
    assert bill_row(bill_id)["status"] == "funded"


def test_claim_is_one_time_and_blocks_removal(client, make_user, make_bill):
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    with db.transaction() as conn:
        trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 5000))
    claim = client.post(f"/admin/ops/payments/{order['id']}/claim", headers=headers(ops["payer"]), json={})
    assert claim.status_code == 200 and "account_reference" in claim.json()
    assert claim.headers["cache-control"] == "no-store"
    assert client.post(f"/admin/ops/payments/{order['id']}/claim", headers=headers(ops["payer"]), json={}).status_code == 409
    with db.transaction() as conn:
        with pytest.raises(trust.TrustError, match="in progress"):
            bills.remove(conn, bill_id, "remove", 50)
        with pytest.raises(trust.TrustError, match="un-released"):
            trust.cancel_payment(conn, ops["payer"], order["id"])


def test_concurrent_preparations_only_create_one_order(client, make_user, make_bill):
    bill_id, contribution_id, _, _ = collected(client, make_user, make_bill)
    with db.transaction() as conn:
        ops = operators(conn)
        trust.reconcile_funding(conn, ops["treasury"], contribution_id, proof(conn, "funding", contribution_id, 5000))
    barrier = threading.Barrier(2)
    results = []
    def prepare():
        barrier.wait()
        try:
            with db.transaction() as conn:
                trust.prepare_payment(conn, ops["reviewer"], bill_id)
            results.append("created")
        except trust.TrustError:
            results.append("blocked")
    threads = [threading.Thread(target=prepare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == ["blocked", "created"]


def test_financial_records_and_instruction_are_immutable(client, make_user, make_bill):
    bill_id, _, _, _, order, _ = prepared(client, make_user, make_bill)
    for sql, args in [
        ("update payment_orders set amount_cents = 1 where id = %s", (order["id"],)),
        ("delete from ledger_journals where bill_id = %s", (bill_id,)),
        ("update audit_events set action = 'changed'", ()),
        ("delete from evidence", ()),
    ]:
        with pytest.raises(psycopg.errors.RaiseException):
            with db.transaction() as conn:
                conn.execute(sql, args)


def test_return_after_posting_refunds_original_giver_and_does_not_duplicate_payment(client, make_user, make_bill):
    bill_id, contribution_id, auth, _ = collected(client, make_user, make_bill)
    order = complete_payment(bill_id)
    with db.transaction() as conn:
        trust.record_return(conn, operators(conn)["treasury"], order["id"], proof(conn, "return", order["id"], 5000))
        assert ledger.balances(conn, bill_id)["program_cash"] == 5000
    assert client.get(f"/api/contributions/{contribution_id}/receipt", headers=auth).json()["payment_status"] == "returned"
    refund(FakeProvider(), contribution_id)
    refund(FakeProvider(), contribution_id)
    with db.transaction() as conn:
        assert all(value == 0 for value in ledger.balances(conn, bill_id).values())
        assert conn.execute("select status from contributions where id = %s", (contribution_id,)).fetchone()["status"] == "refunded"


def test_pending_refund_is_not_completed_or_shown_as_refunded(client, make_user, make_bill, settings):
    bill_id = make_bill()
    auth, user = make_user()
    with db.transaction() as conn:
        hold = bills.reserve_contribution(conn, bill_id=bill_id, user_id=user["id"], amount_cents=1000, provider_name="fake", settings=settings)
    class Pending(FakeProvider):
        def refund(self, payment_ref):
            return RefundResult("refund_pending_1", "pending", 999)
    _settle(Pending(), hold["id"], payment_ref="pi_pending", paid_amount_cents=999)
    with db.transaction() as conn:
        assert conn.execute("select status from contributions where id = %s", (hold["id"],)).fetchone()["status"] == "refund_pending"
        assert ledger.balances(conn, bill_id)["collection_receivable"] == 999
    assert client.get("/api/me/activity", headers=auth).json()["events"] == []
    refund(FakeProvider(), hold["id"])
    receipt = client.get(f"/api/contributions/{hold['id']}/receipt", headers=auth).json()
    assert receipt["contribution_cents"] == 999
    assert client.get("/api/me/activity", headers=auth).json()["events"][0]["amount_cents"] == 999
    with db.transaction() as conn:
        assert all(value == 0 for value in ledger.balances(conn, bill_id).values())


def test_reverification_requires_new_review_and_preserves_debt_identity(client, make_user, make_bill):
    bill_id = make_bill()
    auth, _ = make_user()
    with db.transaction() as conn:
        v = conn.execute("select * from bill_verifications where bill_id = %s", (bill_id,)).fetchone()
        reviewer = operators(conn)["reviewer"]
        trust.verify_bill(conn, reviewer, bill_id, biller_id=v["biller_id"], account_reference=vault.decrypt(v["account_ciphertext"]),
                          period_label=v["period_label"], verified_balance_cents=10000, evidence_id=proof(conn, "debt", bill_id, 10000))
    assert client.get("/api/feed", headers=auth).json()["bills"] == []
    with db.transaction() as conn:
        bills.approve(conn, bill_id, amount_due_cents=10000, biller_name="ignored", story="Reviewed public story", max_live=50, actor=reviewer)
    assert len(client.get("/api/feed", headers=auth).json()["bills"]) == 1


def test_independent_non_execution_can_resolve_a_claim(client, make_user, make_bill):
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    with db.transaction() as conn:
        trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 5000))
        trust.claim_execution(conn, ops["payer"], order["id"])
        evidence_id = proof(conn, "non_execution", order["id"], 5000)
        with pytest.raises(trust.TrustError, match="Independent treasury"):
            trust.confirm_not_executed(conn, {**ops["payer"], "roles": ["treasury"]}, order["id"], evidence_id)
        trust.confirm_not_executed(conn, ops["treasury"], order["id"], evidence_id)
        assert ledger.balances(conn, bill_id)["program_cash"] == 5000
        replacement = trust.prepare_payment(conn, ops["reviewer"], bill_id)
        assert replacement["id"] != order["id"]


def test_lower_fresh_balance_cannot_be_overpaid(client, make_user, make_bill):
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    with db.transaction() as conn:
        with pytest.raises(trust.TrustError, match="does not support"):
            trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 4999))


def test_late_claim_rechecks_source_balance_age(client, make_user, make_bill):
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    with db.transaction() as conn:
        trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 5000))
    with db.transaction() as conn:
        stale = proof(conn, "balance", bill_id, 5000, observed_at=datetime.now(timezone.utc) - timedelta(minutes=16))
        conn.execute("update payment_orders set balance_evidence_id = %s where id = %s", (stale, order["id"]))
        with pytest.raises(trust.TrustError, match="stale"):
            trust.claim_execution(conn, ops["payer"], order["id"])


def test_database_cannot_bind_instruction_to_another_bills_verification(make_bill):
    first, second = make_bill(), make_bill()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.transaction() as conn:
            verification = conn.execute("select verification_id from bills where id = %s", (first,)).fetchone()["verification_id"]
            conn.execute("update bills set verification_id = %s where id = %s", (verification, second))


def test_runtime_role_can_post_but_cannot_rewrite_or_delete_financial_records(client, make_user, make_bill):
    from pathlib import Path
    from uuid import uuid4

    from psycopg import sql
    bill_id, _, _, _, order, ops = prepared(client, make_user, make_bill)
    role = f"paylee_test_{uuid4().hex}"
    with db.transaction() as conn:
        identifier = sql.Identifier(role)
        conn.execute(sql.SQL("create role {} nologin").format(identifier))
        grants = Path(__file__).parents[1].joinpath("runtime-grants.sql").read_text()
        conn.execute(grants.replace(':"runtime_role"', identifier.as_string(conn)))
        balance = proof(conn, "balance", bill_id, 5000)
        conn.execute(sql.SQL("set local role {}").format(identifier))
        trust.release_payment(conn, ops["payer"], order["id"], balance)
        trust.claim_execution(conn, ops["payer"], order["id"])
        for statement in ("delete from ledger_journals", "update evidence set source = 'tampered'",
                          "update staff set roles = array['treasury']"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
        conn.execute("reset role")
        conn.execute(sql.SQL("drop owned by {}").format(identifier))
        conn.execute(sql.SQL("drop role {}").format(identifier))
