"""Payer profile: the side they chose, the practice they set, and the private record around it.

The monthly limit is a ceiling the payer picked. Nothing here collects money, and a missed period
changes no counter.
"""

from datetime import UTC, date, datetime, timedelta

import psycopg

from . import bills

# Month -> (category, sentence). A season ends when the month does.
_SEASONS = {
    1: ("utilities", "utility bills due this month"),
    2: ("rent", "rent due this month"),
    3: ("medical", "medical bills due this month"),
    4: ("phone", "phone bills due this month"),
    5: ("internet", "internet bills due this month"),
    6: ("utilities", "utility bills due this month"),
    7: ("rent", "rent due this month"),
    8: ("medical", "medical bills due this month"),
    9: ("phone", "phone bills due this month"),
    10: ("utilities", "utility bills due this month"),
    11: ("rent", "rent due this month"),
    12: ("medical", "medical bills due this month"),
}


def set_role(conn: psycopg.Connection, user_id, role: str) -> None:
    conn.execute("update users set role = %s where id = %s", (role, user_id))


def role_of(conn: psycopg.Connection, user_id) -> str | None:
    return conn.execute("select role from users where id = %s", (user_id,)).fetchone()["role"]


def settings_view(conn: psycopg.Connection, user_id) -> dict:
    row = conn.execute(
        """
        select show_on_leaderboards, show_city_on_share_cards, notify_payment_facts, notify_open_bills,
               practice_cadence, practice_bills_per_period, practice_monthly_limit_cents, practice_start_day
        from users where id = %s
        """,
        (user_id,),
    ).fetchone()
    practice = None
    if row["practice_cadence"]:
        practice = {
            "cadence": row["practice_cadence"],
            "bills_per_period": row["practice_bills_per_period"],
            "monthly_limit_cents": row["practice_monthly_limit_cents"],
            "start_day": row["practice_start_day"],
        }
    return {
        "show_on_leaderboards": row["show_on_leaderboards"],
        "show_city_on_share_cards": row["show_city_on_share_cards"],
        "notify_payment_facts": row["notify_payment_facts"],
        "notify_open_bills": row["notify_open_bills"],
        "practice": practice,
        "practice_progress": practice_progress(conn, user_id, practice),
    }


def save_settings(conn: psycopg.Connection, user_id, body) -> dict:
    practice = body.practice
    conn.execute(
        """
        update users set
            show_on_leaderboards = %s,
            show_city_on_share_cards = %s,
            notify_payment_facts = %s,
            notify_open_bills = %s,
            practice_cadence = %s,
            practice_bills_per_period = %s,
            practice_monthly_limit_cents = %s,
            practice_start_day = %s
        where id = %s
        """,
        (
            body.show_on_leaderboards,
            body.show_city_on_share_cards,
            body.notify_payment_facts,
            body.notify_open_bills,
            practice.cadence if practice else None,
            practice.bills_per_period if practice else None,
            practice.monthly_limit_cents if practice else None,
            practice.start_day if practice else None,
            user_id,
        ),
    )
    return settings_view(conn, user_id)


def practice_progress(conn: psycopg.Connection, user_id, practice: dict | None) -> dict | None:
    """Finishes in the current period. A finish is paying the last of a bill, not a chip-in."""
    if practice is None:
        return None
    start = _period_start(datetime.now(UTC), practice["cadence"], practice["start_day"])
    finished = conn.execute(
        """
        select count(*) as n from bills
        where closer_user_id = %s and status in ('funded', 'paid') and funded_at >= %s
        """,
        (user_id, start),
    ).fetchone()["n"]
    spent = conn.execute(
        """
        select coalesce(sum(amount_cents), 0) as n from contributions
        where user_id = %s and status = 'succeeded' and created_at >= date_trunc('month', now())
        """,
        (user_id,),
    ).fetchone()["n"]
    target = practice["bills_per_period"]
    return {
        "finished_this_period": finished,
        "target": target,
        "spent_cents_this_month": spent,
        "complete": finished >= target,
        "period_label": "this week" if practice["cadence"] == "weekly" else "this month",
    }


def _period_start(now: datetime, cadence: str, start_day: str) -> datetime:
    if cadence == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # weekday(): Monday is 0. Sunday is 6. Payday is Friday.
    anchor = {"sunday": 6, "payday": 4}.get(start_day, 0)
    delta = (now.weekday() - anchor) % 7
    start = (now - timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def private_rank(conn: psycopg.Connection, user_id) -> dict:
    board = bills.leaderboard(conn, user_id, period="month", limit=100_000, public_only=False)
    visible = conn.execute(
        "select show_on_leaderboards from users where id = %s", (user_id,)
    ).fetchone()["show_on_leaderboards"]
    me = board["me"]
    return {
        "rank": me["rank"] if me else None,
        "of": board["of"],
        "period": "month",
        "bills_completed": me["bills_completed"] if me else 0,
        "visible": visible,
    }


def open_bills(conn: psycopg.Connection, user_id) -> list[dict]:
    """Bills this payer already funded that are still live, soonest due date first."""
    return conn.execute(
        f"""
        select {bills.PUBLIC_COLUMNS},
               coalesce(sum(c.amount_cents), 0) as my_total_cents
        from contributions c
        join bills b on b.id = c.bill_id
        where c.user_id = %s and c.status = 'succeeded' and b.status = 'live'
          and b.amount_raised_cents < b.amount_due_cents
        group by b.id
        order by b.due_date, b.id
        """,
        (user_id,),
    ).fetchall()


def place_this_week(conn: psycopg.Connection, state: str) -> dict | None:
    """How many bills reached covered in the payer's top city this week. Nothing, when it's zero."""
    row = conn.execute(
        """
        select split_part(b.city, ',', 1) as city, count(*) as n
        from bills b
        where b.state = %s and b.status in ('funded', 'paid')
          and b.funded_at >= date_trunc('week', now())
        group by 1
        order by n desc, city
        limit 1
        """,
        (state,),
    ).fetchone()
    if row is None or row["n"] == 0:
        return None
    noun = "bill" if row["n"] == 1 else "bills"
    return {
        "state": state,
        "city": row["city"],
        "bills_paid_off": row["n"],
        "sentence": f"{row['n']} {noun} paid off in {row['city']} this week.",
    }


def current_season(today: date | None = None) -> dict:
    today = today or date.today()
    category, detail = _SEASONS[today.month]
    if today.month == 12:
        through = date(today.year, 12, 31)
    else:
        through = date(today.year, today.month + 1, 1) - timedelta(days=1)
    return {
        "id": today.strftime("%Y-%m"),
        "title": today.strftime("%B"),
        "category": category,
        "detail": detail,
        "through": through.isoformat(),
    }
