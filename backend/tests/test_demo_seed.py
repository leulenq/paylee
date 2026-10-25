import pytest

from app import bills, db, profile
from app.config import Settings
from app.seed_demo import require_demo_environment, seed


def test_demo_account_has_a_swipe_deck_open_bills_and_payment_history(client, settings):
    with db.transaction() as conn:
        result = seed(conn, "device-demo-seed-test")
    with db.transaction() as conn:
        assert len(bills.feed(conn, result["user_id"], limit=20, settings=settings)) == 6
        assert len(profile.open_bills(conn, result["user_id"])) == 2
        assert bills.impact(conn, result["user_id"])["bills_completed"] == 3
        history = bills.my_contributions(conn, result["user_id"])
        assert len(history) == 7
        assert len([c for c in history if c["status"] == "refunded"]) == 1
        assert conn.execute("select count(*) as n from payment_orders where status = 'applied'").fetchone()["n"] == 3
        assert conn.execute("select count(*) as n from staff where disabled_at is null").fetchone()["n"] == 0
        assert all("Demo" in b["biller_name"] for b in bills.feed(conn, result["user_id"], limit=20, settings=settings))
    with db.transaction() as conn:
        repeated = seed(conn, "device-demo-seed-test")
        assert repeated["new_bills"] == 0
        assert conn.execute("select count(*) as n from bills").fetchone()["n"] == 13
        assert len(bills.my_contributions(conn, result["user_id"])) == 7


@pytest.mark.parametrize("overrides", [
    {"environment": "production"}, {"payment_provider": "stripe"}, {"allow_dev_login": False},
    {"email_provider": "smtp"}, {"push_provider": "apns"}, {"storage_provider": "s3"},
])
def test_demo_seed_refuses_real_money_or_external_notification_configuration(overrides):
    with pytest.raises(ValueError, match="Demo seeding requires"):
        require_demo_environment(Settings(**overrides))
