"""Emails to requesters and push notifications to givers.

Both are queued in outbox tables inside the caller's transaction and sent by OutboxWorker, so a message is
never lost and never sent for a change that rolled back.
"""

import logging
import smtplib
import threading
import time
from datetime import date
from email.message import EmailMessage

import httpx
import jwt
import psycopg

from . import db
from .config import Settings
from .money import format_dollars

log = logging.getLogger(__name__)

# Wakes the worker early when something was queued, instead of waiting out its interval.
_wake = threading.Event()

MAX_ATTEMPTS = 5

MESSAGES = {
    "submitted": (
        "We received your bill",
        "Thanks for sending your {biller} bill. A reviewer will check it within two business days and email "
        "you either way.\n\nKeep paying what you can in the meantime. If your balance changes, reply to this "
        "email so we never overpay {biller}.",
    ),
    "queued": (
        "Your bill was approved",
        "Your {biller} bill was approved for {amount_due}. It's on a short waitlist so every bill gets fair "
        "attention, and we'll email you the moment it goes live.",
    ),
    "live": (
        "Your bill is live on Paylee",
        "Givers can now see your {biller} bill and chip in. You don't need to do anything. We'll email you "
        "when it's fully covered.",
    ),
    "rejected": (
        "We couldn't post your bill",
        "We weren't able to verify your {biller} bill, so it won't be posted. The usual reasons are a bill "
        "that isn't in your name, a photo we couldn't read, or a balance we couldn't confirm. You're welcome "
        "to submit again with a clearer copy.",
    ),
    "funded": (
        "Your bill is fully covered",
        "Givers covered the full {amount_due} on your {biller} bill. We will verify the current balance, "
        "pay {biller} directly, and email you when the payment is applied. Until then, your bill remains unpaid. "
        "Tell us about any other payments or balance changes.",
    ),
    "paid": (
        "We paid {biller}",
        "We paid {amount_raised} to {biller} on your account. Confirmation number: {confirmation}.",
    ),
    "expired": (
        "Your bill's posting has ended",
        "The due date for your {biller} bill has passed, so givers no longer see it. {expired_detail}",
    ),
    "removed": (
        "Your bill was taken down",
        "After a review, your {biller} bill is no longer posted. Eligible contributions will be returned "
        "to their original payment methods once reconciled.",
    ),
}


def bill_event(conn: psycopg.Connection, bill_id, event: str) -> None:
    bill = conn.execute(
        """
        select requester_first_name, requester_email, biller_name, amount_due_cents, amount_raised_cents,
               payout_confirmation
        from bills where id = %s
        """,
        (bill_id,),
    ).fetchone()
    if bill is None:
        return
    raised = bill["amount_raised_cents"]
    values = {
        "biller": bill["biller_name"],
        "amount_due": format_dollars(bill["amount_due_cents"] or 0),
        "amount_raised": format_dollars(raised),
        "confirmation": bill["payout_confirmation"] or "",
        "expired_detail": (
            f"Givers raised {format_dollars(raised)}. We will pay it to {bill['biller_name']} "
            "if its current balance and payment route can be verified. Otherwise contributions will be refunded. "
            "We'll email you the outcome."
            if raised
            else "Nothing was raised this time. You're welcome to submit a new bill."
        ),
    }
    subject, body = MESSAGES[event]
    conn.execute(
        "insert into emails (to_address, subject, body) values (%s, %s, %s)",
        (
            bill["requester_email"],
            subject.format(**values),
            f"Hi {bill['requester_first_name']},\n\n{body.format(**values)}\n\nThe Paylee team\n",
        ),
    )
    _wake.set()


def givers_of_bill(conn: psycopg.Connection, bill_id, event: str) -> None:
    """Queues a push for everyone who helped with a bill, when it's fully covered ("funded") or paid."""
    bill = conn.execute(
        "select biller_name, requester_first_name, amount_raised_cents, closer_user_id from bills where id = %s",
        (bill_id,),
    ).fetchone()
    givers = conn.execute(
        "select distinct user_id from contributions where bill_id = %s and status = 'succeeded'", (bill_id,)
    ).fetchall()
    biller = bill["biller_name"]
    amount = format_dollars(bill["amount_raised_cents"])
    for giver in givers:
        if not _wants(conn, giver["user_id"], "notify_payment_facts"):
            continue
        if event == "funded" and giver["user_id"] == bill["closer_user_id"]:
            title, body = "You closed it", f"The {biller} bill is fully covered. Payment to {biller} is next."
        elif event == "funded":
            title, body = (
                "A bill you helped is covered",
                f"The {biller} bill you started is fully covered. Payment to {biller} is next.",
            )
        else:
            title, body = f"{biller} has been paid", f"{biller} confirmed the {amount} payment."
        _queue_push(conn, giver["user_id"], title, body, bill_id)


def giver_refunded(conn: psycopg.Connection, contribution_id) -> None:
    row = conn.execute(
        """
        select c.user_id, coalesce(c.received_amount_cents, c.amount_cents) as amount_cents, b.id as bill_id, b.biller_name
        from contributions c join bills b on b.id = c.bill_id where c.id = %s
        """,
        (contribution_id,),
    ).fetchone()
    if not _wants(conn, row["user_id"], "notify_payment_facts"):
        return
    _queue_push(
        conn,
        row["user_id"],
        "You've been refunded",
        f"{format_dollars(row['amount_cents'])} came back to you. The {row['biller_name']} bill was finished before your payment landed.",
        row["bill_id"],
    )


def queue_open_bill_notices(conn: psycopg.Connection) -> int:
    """One fact per bill a payer already started: a small remainder and a real due date.

    The person who posted the bill is never the reason in the message.
    """
    rows = conn.execute(
        """
        select c.user_id, b.id as bill_id, b.biller_name, b.due_date,
               (b.amount_due_cents - b.amount_raised_cents) as remaining
        from contributions c
        join bills b on b.id = c.bill_id
        join users u on u.id = c.user_id
        where c.status = 'succeeded' and b.status = 'live'
          and u.notify_open_bills and u.deleted_at is null
          and b.due_date >= current_date and b.due_date <= current_date + 3
          and (b.amount_due_cents - b.amount_raised_cents) between 1 and 5000
          and not exists (
              select 1 from open_bill_notices n
              where n.user_id = c.user_id and n.bill_id = b.id
          )
        group by c.user_id, b.id, b.biller_name, b.due_date, b.amount_due_cents, b.amount_raised_cents
        """
    ).fetchall()
    queued = 0
    for row in rows:
        inserted = conn.execute(
            """
            insert into open_bill_notices (user_id, bill_id) values (%s, %s)
            on conflict do nothing returning user_id
            """,
            (row["user_id"], row["bill_id"]),
        ).fetchone()
        if inserted is None:
            continue
        amount = format_dollars(row["remaining"])
        _queue_push(
            conn,
            row["user_id"],
            f"{amount} left",
            f"{amount} left on the {row['biller_name']} bill you started. {_due_phrase(row['due_date'])}.",
            row["bill_id"],
        )
        queued += 1
    return queued


def _wants(conn: psycopg.Connection, user_id, column: str) -> bool:
    if column not in ("notify_payment_facts", "notify_open_bills"):
        raise ValueError(column)
    row = conn.execute(f"select {column} from users where id = %s", (user_id,)).fetchone()
    return bool(row and row[column])


def _due_phrase(due: date) -> str:
    days = (due - date.today()).days
    if days <= 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    if days <= 6:
        return f"Due {due.strftime('%A')}"
    return f"Due {due.strftime('%b')} {due.day}"


def _queue_push(conn: psycopg.Connection, user_id, title: str, body: str, bill_id) -> None:
    conn.execute(
        "insert into pushes (user_id, title, body, bill_id) values (%s, %s, %s, %s)",
        (user_id, title, body, bill_id),
    )
    _wake.set()


class ConsoleMailer:
    """Development: prints emails to the server log instead of sending them."""

    def send(self, to: str, subject: str, body: str) -> None:
        log.info("Email to %s | %s\n%s", to, subject, body)


class SMTPMailer:
    def __init__(self, host: str, port: int, username: str, password: str, sender: str):
        self.host, self.port, self.username, self.password, self.sender = host, port, username, password, sender

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)


def build_mailer(settings: Settings) -> ConsoleMailer | SMTPMailer:
    if settings.email_provider == "smtp":
        if not settings.smtp_host or not settings.email_from:
            raise RuntimeError("EMAIL_PROVIDER=smtp needs SMTP_HOST and EMAIL_FROM")
        return SMTPMailer(
            settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password,
            settings.email_from,
        )
    if settings.email_provider == "console":
        return ConsoleMailer()
    raise RuntimeError(f"Unknown EMAIL_PROVIDER {settings.email_provider!r}")


def deliver_pending(mailer, limit: int = 20) -> int:
    """Sends queued emails. Failures stay queued and are retried, up to MAX_ATTEMPTS."""
    sent = 0
    with db.transaction() as conn:
        rows = conn.execute(
            """
            select id, to_address, subject, body from emails
            where sent_at is null and attempts < %s
            order by created_at
            limit %s
            for update skip locked
            """,
            (MAX_ATTEMPTS, limit),
        ).fetchall()
        for row in rows:
            try:
                mailer.send(row["to_address"], row["subject"], row["body"])
            except Exception as error:
                log.warning("Email %s failed: %s", row["id"], error)
                conn.execute(
                    "update emails set attempts = attempts + 1, last_error = %s where id = %s",
                    (str(error)[:500], row["id"]),
                )
            else:
                conn.execute(
                    "update emails set attempts = attempts + 1, sent_at = now() where id = %s", (row["id"],)
                )
                sent += 1
    return sent


class LogPusher:
    """Development: logs pushes instead of sending them. Test real ones with `xcrun simctl push`."""

    def send(self, token: str, environment: str, title: str, body: str, bill_id) -> bool:
        log.info("Push to %s… | %s: %s", token[:8], title, body)
        return True


class APNsPusher:
    """Sends through Apple Push Notification service with token (JWT) authentication."""

    def __init__(self, key_id: str, team_id: str, private_key: str, bundle_id: str, transport=None):
        self.key_id, self.team_id, self.private_key, self.bundle_id = key_id, team_id, private_key, bundle_id
        # APNs only speaks HTTP/2. `transport` lets tests stand in for Apple.
        self._client = httpx.Client(http2=True, timeout=15, transport=transport)
        self._token: tuple[str, float] | None = None

    def _auth_token(self) -> str:
        # Apple accepts a provider token for up to an hour and rejects ones refreshed too often.
        if self._token is None or time.time() - self._token[1] > 50 * 60:
            issued = time.time()
            token = jwt.encode(
                {"iss": self.team_id, "iat": int(issued)}, self.private_key, algorithm="ES256",
                headers={"kid": self.key_id},
            )
            self._token = (token, issued)
        return self._token[0]

    def send(self, token: str, environment: str, title: str, body: str, bill_id) -> bool:
        """Returns False when the device token is dead and should be forgotten."""
        host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
        if bill_id:
            payload["bill_id"] = str(bill_id)
        response = self._client.post(
            f"https://{host}/3/device/{token}",
            json=payload,
            headers={
                "authorization": f"bearer {self._auth_token()}",
                "apns-topic": self.bundle_id,
                "apns-push-type": "alert",
            },
        )
        if response.status_code == 410 or (
            response.status_code == 400 and response.json().get("reason") == "BadDeviceToken"
        ):
            return False
        response.raise_for_status()
        return True


def build_pusher(settings: Settings) -> LogPusher | APNsPusher:
    if settings.push_provider == "apns":
        if not (settings.apns_key_id and settings.apns_team_id and settings.apns_private_key):
            raise RuntimeError("PUSH_PROVIDER=apns needs APNS_KEY_ID, APNS_TEAM_ID, and APNS_PRIVATE_KEY")
        return APNsPusher(
            settings.apns_key_id, settings.apns_team_id, settings.apns_private_key, settings.apple_bundle_id
        )
    if settings.push_provider == "log":
        return LogPusher()
    raise RuntimeError(f"Unknown PUSH_PROVIDER {settings.push_provider!r}")


def deliver_pending_pushes(pusher, limit: int = 50) -> int:
    """Sends queued pushes to every device of each recipient, forgetting tokens Apple reports as dead."""
    delivered = 0
    with db.transaction() as conn:
        rows = conn.execute(
            """
            select id, user_id, title, body, bill_id from pushes
            where sent_at is null and attempts < %s
            order by created_at
            limit %s
            for update skip locked
            """,
            (MAX_ATTEMPTS, limit),
        ).fetchall()
        for row in rows:
            devices = conn.execute(
                "select token, environment from device_tokens where user_id = %s", (row["user_id"],)
            ).fetchall()
            try:
                for device in devices:
                    if not pusher.send(device["token"], device["environment"], row["title"], row["body"], row["bill_id"]):
                        conn.execute("delete from device_tokens where token = %s", (device["token"],))
            except Exception as error:
                log.warning("Push %s failed: %s", row["id"], error)
                conn.execute(
                    "update pushes set attempts = attempts + 1, last_error = %s where id = %s",
                    (str(error)[:500], row["id"]),
                )
            else:
                conn.execute("update pushes set attempts = attempts + 1, sent_at = now() where id = %s", (row["id"],))
                delivered += 1
    return delivered


class OutboxWorker:
    def __init__(self, mailer, pusher, interval_seconds: float = 15):
        self.mailer = mailer
        self.pusher = pusher
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="email-outbox", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        _wake.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with db.transaction() as conn:
                    queue_open_bill_notices(conn)
            except Exception:
                log.exception("Open-bill notices failed")
            try:
                deliver_pending(self.mailer)
            except Exception:
                log.exception("Email outbox run failed")
            try:
                deliver_pending_pushes(self.pusher)
            except Exception:
                log.exception("Push outbox run failed")
            _wake.wait(self.interval_seconds)
            _wake.clear()
