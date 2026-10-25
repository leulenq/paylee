import pytest

from app.config import Settings


def test_requests_without_a_valid_session_are_rejected(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_dev_login_is_stable_per_device(client):
    first = client.post("/api/auth/dev", json={"device_id": "simulator-1234"}).json()
    second = client.post("/api/auth/dev", json={"device_id": "simulator-1234"}).json()

    assert first["user"] == second["user"]
    assert first["user"]["handle"]


def test_dev_login_can_be_disabled(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "allow_dev_login", False)
    assert client.post("/api/auth/dev", json={"device_id": "simulator-1234"}).status_code == 404


def test_forged_apple_token_is_rejected(client):
    response = client.post("/api/auth/apple", json={"identity_token": "eyJhbGciOiJub25lIn0.e30."})
    assert response.status_code == 401


def test_deleting_an_account_ends_its_sessions(client, make_user):
    headers, _ = make_user()

    assert client.delete("/api/me", headers=headers).status_code == 204
    assert client.get("/api/me", headers=headers).status_code == 401


def test_production_refuses_development_defaults():
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        Settings(environment="production").check_safe_for_environment()

    Settings(
        environment="production",
        schema_auto_apply=False,
        session_secret="x" * 48,
        admin_password="a long admin password",
        allow_dev_login=False,
        payment_provider="stripe",
        email_provider="smtp",
        public_base_url="https://paylee.example",
        stripe_secret_key="sk_test_approved_program",
        stripe_webhook_secret="whsec_test",
        smtp_host="smtp.example",
        email_from="help@paylee.example",
        live_payments_approved=True,
        funds_program_reference="test-approved-program",
        data_encryption_key="eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
        account_fingerprint_key="f" * 48,
        storage_provider="s3",
        s3_bucket="paylee-test",
        s3_kms_key_id="test-kms-key",
        malware_scanner="clamav",
    ).check_safe_for_environment()


def test_impact_and_badges(client, make_user, make_bill):
    headers, _ = make_user()
    partial = make_bill(amount_due_cents=10_000)
    closing = make_bill(amount_due_cents=2_000)
    client.post(f"/api/bills/{partial}/contributions", json={"amount_cents": 1_000}, headers=headers)
    client.post(f"/api/bills/{closing}/contributions", json={"amount_cents": 2_000}, headers=headers)
    from .trust_helpers import complete_payment
    complete_payment(closing)

    me = client.get("/api/me", headers=headers).json()

    assert me["member_since"]
    assert me["impact"] == {
        "total_given_cents": 3_000, "bills_helped": 2, "bills_completed": 1, "bills_closed": 1,
    }
    earned = {b["id"] for b in me["badges"] if b["earned"]}
    assert earned == {"first_bill", "closer"}

    history = client.get("/api/me/contributions", headers=headers).json()["contributions"]
    assert [c["closed_by_me"] for c in history] == [True, False]


def test_leaderboard_ranks_bills_completed_not_dollars(client, make_user, make_bill):
    """The big giver gives $290 across 3 bills; the small giver gives $30 across 4. The small giver ranks first."""
    big_headers, big = make_user()
    small_headers, small = make_user()
    a, b, c = (make_bill(amount_due_cents=10_000) for _ in range(3))
    d, e = (make_bill(amount_due_cents=1_000) for _ in range(2))

    client.post(f"/api/bills/{a}/contributions", json={"amount_cents": 10_000}, headers=big_headers)
    for shared in (b, c):
        client.post(f"/api/bills/{shared}/contributions", json={"amount_cents": 500}, headers=small_headers)
        client.post(f"/api/bills/{shared}/contributions", json={"amount_cents": 9_500}, headers=big_headers)
    for small_bill in (d, e):
        client.post(f"/api/bills/{small_bill}/contributions", json={"amount_cents": 1_000}, headers=small_headers)
    from .trust_helpers import complete_payment
    for bill_id in (a, b, c, d, e):
        complete_payment(bill_id)

    board = client.get("/api/leaderboard?period=all", headers=small_headers).json()

    assert [(e["handle"], e["bills_completed"]) for e in board["entries"]] == [
        (small["handle"], 4), (big["handle"], 3),
    ]
    assert board["me"]["rank"] == 1
    assert all("cents" not in key for entry in board["entries"] for key in entry)
