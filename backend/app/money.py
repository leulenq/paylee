from decimal import Decimal, InvalidOperation

MAX_BILL_CENTS = 2_500_000  # $25,000


def parse_dollars(text: str) -> int:
    """Parses user-typed dollars ("$1,234.50") into integer cents, or raises ValueError with a readable message."""
    cleaned = text.strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Enter an amount like 212.50") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Enter an amount greater than $0")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Use at most two decimal places")
    cents = int(amount * 100)
    if cents > MAX_BILL_CENTS:
        raise ValueError("Bills over $25,000 can't be posted yet")
    return cents


def format_dollars(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"
