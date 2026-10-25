"""Authenticated requester endpoints matching the native app's request contract."""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import bills, db, documents, intake, notify, ratelimit, requests, trust
from .money import MAX_BILL_CENTS
from .security import CurrentUser

router = APIRouter(prefix="/api/requests", tags=["Requests"])


@router.get("")
def list_requests(user: CurrentUser):
    with db.transaction() as conn:
        return {"requests": requests.get(conn, user["id"])}


@router.get("/{bill_id}")
def get_request(bill_id: UUID, user: CurrentUser):
    with db.transaction() as conn:
        return requests.get(conn, user["id"], bill_id)


@router.post("", status_code=201)
def submit_request(
    request: Request, user: CurrentUser,
    first_name: Annotated[str, Form()] = "", email: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "", state: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "", biller_name: Annotated[str, Form()] = "",
    amount_cents: Annotated[str, Form()] = "", due_date: Annotated[str, Form()] = "",
    story: Annotated[str, Form()] = "", consent: Annotated[str, Form()] = "",
    bill_file: Annotated[UploadFile | None, File()] = None,
):
    client = request.client.host if request.client else "unknown"
    if ratelimit.submissions.is_blocked(client):
        raise HTTPException(429, "Too many submissions. Please try again later.")
    values = {key: value.strip() for key, value in {
        "first_name": first_name, "email": email, "city": city, "state": state,
        "category": category, "biller_name": biller_name, "due_date": due_date, "story": story,
    }.items()}
    amount = int(amount_cents) if re.fullmatch(r"[0-9]{1,8}", amount_cents) else 0
    due, errors = intake.validate(values, amount, consent.lower() in ("true", "1", "on"))
    content, detected, file_error = intake.read_upload(bill_file)
    if file_error:
        errors["bill_file"] = file_error
    if errors:
        raise HTTPException(422, errors)
    with db.transaction() as conn:
        # Serialize the per-account quota so changing IPs or concurrent submissions cannot evade it.
        owner = conn.execute("select id from users where id = %s and deleted_at is null for update", (user["id"],)).fetchone()
        if owner is None:
            raise HTTPException(401, "Sign in again")
        recent = conn.execute("""select count(*) as n from bills where requester_user_id = %s
                                 and created_at > now() - interval '1 hour'""", (user["id"],)).fetchone()["n"]
        if recent >= ratelimit.submissions.limit:
            raise HTTPException(429, "Too many submissions from this account. Please try again later.")
        document = documents.store(conn, content, detected[0], "bill")
        submission = bills.create_submission(conn, first_name=values["first_name"], email=values["email"],
            city=values["city"], state=values["state"], category=values["category"],
            biller_name=values["biller_name"], claimed_amount_cents=amount, due_date=due,
            story=values["story"], upload_path=document["storage_key"], upload_content_type=detected[0])
        conn.execute("""update bills set requester_user_id = %s, document_id = %s, consented_at = now()
                        where id = %s""", (user["id"], document["id"], submission["id"]))
        trust.audit(conn, None, "request.submitted", submission["id"], requester_user_id=str(user["id"]))
        notify.bill_event(conn, submission["id"], "submitted")
        result = requests.get(conn, user["id"], submission["id"])
    ratelimit.submissions.hit(client)
    return result


@router.post("/{bill_id}/withdraw")
def withdraw_request(bill_id: UUID, user: CurrentUser):
    with db.transaction() as conn:
        return requests.withdraw(conn, user["id"], bill_id)


class BalanceUpdate(BaseModel):
    amount_cents: int = Field(ge=0, le=MAX_BILL_CENTS, strict=True)
    note: str = Field(default="", max_length=500)


@router.post("/{bill_id}/balance")
def update_request_balance(bill_id: UUID, body: BalanceUpdate, user: CurrentUser):
    with db.transaction() as conn:
        return requests.report_balance(conn, user["id"], bill_id, body.amount_cents, body.note.strip())
