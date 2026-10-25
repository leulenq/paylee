"""Loads unverified sample submissions for local development; never invents verification."""

from datetime import date, timedelta

from . import db, documents
from .config import get_settings

SAMPLES = [
    ("utilities", "Kentucky Utilities", "Maria", "Lexington, KY", 21_250, 8_000, 5,
     "My hours got cut this fall and I fell behind. I have two kids and a shutoff notice for next week. "
     "I'm back to full shifts in November."),
    ("medical", "Baptist Health", "Dev", "Louisville, KY", 64_000, 51_500, 12,
     "ER visit after a bike accident. Insurance covered most of it but this is what's left, "
     "and it just got a collections warning."),
    ("phone", "Verizon", "Tasha", "Columbus, OH", 8_900, 0, 3,
     "I'm job hunting and my phone is how employers reach me. One month would get me to my first paycheck."),
    ("rent", "Park Place Apartments", "James", "Cincinnati, OH", 115_000, 30_000, 8,
     "My roommate moved out without notice and left me with the full rent. "
     "I've found a new roommate starting next month."),
    ("internet", "Spectrum", "Lin", "Nashville, TN", 7_499, 2_000, 6,
     "I'm finishing an online nursing program and need internet for my last two exams."),
    ("utilities", "Atmos Energy", "Robert", "Bowling Green, KY", 13_420, 11_000, 4,
     "Retired and on a fixed income. The gas bill doubled when it got cold and I'm a little short this month."),
]

PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


def main() -> None:
    settings = get_settings()
    if settings.environment == "production":
        raise SystemExit("Refusing to seed sample bills in production")
    db.open_pool(settings.database_url)
    try:
        with db.transaction() as conn:
            db.init_schema(conn)
            for category, biller, name, city, due, _raised, days, story in SAMPLES:
                document = documents.store(conn, PLACEHOLDER_PNG, "image/png", "bill")
                conn.execute(
                    """
                    insert into bills (status, category, biller_name, requester_first_name, requester_email, city, state,
                                       story, claimed_amount_cents, due_date, upload_path, upload_content_type,
                                       document_id, consented_at)
                    values ('submitted', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'image/png', %s, now())
                    """,
                    (category, biller, name, f"{name.lower()}@example.com", city, city[-2:], story, due,
                     date.today() + timedelta(days=days), document["storage_key"], document["id"]),
                )
        print(f"Added {len(SAMPLES)} sample submissions for scanning and review. They are not verified or live.")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
