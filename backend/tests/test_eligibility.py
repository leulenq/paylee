from datetime import date

import psycopg
import pytest

from app import bills, db, eligibility, staff, trust

from .conftest import bill_row
from .test_web import submit, valid_form
from .trust_helpers import (
    PASSWORD,
    bind_fixture,
    claim_fixture,
    complete_payment,
    hardship_fixture,
    identity_fixture,
    operators,
)


@pytest.fixture(autouse=True)
def no_random_audits(settings, monkeypatch):
    monkeypatch.setattr(settings, "eligibility_audit_rate", 0.0)


def pending(make_bill, amount, *, street="12 Oak Street", holder="Maria Garcia", user_id=None):
    """A verified, unpublished bill whose account holder the biller has confirmed."""
    bill_id = make_bill(amount_due_cents=amount, status="submitted", eligible=False)
    with db.transaction() as conn:
        if user_id:
            conn.execute("update bills set requester_user_id = %s where id = %s", (user_id, bill_id))
        claim_fixture(conn, bill_id, holder=holder, street=street, user_id=user_id)
        bind_fixture(conn, bill_id, holder=holder, street=street)
    return bill_id


def publish(bill_id, amount):
    with db.transaction() as conn:
        return bills.approve(conn, bill_id, amount_due_cents=amount, biller_name="",
                             story="A public story without any private details.", max_live=50,
                             actor=operators(conn)["reviewer"])


def published(bill_id):
    with db.transaction() as conn:
        return conn.execute("select * from bill_eligibility where bill_id = %s", (bill_id,)).fetchone()


def second_reviewer(conn):
    row = conn.execute("select * from staff where username = 'reviewer2'").fetchone()
    return row or conn.execute("insert into staff (username, password_hash, roles) values ('reviewer2', %s, %s) returning *",
                               (staff.password_hash(PASSWORD), ["reviewer"])).fetchone()


def give(client, make_user, bill_id, amount):
    auth, _ = make_user()
    return client.post(f"/api/bills/{bill_id}/contributions", headers=auth, json={"amount_cents": amount})


# --- Matching rules -------------------------------------------------------------


@pytest.mark.parametrize("a, b, expected", [
    ("Maria Garcia", "GARCIA, MARIA L", True),
    ("Maria Lopez Garcia", "Maria Garcia", True),
    ("María García", "Maria Garcia", True),
    ("Maria Garcia", "Jose Garcia", False),
    ("Maria Garcia", "Maria Garcia Jose Smith", True),
    ("Garcia", "Maria Garcia", False),
    ("M Garcia", "Maria Garcia", False),
])
def test_names_match_requires_two_shared_names(a, b, expected):
    assert eligibility.names_match(a, b) is expected


def test_household_ignores_address_formatting_but_not_units():
    same = eligibility.household_fingerprint
    assert same("12 Oak Street Apt 2", "40508") == same("12 oak st #2", "40508-1234") == same("12 Oak St. Unit 2", "40508")
    assert same("12 Oak Street Apt 2", "40508") != same("12 Oak Street Apt 3", "40508")
    assert same("12 Oak Street", "40508") != same("12 Oak Street", "40509")


# --- Intake ---------------------------------------------------------------------


def test_web_intake_requires_holder_address_cause_and_signature(client):
    response = submit(client, valid_form(account_holder_name="", attest="", hardship_cause=""))
    assert response.status_code == 400
    assert "full name on the account" in response.text and "Confirm the statement" in response.text
    response = submit(client, valid_form(signature="Someone Else"))
    assert response.status_code == 400 and "The account must be in your name" in response.text
    assert submit(client, valid_form()).status_code == 200
    with db.transaction() as conn:
        claim = conn.execute("select * from eligibility_claims").fetchone()
    assert claim["hardship_cause"] == "reduced_income" and "Garcia" not in str(dict(claim))


def test_requester_api_claim_and_next_steps(client, make_user, make_bill):
    auth, user = make_user()
    bill_id = make_bill(amount_due_cents=9000, status="submitted", eligible=False)
    with db.transaction() as conn:
        conn.execute("update bills set requester_user_id = %s where id = %s", (user["id"], bill_id))
    view = client.get(f"/api/requests/{bill_id}/eligibility", headers=auth).json()
    assert [step["code"] for step in view["steps"]] == ["claim"]
    body = {"account_holder_name": "Maria Garcia", "service_street": "12 Oak Street", "service_zip": "40508",
            "hardship_cause": "medical", "signature": "Maria Garcia", "attest": True}
    assert client.put(f"/api/requests/{bill_id}/eligibility", headers=auth, json={**body, "signature": "Ana Ruiz"}).status_code == 422
    view = client.put(f"/api/requests/{bill_id}/eligibility", headers=auth, json=body).json()
    assert view["claim_submitted"] and [step["code"] for step in view["steps"]] == ["account_holder"]
    other, _ = make_user()
    assert client.get(f"/api/requests/{bill_id}/eligibility", headers=other).status_code == 404
    assert client.put(f"/api/requests/{bill_id}/eligibility", headers=other, json=body).status_code == 404


# --- Bindings -------------------------------------------------------------------


def test_biller_holder_or_address_mismatch_is_refused(make_bill):
    bill_id = make_bill(amount_due_cents=9000, status="submitted", eligible=False)
    with db.transaction() as conn:
        claim_fixture(conn, bill_id, street="12 Oak Street")
    with pytest.raises(trust.TrustError, match="account holder does not match"), db.transaction() as conn:
        bind_fixture(conn, bill_id, holder="Jose Garcia", street="12 Oak Street")
    with pytest.raises(trust.TrustError, match="service address does not match"), db.transaction() as conn:
        bind_fixture(conn, bill_id, street="14 Oak Street")
    with pytest.raises(trust.TrustError, match="account holder"):
        publish(bill_id, 9000)


def test_a_new_claim_after_binding_requires_rebinding(make_bill):
    bill_id = pending(make_bill, 9000)
    with db.transaction() as conn:
        claim_fixture(conn, bill_id, holder="Ana Ruiz", street="99 Elm Street")
    with pytest.raises(trust.TrustError, match="account holder"):
        publish(bill_id, 9000)


def test_legacy_published_bill_without_eligibility_cannot_take_gifts(client, make_user, make_bill):
    bill_id = make_bill(amount_due_cents=5000, eligible=False)
    response = give(client, make_user, bill_id, 5000)
    assert response.status_code == 409
    giver, _ = make_user()
    assert client.get("/api/feed", headers=giver).json()["bills"] == []


# --- Assurance levels and caps ----------------------------------------------------


def test_small_bill_publishes_on_attestation_without_documents(client, make_user, make_bill):
    bill_id = pending(make_bill, 9000)
    assert publish(bill_id, 9000)
    row = published(bill_id)
    assert (row["level"], row["hardship_method"], row["identity_check_id"]) == ("attested", "attestation", None)
    assert give(client, make_user, bill_id, 9000).status_code == 201


def test_splitting_across_bills_accounts_or_requester_accounts_escalates(make_bill, make_user):
    _, first_user = make_user()
    _, second_user = make_user()
    first = pending(make_bill, 9000, user_id=first_user["id"])
    assert publish(first, 9000)
    # Different account number, different requester account, same household: still counted.
    second = pending(make_bill, 9000, user_id=second_user["id"])
    with pytest.raises(trust.TrustError, match="identity check"):
        publish(second, 9000)
    with db.transaction() as conn:
        identity_fixture(conn, second)
    with pytest.raises(trust.TrustError, match="Third-party hardship"):
        publish(second, 9000)
    with db.transaction() as conn:
        hardship_fixture(conn, second, method="biller_program")
    assert publish(second, 9000)
    assert (published(second)["level"], published(second)["trailing_cents"]) == ("identified", 9000)


def test_documented_level_needs_person_level_hardship(make_bill):
    bill_id = pending(make_bill, 70_000)
    with db.transaction() as conn:
        identity_fixture(conn, bill_id)
        hardship_fixture(conn, bill_id, method="biller_program")
    with pytest.raises(trust.TrustError, match="Third-party hardship"):
        publish(bill_id, 70_000)
    with db.transaction() as conn:
        hardship_fixture(conn, bill_id, method="shock")
    assert publish(bill_id, 70_000)
    assert published(bill_id)["level"] == "documented"


def test_identity_or_hardship_for_someone_else_does_not_count(make_bill):
    bill_id = pending(make_bill, 20_000)
    with db.transaction() as conn:
        identity_fixture(conn, bill_id, legal_name="Ana Ruiz")
        hardship_fixture(conn, bill_id, method="benefits", legal_name="Maria Garcia")
    with pytest.raises(trust.TrustError, match="identity check"):
        publish(bill_id, 20_000)
    with db.transaction() as conn:
        identity_fixture(conn, bill_id)
        hardship_fixture(conn, bill_id, method="income", legal_name="Ana Ruiz")
    assert publish(bill_id, 20_000)
    assert published(bill_id)["hardship_method"] == "benefits"


def test_annual_household_limit(make_bill, settings, monkeypatch):
    monkeypatch.setattr(settings, "eligibility_annual_limit_cents", 100_000)
    first = pending(make_bill, 70_000)
    second = pending(make_bill, 40_000)
    for bill_id in (first, second):
        with db.transaction() as conn:
            identity_fixture(conn, bill_id)
            hardship_fixture(conn, bill_id)
    assert publish(first, 70_000)
    with pytest.raises(trust.TrustError, match="yearly limit"):
        publish(second, 40_000)


def test_refunded_bills_stop_counting_toward_caps(make_bill):
    first = pending(make_bill, 12_000)
    assert publish(first, 12_000)
    with db.transaction() as conn:
        bills.remove(conn, first, "Requester asked", 50)
    second = pending(make_bill, 12_000)
    assert publish(second, 12_000)
    assert published(second)["level"] == "attested"


def test_same_requester_in_a_new_household_needs_identity(make_bill, make_user):
    _, user = make_user()
    assert publish(pending(make_bill, 3000, user_id=user["id"]), 3000)
    moved = pending(make_bill, 3000, street="400 Pine Street", user_id=user["id"])
    with pytest.raises(trust.TrustError, match="identity check"):
        publish(moved, 3000)


def test_eligibility_is_frozen_at_publication(make_bill):
    bill_id = pending(make_bill, 9000)
    publish(bill_id, 9000)
    with pytest.raises(psycopg.errors.RaiseException), db.transaction() as conn:
        conn.execute("update bill_eligibility set level = 'documented' where bill_id = %s", (bill_id,))
    with pytest.raises(psycopg.errors.RaiseException), db.transaction() as conn:
        conn.execute("delete from eligibility_claims")


# --- Audits ---------------------------------------------------------------------


def test_selected_audit_allows_giving_but_blocks_payment_until_passed(client, make_user, make_bill, settings, monkeypatch):
    monkeypatch.setattr(settings, "eligibility_audit_rate", 1.0)
    bill_id = pending(make_bill, 5000)
    publish(bill_id, 5000)
    assert published(bill_id)["audit_status"] == "selected"
    assert give(client, make_user, bill_id, 5000).status_code == 201
    with pytest.raises(trust.TrustError, match="hardship audit"):
        complete_payment(bill_id)
    with db.transaction() as conn:
        check = hardship_fixture(conn, bill_id, method="benefits")
    with pytest.raises(trust.TrustError, match="different reviewer"), db.transaction() as conn:
        eligibility.resolve_audit(conn, operators(conn)["reviewer"], bill_id, outcome="passed", hardship_check_id=check["id"])
    with db.transaction() as conn:
        eligibility.resolve_audit(conn, second_reviewer(conn), bill_id, outcome="passed", hardship_check_id=check["id"])
    complete_payment(bill_id)
    assert bill_row(bill_id)["status"] == "paid"


def test_requester_learns_of_audit_but_givers_never_do(client, make_user, make_bill, settings, monkeypatch):
    monkeypatch.setattr(settings, "eligibility_audit_rate", 1.0)
    auth, user = make_user()
    bill_id = pending(make_bill, 5000, user_id=user["id"])
    publish(bill_id, 5000)
    view = client.get(f"/api/requests/{bill_id}/eligibility", headers=auth).json()
    assert view["steps"][0]["code"] == "hardship"
    giver, _ = make_user()
    public = client.get(f"/api/bills/{bill_id}", headers=giver).text
    assert "audit" not in public and "attest" not in public and "hardship" not in public


def test_failed_audit_refunds_and_bars_every_key(client, make_user, make_bill, settings, monkeypatch):
    monkeypatch.setattr(settings, "eligibility_audit_rate", 1.0)
    bill_id = pending(make_bill, 5000)
    publish(bill_id, 5000)
    monkeypatch.setattr(settings, "eligibility_audit_rate", 0.0)
    sibling = pending(make_bill, 4000, street="300 Birch Street")
    publish(sibling, 4000)
    assert give(client, make_user, bill_id, 2500).status_code == 201
    with db.transaction() as conn:
        eligibility.resolve_audit(conn, second_reviewer(conn), bill_id, outcome="failed")
    assert bill_row(bill_id)["status"] == "removed"
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from contributions where bill_id = %s and status = 'refund_pending'",
                            (bill_id,)).fetchone()["n"] == 1
    # The household cannot come back with another bill, even with full documentation.
    again = pending(make_bill, 3000)
    with db.transaction() as conn:
        identity_fixture(conn, again)
        hardship_fixture(conn, again)
    with pytest.raises(trust.TrustError, match="barred"):
        publish(again, 3000)
    # An unrelated household is unaffected.
    assert give(client, make_user, sibling, 4000).status_code == 201


def test_failed_audit_stops_other_live_bills_in_the_household(client, make_user, make_bill, settings, monkeypatch):
    first = pending(make_bill, 5000, street="5 Ash Street")
    publish(first, 5000)
    monkeypatch.setattr(settings, "eligibility_audit_rate", 1.0)
    second = pending(make_bill, 4000, street="5 Ash Street")
    publish(second, 4000)
    with db.transaction() as conn:
        eligibility.resolve_audit(conn, second_reviewer(conn), second, outcome="failed")
    assert give(client, make_user, first, 1000).status_code == 409
    giver, _ = make_user()
    assert client.get("/api/feed", headers=giver).json()["bills"] == []


def test_withdrawing_during_an_audit_bars_attestation_for_the_household(client, make_user, make_bill, settings, monkeypatch):
    monkeypatch.setattr(settings, "eligibility_audit_rate", 1.0)
    auth, user = make_user()
    bill_id = pending(make_bill, 5000, user_id=user["id"])
    publish(bill_id, 5000)
    assert client.post(f"/api/requests/{bill_id}/withdraw", headers=auth).status_code == 200
    assert published(bill_id)["audit_status"] == "abandoned"
    monkeypatch.setattr(settings, "eligibility_audit_rate", 0.0)
    again = pending(make_bill, 3000)
    with pytest.raises(trust.TrustError, match="Third-party hardship"):
        publish(again, 3000)
    with db.transaction() as conn:
        hardship_fixture(conn, again, method="biller_program")
    assert publish(again, 3000)


def test_ops_endpoints_record_eligibility(client, make_bill):
    from .trust_helpers import headers, proof

    bill_id = make_bill(amount_due_cents=20_000, status="submitted", eligible=False)
    with db.transaction() as conn:
        claim_fixture(conn, bill_id, street="77 Cedar Lane")
        reviewer = operators(conn)["reviewer"]
        holder_proof = proof(conn, "account_holder", bill_id)
        identity_proof = proof(conn, "identity", bill_id)
        hardship_proof = proof(conn, "hardship", bill_id)
    auth = headers(reviewer)
    status = client.get(f"/admin/ops/bills/{bill_id}/eligibility", headers=auth).json()
    assert status["missing"] == ["account_holder"]
    assert client.post(f"/admin/ops/bills/{bill_id}/account-holder", headers=auth, json={
        "evidence_id": str(holder_proof), "holder_name": "GARCIA MARIA", "service_street": "77 Cedar Ln",
        "service_zip": "40508"}).status_code == 201
    assert client.post(f"/admin/ops/bills/{bill_id}/identity", headers=auth, json={
        "evidence_id": str(identity_proof), "legal_name": "Maria Garcia", "date_of_birth": "1985-04-02"}).status_code == 201
    assert client.post(f"/admin/ops/bills/{bill_id}/hardship", headers=auth, json={
        "evidence_id": str(hardship_proof), "method": "benefits"}).status_code == 409
    assert client.post(f"/admin/ops/bills/{bill_id}/hardship", headers=auth, json={
        "evidence_id": str(hardship_proof), "method": "benefits", "legal_name": "Maria Garcia"}).status_code == 201
    status = client.get(f"/admin/ops/bills/{bill_id}/eligibility", headers=auth).json()
    assert status["missing"] == [] and status["level"] == "identified"
    assert client.post(f"/admin/ops/bills/{bill_id}/approve", headers=auth, json={
        "story": "A public story without private details.", "amount_due_cents": 20_000,
        "private_information_removed": True}).status_code == 200
    assert client.get("/admin/ops/state", headers=auth).json()["eligibility"][0]["level"] == "identified"


def test_identity_requires_adult_date_of_birth(make_bill):
    bill_id = pending(make_bill, 9000)
    with pytest.raises(trust.TrustError, match="adult date of birth"), db.transaction() as conn:
        identity_fixture(conn, bill_id, dob=date.today())
