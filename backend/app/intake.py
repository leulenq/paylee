"""Shared field/file validation for authenticated mobile intake and the web intake form."""

import re
from datetime import date, timedelta

from . import bills
from .config import get_settings
from .money import MAX_BILL_CENTS

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def detect_upload_type(head: bytes):
    if head.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "image/heic", ".heic"
    return None


def validate(values, amount_cents, consent):
    errors = {}
    if not 1 <= len(values["first_name"]) <= 40:
        errors["first_name"] = "Enter your first name."
    if not EMAIL_PATTERN.fullmatch(values["email"]) or len(values["email"]) > 254:
        errors["email"] = "Enter an email we can reach you at."
    if not 2 <= len(values["city"]) <= 60:
        errors["city"] = "Enter your city."
    if values["state"] not in bills.US_STATES:
        errors["state"] = "Choose your state."
    if values["category"] not in bills.CATEGORIES:
        errors["category"] = "Choose what kind of bill this is."
    if not 2 <= len(values["biller_name"]) <= 80:
        errors["biller_name"] = "Enter the company on the bill."
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or not 1 <= amount_cents <= MAX_BILL_CENTS:
        errors["amount_cents"] = "Enter a whole number of cents between 1 and 2500000."
    parsed_due = None
    try:
        parsed_due = date.fromisoformat(values["due_date"])
        if not date.today() - timedelta(days=60) <= parsed_due <= date.today() + timedelta(days=365):
            errors["due_date"] = "Enter a due date within the last 60 days or the next year."
    except ValueError:
        errors["due_date"] = "Enter the due date on the bill."
    if not 40 <= len(values["story"]) <= 800:
        errors["story"] = "Write between 40 and 800 characters."
    if not consent:
        errors["consent"] = "Confirm the bill is in your name."
    return parsed_due, errors


def read_upload(upload):
    if upload is None or not upload.filename:
        return b"", None, "Attach a photo or PDF of the bill."
    content = upload.file.read(get_settings().max_upload_bytes + 1)
    if len(content) > get_settings().max_upload_bytes:
        return b"", None, "The file must be under 10 MB."
    detected = detect_upload_type(content[:16])
    if detected is None:
        return b"", None, "Upload a PDF, JPG, PNG, or HEIC file."
    return content, detected, None
