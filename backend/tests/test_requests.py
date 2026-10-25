import psycopg
import pytest

from app import bills, db, documents, ledger, ratelimit, refunds, trust, vault
from app.api import _settle
from app.payments import FakeProvider

from .conftest import bill_row
from .test_web import PNG_BYTES, valid_form
from .trust_helpers import headers as staff_headers
from .trust_helpers import operators, proof


def submit(client, auth, **overrides):
    values = valid_form()
    values.pop("amount")
    values["amount_cents"] = "21250"
    values.update(overrides)
    return client.post("/api/requests", headers=auth, data=values,
                       files={"bill_file": ("bill.png", PNG_BYTES, "image/png")})


@pytest.fixture
def owned(make_user, make_bill):
    def create(*, amount=10000, status="live"):
        auth, owner = make_user()
        bill_id = make_bill(amount_due_cents=amount, status=status)
        with db.transaction() as conn:
            conn.execute("update bills set requester_user_id = %s where id = %s", (owner["id"], bill_id))
        return auth, owner, bill_id
    return create


def give(client, auth, bill_id, amount):
    response = client.post(f"/api/bills/{bill_id}/contributions", headers=auth, json={"amount_cents": amount})
    assert response.status_code == 201, response.text
    return response.json()["contribution"]["id"]


def test_submission_is_owned_private_and_quarantined(client, make_user):
    auth, owner = make_user()
    response = submit(client, auth, requester_user_id="00000000-0000-0000-0000-000000000000")
    assert response.status_code == 201, response.text
    request = response.json()
    assert request["status"] == "submitted" and request["verified_amount_cents"] is None
    assert request["city"] == "Lexington" and request["state"] == "KY"
    assert request["can_withdraw"] and request["can_update_balance"]
    with db.transaction() as conn:
        row = conn.execute("select * from bills where id = %s", (request["id"],)).fetchone()
        assert str(row["requester_user_id"]) == owner["id"]
        doc = conn.execute("select * from documents where id = %s", (row["document_id"],)).fetchone()
        assert doc["scan_status"] == "pending" and documents.read(doc) == PNG_BYTES
        assert conn.execute("select count(*) as n from work_jobs where kind = 'document'").fetchone()["n"] == 1
    assert client.get("/api/requests", headers=auth).json()["requests"] == [request]
    assert client.get(f"/api/requests/{request['id']}", headers=auth).json() == request
    assert client.get("/api/feed", headers=auth).json()["bills"] == []


def test_routes_require_authentication_and_ownership(client, make_user, owned):
    auth, _, bill_id = owned()
    stranger, _ = make_user()
    assert client.get("/api/requests").status_code == 401
    assert submit(client, {}).status_code == 401
    for headers, status in (({}, 401), (stranger, 404)):
        assert client.get(f"/api/requests/{bill_id}", headers=headers).status_code == status
        assert client.post(f"/api/requests/{bill_id}/withdraw", headers=headers).status_code == status
        assert client.post(f"/api/requests/{bill_id}/balance", headers=headers, json={"amount_cents": 100}).status_code == status
    assert client.get("/api/requests", headers=stranger).json()["requests"] == []
    assert client.get(f"/api/requests/{bill_id}", headers=auth).status_code == 200


def test_legacy_anonymous_uploads_are_not_claimed_by_email(client, make_user, make_bill):
    auth, _ = make_user()
    bill_id = make_bill()
    assert client.get("/api/requests", headers=auth).json()["requests"] == []
    assert client.get(f"/api/requests/{bill_id}", headers=auth).status_code == 404


def test_requester_projection_excludes_donors_internal_notes_and_feed_metrics(client, make_user, owned):
    auth, _, bill_id = owned()
    donor, _ = make_user()
    give(client, donor, bill_id, 2000)
    with db.transaction() as conn:
        conn.execute("update bills set review_note = 'PRIVATE FRAUD INVESTIGATION', times_shown = 987 where id = %s", (bill_id,))
    response = client.get(f"/api/requests/{bill_id}", headers=auth)
    forbidden = {"review_note", "requester_email", "upload_path", "document_id", "times_shown", "closer_user_id",
                 "user_id", "contributions", "passes", "reports", "account_ciphertext", "account_fingerprint"}
    assert forbidden.isdisjoint(response.json()) and "PRIVATE FRAUD INVESTIGATION" not in response.text
    assert response.json()["status_note"] is None and response.json()["amount_raised_cents"] == 2000
    assert response.json()["account_ending"] is not None


@pytest.mark.parametrize("field,value", [
    ("amount_cents", "1.5"), ("amount_cents", "-1"), ("amount_cents", "0"), ("amount_cents", "2500001"),
    ("email", "bad"), ("consent", "false"), ("state", "XX"), ("category", "unknown"),
    ("first_name", ""), ("story", "Too short"), ("due_date", "not-a-date"),
])
def test_submission_returns_field_errors_without_storing_files(client, make_user, field, value):
    auth, _ = make_user()
    response = submit(client, auth, **{field: value})
    assert response.status_code == 422 and field in response.json()["detail"]
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from documents").fetchone()["n"] == 0


def test_missing_spoofed_and_large_attachments(client, make_user):
    auth, _ = make_user()
    values = valid_form()
    values["amount_cents"] = "21250"
    missing = client.post("/api/requests", headers=auth, data=values)
    assert missing.status_code == 422 and "bill_file" in missing.json()["detail"]
    spoof = client.post("/api/requests", headers=auth, data=values,
                        files={"bill_file": ("bill.png", b"<script>bad</script>", "image/png")})
    assert spoof.status_code == 422 and "bill_file" in spoof.json()["detail"]
    large = client.post("/api/requests", headers=auth, data=values,
                        files={"bill_file": ("large.png", PNG_BYTES + b"x" * 1200000, "image/png")})
    assert large.status_code == 201, large.text


def test_per_account_quota_survives_resetting_the_ip_limiter(client, make_user):
    auth, _ = make_user()
    for _ in range(5):
        ratelimit.submissions.reset()
        assert submit(client, auth).status_code == 201
    ratelimit.submissions.reset()
    assert submit(client, auth).status_code == 429


def test_self_funding_is_denied_even_after_role_switch_and_at_database_level(client, owned):
    auth, owner, bill_id = owned()
    client.put("/api/me/role", headers=auth, json={"role": "payer"})
    assert client.get("/api/feed", headers=auth).json()["bills"] == []
    response = client.post(f"/api/bills/{bill_id}/contributions", headers=auth, json={"amount_cents": 1000})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "own_bill"
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction() as conn:
            conn.execute("""insert into contributions (bill_id, user_id, amount_cents, provider, expires_at)
                            values (%s, %s, 1000, 'fake', now() + interval '1 hour')""", (bill_id, owner["id"]))


def test_withdrawal_queues_original_source_refunds_idempotently(client, make_user, owned):
    auth, _, bill_id = owned()
    donor, _ = make_user()
    contribution = give(client, donor, bill_id, 2000)
    response = client.post(f"/api/requests/{bill_id}/withdraw", headers=auth)
    assert response.status_code == 200 and response.json()["status"] == "withdrawn"
    assert not response.json()["can_withdraw"] and not response.json()["can_update_balance"]
    assert client.post(f"/api/requests/{bill_id}/withdraw", headers=auth).status_code == 200
    with db.transaction() as conn:
        assert conn.execute("select status from contributions where id = %s", (contribution,)).fetchone()["status"] == "refund_pending"
        assert conn.execute("select count(*) as n from work_jobs where kind = 'refund'").fetchone()["n"] == 1
    refunds.process(FakeProvider(), contribution)
    with db.transaction() as conn:
        assert all(n == 0 for n in ledger.balances(conn, bill_id).values())


def test_submitted_request_can_be_withdrawn_without_approval(client, make_user):
    auth, _ = make_user()
    bill_id = submit(client, auth).json()["id"]
    response = client.post(f"/api/requests/{bill_id}/withdraw", headers=auth)
    assert response.json()["status"] == "withdrawn" and bill_row(bill_id)["amount_due_cents"] is None


def test_late_checkout_after_withdrawal_is_refunded(client, owned, make_user, settings):
    auth, _, bill_id = owned()
    _, donor = make_user()
    with db.transaction() as conn:
        held = bills.reserve_contribution(conn, bill_id=bill_id, user_id=donor["id"], amount_cents=1000,
                                          provider_name="fake", settings=settings)
    assert client.post(f"/api/requests/{bill_id}/withdraw", headers=auth).status_code == 200
    assert _settle(FakeProvider(), held["id"], payment_ref="fake_late_withdrawal", paid_amount_cents=1000).outcome == "refund"
    with db.transaction() as conn:
        assert conn.execute("select status from contributions where id = %s", (held["id"],)).fetchone()["status"] == "refunded"
        assert conn.execute("select amount_raised_cents from bills where id = %s", (bill_id,)).fetchone()["amount_raised_cents"] == 0


def test_balance_report_pauses_money_without_changing_verified_amount(client, owned, make_user):
    auth, _, bill_id = owned()
    donor, _ = make_user()
    give(client, donor, bill_id, 2000)
    response = client.post(f"/api/requests/{bill_id}/balance", headers=auth,
                           json={"amount_cents": 5000, "note": "Private: I paid part myself."})
    assert response.status_code == 200 and response.json()["status"] == "in_review"
    assert response.json()["verified_amount_cents"] == 10000 and response.json()["amount_raised_cents"] == 2000
    assert "Private:" not in response.text
    stranger, _ = make_user()
    assert client.get("/api/feed", headers=stranger).json()["bills"] == []
    assert client.post(f"/api/bills/{bill_id}/contributions", headers=stranger, json={"amount_cents": 500}).status_code == 409
    with db.transaction() as conn:
        report = conn.execute("select * from balance_reports").fetchone()
        assert vault.decrypt(report["note_ciphertext"]) == "Private: I paid part myself."
        reviewer = operators(conn)["reviewer"]
    assert "Private:" in client.get(f"/admin/ops/bills/{bill_id}/balance-reports", headers=staff_headers(reviewer)).text
    assert client.get(f"/admin/ops/bills/{bill_id}/balance-reports", headers=auth).status_code == 401


def test_zero_balance_allowed_but_not_increases_and_duplicate_reports_are_idempotent(client, owned):
    auth, _, bill_id = owned()
    path = f"/api/requests/{bill_id}/balance"
    for amount, status in ((10001, 409), (5000, 200), (6000, 409), (0, 200), (0, 200), (0.5, 422)):
        assert client.post(path, headers=auth, json={"amount_cents": amount}).status_code == status
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from balance_reports").fetchone()["n"] == 2


def test_only_fresh_independent_verification_and_review_can_resume_funding(client, owned, make_user):
    auth, _, bill_id = owned()
    donor, _ = make_user()
    give(client, donor, bill_id, 2000)
    with db.transaction() as conn:
        old = conn.execute("select * from bill_verifications where bill_id = %s", (bill_id,)).fetchone()
        ops = operators(conn)
    client.post(f"/api/requests/{bill_id}/balance", headers=auth, json={"amount_cents": 5000})
    with db.transaction() as conn:
        kwargs = dict(biller_id=old["biller_id"], account_reference=vault.decrypt(old["account_ciphertext"]), period_label=old["period_label"])
        with pytest.raises(trust.TrustError, match="fresh biller confirmation"):
            trust.verify_bill(conn, ops["reviewer"], bill_id, **kwargs, verified_balance_cents=10000, evidence_id=old["evidence_id"])
        trust.verify_bill(conn, ops["reviewer"], bill_id, **kwargs, verified_balance_cents=5000, evidence_id=proof(conn, "debt", bill_id, 5000))
    stranger, _ = make_user()
    assert client.get("/api/feed", headers=stranger).json()["bills"] == []
    with db.transaction() as conn:
        assert bills.approve(conn, bill_id, amount_due_cents=5000, biller_name="ignored", story="Public reviewed story", max_live=50, actor=ops["reviewer"])
    assert bill_row(bill_id)["status"] == "live" and bill_row(bill_id)["amount_due_cents"] == 5000
    assert len(client.get("/api/feed", headers=stranger).json()["bills"]) == 1


def test_released_payment_cannot_be_withdrawn_or_executed_after_balance_report(client, owned, make_user):
    auth, _, bill_id = owned(amount=5000)
    donor, _ = make_user()
    contribution = give(client, donor, bill_id, 5000)
    with db.transaction() as conn:
        ops = operators(conn)
        trust.reconcile_funding(conn, ops["treasury"], contribution, proof(conn, "funding", contribution, 5000))
        order = trust.prepare_payment(conn, ops["reviewer"], bill_id)
        trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 5000))
    response = client.post(f"/api/requests/{bill_id}/balance", headers=auth, json={"amount_cents": 4000})
    assert response.status_code == 200 and not response.json()["can_withdraw"]
    assert client.post(f"/api/requests/{bill_id}/withdraw", headers=auth).status_code == 409
    with db.transaction() as conn:
        with pytest.raises(trust.TrustError, match="balance change"):
            trust.claim_execution(conn, ops["payer"], order["id"])
        assert conn.execute("select status from payment_orders where id = %s", (order["id"],)).fetchone()["status"] == "released"


def test_ownership_cannot_be_transferred(owned, make_user):
    _, _, bill_id = owned()
    _, stranger = make_user()
    with pytest.raises(psycopg.errors.RaiseException, match="ownership"):
        with db.transaction() as conn:
            conn.execute("update bills set requester_user_id = %s where id = %s", (stranger["id"], bill_id))


def test_concurrent_withdrawal_and_collection_leave_no_money_applied(client, owned, make_user, settings):
    import threading

    from app import requests
    _, owner, bill_id = owned()
    _, donor = make_user()
    with db.transaction() as conn:
        held = bills.reserve_contribution(conn, bill_id=bill_id, user_id=donor["id"], amount_cents=2000,
                                          provider_name="fake", settings=settings)
    start = threading.Barrier(2)
    errors = []
    def withdraw():
        try:
            start.wait()
            with db.transaction() as conn:
                requests.withdraw(conn, owner["id"], bill_id)
        except Exception as error:
            errors.append(error)
    def collect():
        try:
            start.wait()
            _settle(FakeProvider(), held["id"], payment_ref="fake_concurrent_withdrawal", paid_amount_cents=2000)
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=withdraw), threading.Thread(target=collect)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    refunds.process(FakeProvider(), held["id"])
    assert bill_row(bill_id)["status"] == "withdrawn"
    with db.transaction() as conn:
        assert all(n == 0 for n in ledger.balances(conn, bill_id).values())
        assert conn.execute("select count(*) as n from payment_orders").fetchone()["n"] == 0


def test_balance_change_cancels_only_a_preparation_and_excess_can_be_refunded(client, owned, make_user):
    auth, _, bill_id = owned(amount=5000)
    donor, _ = make_user()
    contribution = give(client, donor, bill_id, 5000)
    with db.transaction() as conn:
        ops = operators(conn)
        trust.reconcile_funding(conn, ops["treasury"], contribution, proof(conn, "funding", contribution, 5000))
        order = trust.prepare_payment(conn, ops["reviewer"], bill_id)
    response = client.post(f"/api/requests/{bill_id}/balance", headers=auth, json={"amount_cents": 4000})
    assert response.status_code == 200 and response.json()["can_withdraw"]
    with db.transaction() as conn:
        assert conn.execute("select status from payment_orders where id = %s", (order["id"],)).fetchone()["status"] == "canceled"
        v = conn.execute("select * from bill_verifications where bill_id = %s", (bill_id,)).fetchone()
        with pytest.raises(trust.TrustError, match="excess funding"):
            trust.verify_bill(conn, ops["reviewer"], bill_id, biller_id=v["biller_id"],
                account_reference=vault.decrypt(v["account_ciphertext"]), period_label=v["period_label"],
                verified_balance_cents=4000, evidence_id=proof(conn, "debt", bill_id, 4000))
    assert client.post(f"/api/requests/{bill_id}/withdraw", headers=auth).status_code == 200
    refunds.process(FakeProvider(), contribution)


def test_canceled_checkout_cannot_be_applied_after_a_fresh_review(client, owned, make_user, settings):
    auth, _, bill_id = owned()
    _, donor = make_user()
    with db.transaction() as conn:
        held = bills.reserve_contribution(conn, bill_id=bill_id, user_id=donor["id"], amount_cents=1000,
                                          provider_name="fake", settings=settings)
        old = conn.execute("select * from bill_verifications where bill_id = %s", (bill_id,)).fetchone()
        reviewer = operators(conn)["reviewer"]
    client.post(f"/api/requests/{bill_id}/balance", headers=auth, json={"amount_cents": 5000})
    with db.transaction() as conn:
        trust.verify_bill(conn, reviewer, bill_id, biller_id=old["biller_id"], account_reference=vault.decrypt(old["account_ciphertext"]),
            period_label=old["period_label"], verified_balance_cents=5000, evidence_id=proof(conn, "debt", bill_id, 5000))
        bills.approve(conn, bill_id, amount_due_cents=5000, biller_name="ignored", story="Public reviewed story", max_live=50, actor=reviewer)
    assert bill_row(bill_id)["status"] == "live"
    assert _settle(FakeProvider(), held["id"], payment_ref="fake_canceled_old_checkout", paid_amount_cents=1000).outcome == "refund"
    assert bill_row(bill_id)["amount_raised_cents"] == 0


def test_resubmitting_a_withdrawn_debt_waits_for_all_refunds(client, owned, make_user, make_bill):
    auth, _, original = owned()
    donor, _ = make_user()
    contribution = give(client, donor, original, 2000)
    client.post(f"/api/requests/{original}/withdraw", headers=auth)
    replacement = make_bill(status="submitted")
    with db.transaction() as conn:
        old = conn.execute("select * from bill_verifications where bill_id = %s", (original,)).fetchone()
        reviewer = operators(conn)["reviewer"]
        kwargs = dict(biller_id=old["biller_id"], account_reference=vault.decrypt(old["account_ciphertext"]), period_label=old["period_label"],
                      verified_balance_cents=10000, evidence_id=proof(conn, "debt", replacement, 10000))
        with pytest.raises(trust.TrustError, match="already been submitted"):
            trust.verify_bill(conn, reviewer, replacement, **kwargs)
    refunds.process(FakeProvider(), contribution)
    with db.transaction() as conn:
        trust.verify_bill(conn, reviewer, replacement, **kwargs)
        assert conn.execute("select bill_id from debt_claims where fingerprint = %s", (old["invoice_fingerprint"],)).fetchone()["bill_id"] == bill_row(replacement)["id"]


def test_reverification_does_not_reopen_an_expired_posting(client, owned, make_user):
    from datetime import date, timedelta
    auth, _, bill_id = owned(status="expired")
    with db.transaction() as conn:
        conn.execute("update bills set due_date = %s where id = %s", (date.today() - timedelta(days=20), bill_id))
        old = conn.execute("select * from bill_verifications where bill_id = %s", (bill_id,)).fetchone()
        reviewer = operators(conn)["reviewer"]
    assert client.post(f"/api/requests/{bill_id}/balance", headers=auth, json={"amount_cents": 5000}).status_code == 200
    with db.transaction() as conn:
        trust.verify_bill(conn, reviewer, bill_id, biller_id=old["biller_id"], account_reference=vault.decrypt(old["account_ciphertext"]),
            period_label=old["period_label"], verified_balance_cents=5000, evidence_id=proof(conn, "debt", bill_id, 5000))
        bills.approve(conn, bill_id, amount_due_cents=5000, biller_name="ignored", story="Public reviewed story", max_live=50, actor=reviewer)
    assert bill_row(bill_id)["status"] == "expired"


def test_balance_report_during_execution_preserves_payment_facts_and_review_hold(client, owned, make_user):
    auth, _, bill_id = owned(amount=5000)
    donor, _ = make_user()
    contribution = give(client, donor, bill_id, 5000)
    with db.transaction() as conn:
        ops = operators(conn)
        trust.reconcile_funding(conn, ops["treasury"], contribution, proof(conn, "funding", contribution, 5000))
        order = trust.prepare_payment(conn, ops["reviewer"], bill_id)
        trust.release_payment(conn, ops["payer"], order["id"], proof(conn, "balance", bill_id, 5000))
        trust.claim_execution(conn, ops["payer"], order["id"])
    assert client.post(f"/api/requests/{bill_id}/balance", headers=auth, json={"amount_cents": 4000}).status_code == 200
    with db.transaction() as conn:
        trust.record_submission(conn, ops["payer"], order["id"], proof(conn, "submission", order["id"], 5000), "external-before-balance-arrived")
        trust.record_settlement(conn, ops["treasury"], order["id"], proof(conn, "settlement", order["id"], 5000))
        trust.record_posting(conn, ops["treasury"], order["id"], proof(conn, "posting", order["id"], 5000))
    request = client.get(f"/api/requests/{bill_id}", headers=auth).json()
    assert request["status"] == "in_review" and not request["can_withdraw"]
    assert "reconciling" in request["status_note"]
    assert client.get(f"/api/contributions/{contribution}/receipt", headers=donor).json()["payment_status"] == "applied"
    assert client.get("/api/me", headers=donor).json()["impact"]["bills_completed"] == 0
