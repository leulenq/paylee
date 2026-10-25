"""Feed filters, rewind, tiers, devices, and giver push notifications."""

from app import bills, db, notify

from .conftest import bill_row
from .test_contributions import contribute

TOKEN = "a1" * 32


def feed_ids(client, headers, **params):
    response = client.get("/api/feed", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return {b["id"] for b in response.json()["bills"]}


def test_feed_filters_by_category_state_and_amount(client, make_user, make_bill):
    headers, _ = make_user()
    ky_power = make_bill(category="utilities", state="KY", amount_due_cents=8_000)
    ky_rent = make_bill(category="rent", state="KY", amount_due_cents=120_000)
    oh_medical = make_bill(category="medical", state="OH", amount_due_cents=30_000)

    assert feed_ids(client, headers) == {ky_power, ky_rent, oh_medical}
    assert feed_ids(client, headers, category=["utilities", "medical"]) == {ky_power, oh_medical}
    assert feed_ids(client, headers, state="ky") == {ky_power, ky_rent}
    assert feed_ids(client, headers, max_remaining_cents=50_000) == {ky_power, oh_medical}
    assert feed_ids(client, headers, state="OH", category=["rent"]) == set()


def test_feed_skips_bills_already_in_the_deck(client, make_user, make_bill):
    headers, _ = make_user()
    first, second = make_bill(), make_bill()

    assert feed_ids(client, headers, exclude=[first]) == {second}


def test_feed_rejects_unknown_filters(client, make_user):
    headers, _ = make_user()
    assert client.get("/api/feed", headers=headers, params={"category": "yachts"}).status_code == 422
    assert client.get("/api/feed", headers=headers, params={"state": "ZZ"}).status_code == 422


def test_rewind_puts_a_passed_bill_back(client, make_user, make_bill):
    headers, _ = make_user()
    bill_id = make_bill()
    client.post(f"/api/bills/{bill_id}/pass", headers=headers)
    assert feed_ids(client, headers) == set()

    assert client.delete(f"/api/bills/{bill_id}/pass", headers=headers).status_code == 204

    assert feed_ids(client, headers) == {bill_id}


def test_tiers_count_bills_paid_off():
    assert bills.tier(0) == {"name": "Member", "next_name": "Gold", "next_at": 3, "progress": 0.0}
    assert bills.tier(5)["name"] == "Gold"
    assert bills.tier(5)["progress"] == round(2 / 7, 3)
    assert bills.tier(40) == {"name": "Black", "next_name": None, "next_at": None, "progress": 1.0}


def test_me_includes_tier(client, make_user):
    headers, _ = make_user()
    assert client.get("/api/me", headers=headers).json()["tier"]["name"] == "Member"


def test_device_registration_moves_with_sign_in(client, make_user):
    first_headers, first = make_user()
    second_headers, second = make_user()

    assert client.post("/api/me/devices", json={"token": TOKEN, "environment": "sandbox"}, headers=first_headers).status_code == 204
    client.post("/api/me/devices", json={"token": TOKEN, "environment": "sandbox"}, headers=second_headers)

    with db.transaction() as conn:
        owner = conn.execute("select user_id from device_tokens where token = %s", (TOKEN,)).fetchone()["user_id"]
    assert str(owner) == second["id"]

    assert client.post("/api/me/devices", json={"token": "not hex!"}, headers=first_headers).status_code == 422
    client.delete(f"/api/me/devices/{TOKEN}", headers=second_headers)
    with db.transaction() as conn:
        assert conn.execute("select count(*) as n from device_tokens").fetchone()["n"] == 0


def pushes() -> list[tuple[str, str]]:
    with db.transaction() as conn:
        rows = conn.execute(
            "select u.handle, p.title from pushes p join users u on u.id = p.user_id order by p.created_at, u.handle"
        ).fetchall()
    return [(r["handle"], r["title"]) for r in rows]


def test_givers_get_pushes_when_their_bill_closes_and_is_paid(client, make_user, make_bill):
    helper_headers, helper = make_user()
    closer_headers, closer = make_user()
    bill_id = make_bill(amount_due_cents=10_000)
    contribute(client, helper_headers, bill_id, 4_000)
    contribute(client, closer_headers, bill_id, 6_000)

    assert sorted(pushes()) == sorted([
        (helper["handle"], "A bill you helped is covered"),
        (closer["handle"], "You closed it"),
    ])

    from .trust_helpers import complete_payment
    complete_payment(bill_id, "CONF-9")
    assert sorted(t for h, t in pushes() if t.endswith("has been paid")) == ["Kentucky Utilities has been paid"] * 2


def test_refunded_giver_gets_a_push(client, make_user, make_bill):
    from app.security import admin_csrf_token

    from .conftest import ADMIN_AUTH

    headers, giver = make_user()
    bill_id = make_bill()
    contribute(client, headers, bill_id, 2_000)
    client.post(f"/admin/bills/{bill_id}/remove", data={"csrf": admin_csrf_token(), "note": "x"}, auth=ADMIN_AUTH)

    assert pushes() == [(giver["handle"], "You've been refunded")]
    assert bill_row(bill_id)["status"] == "removed"


def test_push_delivery_drops_dead_tokens(client, make_user, make_bill):
    headers, _ = make_user()
    client.post("/api/me/devices", json={"token": TOKEN, "environment": "sandbox"}, headers=headers)
    dead = "b2" * 32
    client.post("/api/me/devices", json={"token": dead, "environment": "sandbox"}, headers=headers)
    bill_id = make_bill(amount_due_cents=1_000)
    contribute(client, headers, bill_id, 1_000)

    sent = []

    class FakePusher:
        def send(self, token, environment, title, body, bill_id):
            sent.append((token, title, str(bill_id)))
            return token != dead

    assert notify.deliver_pending_pushes(FakePusher()) == 1
    assert {(t, title) for t, title, _ in sent} == {(TOKEN, "You closed it"), (dead, "You closed it")}
    assert all(b == bill_id for _, _, b in sent)
    with db.transaction() as conn:
        tokens = [r["token"] for r in conn.execute("select token from device_tokens").fetchall()]
    assert tokens == [TOKEN]
    assert notify.deliver_pending_pushes(FakePusher()) == 0


def test_apns_request_shape_and_dead_token_handling():
    import httpx
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    requests = []

    def apple(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("dead"):
            return httpx.Response(410, json={"reason": "Unregistered"})
        return httpx.Response(200)

    pusher = notify.APNsPusher("KEY123", "TEAM456", pem.decode(), "enquanhone.SwipeBills", transport=httpx.MockTransport(apple))

    assert pusher.send("abc", "sandbox", "You closed it", "Body", "bill-1") is True
    assert pusher.send("dead", "production", "Hi", "Body", None) is False

    first = requests[0]
    assert first.url.host == "api.sandbox.push.apple.com"
    assert requests[1].url.host == "api.push.apple.com"
    assert first.headers["apns-topic"] == "enquanhone.SwipeBills"
    assert first.headers["apns-push-type"] == "alert"
    token = first.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(token)["kid"] == "KEY123"
    assert jwt.decode(token, key.public_key(), algorithms=["ES256"])["iss"] == "TEAM456"
    import json
    assert json.loads(first.content) == {
        "aps": {"alert": {"title": "You closed it", "body": "Body"}, "sound": "default"}, "bill_id": "bill-1",
    }
