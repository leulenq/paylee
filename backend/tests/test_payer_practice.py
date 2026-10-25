"""The payer loop: a practice they set, a finishable card, open bills, and facts about the money."""

from datetime import date, timedelta

from .trust_helpers import complete_payment


def _settings(**overrides):
    body = {
        "show_on_leaderboards": True,
        "show_city_on_share_cards": True,
        "notify_payment_facts": True,
        "notify_open_bills": True,
        "practice": {
            "cadence": "weekly",
            "bills_per_period": 1,
            "monthly_limit_cents": 10_000,
            "start_day": "sunday",
        },
    }
    body.update(overrides)
    return body


def _pushes():
    from app import db

    with db.transaction() as conn:
        return conn.execute("select title, body from pushes order by created_at").fetchall()


def test_role_and_practice_round_trip(client, make_user):
    headers, _ = make_user()
    assert client.get("/api/me", headers=headers).json()["role"] is None

    assert client.put("/api/me/role", json={"role": "payer"}, headers=headers).status_code == 204
    assert client.get("/api/me", headers=headers).json()["role"] == "payer"

    saved = client.put("/api/me/settings", json=_settings(), headers=headers)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["practice"]["cadence"] == "weekly"
    assert body["practice_progress"]["target"] == 1
    assert body["practice_progress"]["complete"] is False

    cleared = client.put("/api/me/settings", json=_settings(practice=None), headers=headers).json()
    assert cleared["practice"] is None
    assert cleared["practice_progress"] is None

    rejected = client.put(
        "/api/me/settings",
        json=_settings(practice={**_settings()["practice"], "bills_per_period": 9}),
        headers=headers,
    )
    assert rejected.status_code == 422


def test_a_finish_completes_the_practice_and_a_chip_in_does_not(client, make_user, make_bill):
    headers, _ = make_user()
    client.put("/api/me/settings", json=_settings(), headers=headers)
    chip = make_bill(amount_due_cents=10_000)
    close = make_bill(amount_due_cents=4_000)
    client.post(f"/api/bills/{chip}/contributions", json={"amount_cents": 2_000}, headers=headers)

    midway = client.get("/api/me/settings", headers=headers).json()["practice_progress"]
    assert midway["finished_this_period"] == 0
    assert midway["spent_cents_this_month"] == 2_000
    assert midway["complete"] is False

    client.post(f"/api/bills/{close}/contributions", json={"amount_cents": 4_000}, headers=headers)
    done = client.get("/api/me/settings", headers=headers).json()["practice_progress"]
    assert done["finished_this_period"] == 1
    assert done["complete"] is True
    assert done["period_label"] == "this week"


def test_open_bills_are_the_ones_still_live_soonest_first(client, make_user, make_bill):
    headers, _ = make_user()
    later = make_bill(due_date=date.today() + timedelta(days=6), amount_due_cents=8_000)
    sooner = make_bill(due_date=date.today() + timedelta(days=1), amount_due_cents=8_000)
    closed = make_bill(amount_due_cents=3_000)
    client.post(f"/api/bills/{later}/contributions", json={"amount_cents": 1_000}, headers=headers)
    client.post(f"/api/bills/{sooner}/contributions", json={"amount_cents": 2_500}, headers=headers)
    client.post(f"/api/bills/{closed}/contributions", json={"amount_cents": 3_000}, headers=headers)

    bills = client.get("/api/me/open", headers=headers).json()["bills"]

    assert [b["id"] for b in bills] == [sooner, later]
    assert bills[0]["my_total_cents"] == 2_500
    assert "requester_email" not in bills[0]


def test_open_bill_notice_is_one_fact_and_never_repeats(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=10_000, due_date=date.today() + timedelta(days=2))
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 6_000}, headers=headers)

    assert client.get("/api/feed", headers=headers).status_code == 200
    notices = [p for p in _pushes() if p["title"].endswith("left")]
    assert len(notices) == 1
    assert "Kentucky Utilities" in notices[0]["body"]
    assert "Maria" not in notices[0]["body"]
    assert "Due" in notices[0]["body"]

    client.get("/api/feed", headers=headers)
    assert len([p for p in _pushes() if p["title"].endswith("left")]) == 1


def test_open_bill_notice_waits_for_a_real_due_date_and_can_be_turned_off(client, make_user, make_bill):
    headers, _ = make_user()
    far = make_bill(amount_due_cents=10_000, due_date=date.today() + timedelta(days=10))
    client.post(f"/api/bills/{far}/contributions", json={"amount_cents": 6_000}, headers=headers)
    client.get("/api/feed", headers=headers)
    assert _pushes() == []

    client.put("/api/me/settings", json=_settings(notify_open_bills=False), headers=headers)
    soon = make_bill(amount_due_cents=10_000, due_date=date.today() + timedelta(days=1))
    client.post(f"/api/bills/{soon}/contributions", json={"amount_cents": 6_000}, headers=headers)
    client.get("/api/feed", headers=headers)
    assert _pushes() == []


def test_first_session_opens_on_a_bill_one_gift_can_finish(client, make_user, make_bill):
    headers, _ = make_user()
    full = make_bill(due_date=date.today() + timedelta(days=20))
    almost = make_bill(amount_due_cents=10_000, amount_raised_cents=8_000, due_date=date.today() + timedelta(days=5))
    from app import db

    with db.transaction() as conn:
        conn.execute("update bills set times_shown = 40 where id = %s", (almost,))

    ids = [b["id"] for b in client.get("/api/feed", headers=headers).json()["bills"]]
    assert ids[0] == almost
    assert full in ids


def test_place_and_season_come_with_the_deck(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill(amount_due_cents=5_000)
    client.post(f"/api/bills/{bill_id}/contributions", json={"amount_cents": 5_000}, headers=headers)

    deck = client.get("/api/feed?state=KY", headers=headers).json()

    assert deck["place"]["sentence"] == "1 bill paid off in Lexington this week."
    assert deck["place"]["bills_paid_off"] == 1
    season = deck["season"]
    assert season["category"] in {"rent", "utilities", "medical", "phone", "internet", "other"}
    through = date.fromisoformat(season["through"])
    assert (through.year, through.month) == (date.today().year, date.today().month)
    assert through.day >= 28
    assert season["id"] == date.today().strftime("%Y-%m")


def test_hidden_payer_keeps_a_private_rank(client, make_user, make_bill):
    hidden_headers, hidden = make_user()
    shown_headers, shown = make_user()
    client.put("/api/me/settings", json=_settings(show_on_leaderboards=False, practice=None), headers=hidden_headers)
    theirs = make_bill(amount_due_cents=2_000)
    mine = make_bill(amount_due_cents=2_000)
    client.post(f"/api/bills/{mine}/contributions", json={"amount_cents": 2_000}, headers=hidden_headers)
    client.post(f"/api/bills/{theirs}/contributions", json={"amount_cents": 2_000}, headers=shown_headers)
    complete_payment(mine)
    complete_payment(theirs)

    board = client.get("/api/leaderboard?period=month", headers=shown_headers).json()
    assert hidden["handle"] not in [e["handle"] for e in board["entries"]]
    assert shown["handle"] in [e["handle"] for e in board["entries"]]

    rank = client.get("/api/me/rank", headers=hidden_headers).json()
    assert rank["visible"] is False
    assert rank["bills_completed"] == 1
    assert rank["rank"] == 1
    assert rank["of"] == 2
