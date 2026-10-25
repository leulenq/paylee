import secrets

import psycopg

ADJECTIVES = [
    "Quiet", "Kind", "Steady", "Bright", "Brave", "Gentle", "Swift", "Lucky",
    "Calm", "Sunny", "Bold", "Warm", "Clever", "Noble", "Merry", "Humble",
]
ANIMALS = [
    "Otter", "Falcon", "Heron", "Fox", "Badger", "Owl", "Lynx", "Robin",
    "Bison", "Wren", "Marten", "Hare", "Koi", "Panda", "Sparrow", "Elk",
]


def _random_handle() -> str:
    return f"{secrets.choice(ADJECTIVES)}{secrets.choice(ANIMALS)}{secrets.randbelow(900) + 100}"


def get_or_create_user(conn: psycopg.Connection, auth_subject: str) -> dict:
    for _ in range(5):
        user = conn.execute(
            "select id, handle, created_at from users where auth_subject = %s and deleted_at is null",
            (auth_subject,),
        ).fetchone()
        if user:
            return user
        # "do nothing" covers both a taken handle and a concurrent sign-in for the same subject; loop and retry.
        user = conn.execute(
            """
            insert into users (auth_subject, handle) values (%s, %s)
            on conflict do nothing
            returning id, handle, created_at
            """,
            (auth_subject, _random_handle()),
        ).fetchone()
        if user:
            return user
    raise RuntimeError("Could not create a user handle")


def delete_account(conn: psycopg.Connection, user_id) -> None:
    """Anonymizes the account. Contribution rows stay, because they are financial records."""
    conn.execute(
        """
        update users
        set deleted_at = now(), auth_subject = null, handle = 'deleted-' || id::text
        where id = %s
        """,
        (user_id,),
    )
    conn.execute("delete from passes where user_id = %s", (user_id,))
    conn.execute("delete from device_tokens where user_id = %s", (user_id,))
