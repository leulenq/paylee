"""Apply additive schema using a database owner, separately from runtime credentials."""

from . import db
from .config import get_settings


def main():
    db.open_pool(get_settings().database_url)
    try:
        with db.transaction() as conn:
            db.init_schema(conn)
        print("Paylee schema applied.")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
