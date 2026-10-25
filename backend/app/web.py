"""Server-rendered pages: the requester intake form, the admin console, and the checkout return hop."""

import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import bills, db, documents, intake, notify, ratelimit, refunds, staff, trust
from .config import get_settings
from .money import format_dollars, parse_dollars
from .security import admin_csrf_token, require_admin

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
templates.env.filters["dollars"] = format_dollars

APP_CALLBACK_SCHEME = "paylee"


# --- Requester intake --------------------------------------------------------


@router.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/submit")


@router.get("/submit", response_class=HTMLResponse)
def submit_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "submit.html", {"categories": bills.CATEGORIES, "states": bills.US_STATES, "values": {}, "errors": {}}
    )


@router.post("/submit", response_class=HTMLResponse)
def submit_bill(
    request: Request,
    first_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    biller_name: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
    story: Annotated[str, Form()] = "",
    consent: Annotated[bool, Form()] = False,
    bill_file: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    client = request.client.host if request.client else "unknown"
    if ratelimit.submissions.is_blocked(client):
        return templates.TemplateResponse(
            request, "message.html",
            {"title": "Please try again later",
             "message": "We've received several bills from your connection in the last hour. Try again in a little while."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    values = {
        "first_name": first_name.strip(), "email": email.strip(), "city": city.strip(), "state": state,
        "category": category, "biller_name": biller_name.strip(), "amount": amount.strip(),
        "due_date": due_date, "story": story.strip(),
    }
    amount_cents = 0
    amount_error = None
    try:
        amount_cents = parse_dollars(values["amount"])
    except ValueError as error:
        amount_error = str(error)
    parsed_due, errors = intake.validate(values, amount_cents, consent)
    if amount_error:
        errors.pop("amount_cents", None)
        errors["amount"] = amount_error
    content, detected, file_error = intake.read_upload(bill_file)
    if file_error:
        errors["bill_file"] = file_error

    if errors:
        return templates.TemplateResponse(
            request,
            "submit.html",
            {"categories": bills.CATEGORIES, "states": bills.US_STATES, "values": values, "errors": errors},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    content_type, extension = detected
    ratelimit.submissions.hit(client)
    with db.transaction() as conn:
        document = documents.store(conn, content, content_type, "bill")
        submission = bills.create_submission(
            conn,
            first_name=values["first_name"],
            email=values["email"],
            city=values["city"],
            state=state,
            category=category,
            biller_name=values["biller_name"],
            claimed_amount_cents=amount_cents,
            due_date=parsed_due,
            story=values["story"],
            upload_path=document["storage_key"],
            upload_content_type=content_type,
        )
        conn.execute("update bills set document_id = %s, consented_at = now() where id = %s",
                     (document["id"], submission["id"]))
        notify.bill_event(conn, submission["id"], "submitted")
    return templates.TemplateResponse(request, "submit_done.html", {"first_name": values["first_name"]})


# --- Public bill pages (shared links) -------------------------------------------


@router.get("/b/{bill_id}", response_class=HTMLResponse)
def public_bill(request: Request, bill_id: UUID) -> HTMLResponse:
    with db.transaction() as conn:
        bill = bills.get_public_bill(conn, bill_id)
    if bill is None:
        return templates.TemplateResponse(
            request, "message.html",
            {"title": "This bill isn't available", "message": "It may have been taken down or never posted."},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    due = bill["amount_due_cents"]
    raised = bill["amount_raised_cents"]
    return templates.TemplateResponse(
        request,
        "bill.html",
        {
            "bill": bill,
            "remaining_cents": max(due - raised, 0),
            "percent": min(round(raised * 100 / due), 100) if due else 100,
            "page_url": f"{get_settings().public_base_url.rstrip('/')}/b/{bill_id}",
        },
    )


# --- Checkout return ---------------------------------------------------------


@router.get("/checkout/return", include_in_schema=False)
def checkout_return(contribution_id: UUID, result: str = "success") -> RedirectResponse:
    """Stripe sends the browser here; this hands control back to the app, which then checks the status."""
    outcome = "success" if result == "success" else "cancel"
    query = urlencode({"contribution_id": str(contribution_id), "result": outcome})
    return RedirectResponse(f"{APP_CALLBACK_SCHEME}://checkout?{query}", status_code=status.HTTP_303_SEE_OTHER)


# --- Admin -------------------------------------------------------------------


@router.get("/admin/login", response_class=HTMLResponse)
def staff_login_form(request: Request):
    return templates.TemplateResponse(request, "staff_login.html", {"error": ""})


@router.post("/admin/login")
def staff_login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()],
                code: Annotated[str, Form()] = ""):
    client = request.client.host if request.client else "unknown"
    if ratelimit.admin_failures.is_blocked(client):
        raise HTTPException(429, "Too many failed sign-ins")
    # Protect against login CSRF. Normal browser POSTs include Origin.
    origin = request.headers.get("origin")
    if origin and origin != get_settings().public_base_url.rstrip("/"):
        raise HTTPException(403, "Invalid sign-in origin")
    try:
        with db.transaction() as conn:
            actor = staff.authenticate(conn, username, password, code)
            trust.audit(conn, actor, "staff.signed_in", actor["id"])
    except HTTPException:
        ratelimit.admin_failures.hit(client)
        return templates.TemplateResponse(request, "staff_login.html", {"error": "Invalid staff credentials"}, status_code=401)
    response = RedirectResponse("/admin/ops", status_code=303)
    response.set_cookie(staff.COOKIE, staff.issue_token(actor), httponly=True, samesite="strict",
                        secure=get_settings().environment == "production",
                        max_age=get_settings().staff_session_hours * 3600, path="/admin")
    response.headers["Cache-Control"] = "no-store"
    return response

admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _back_to_admin(message: str) -> RedirectResponse:
    return RedirectResponse(f"/admin?{urlencode({'message': message})}", status_code=status.HTTP_303_SEE_OTHER)


@admin.get("", response_class=HTMLResponse)
def admin_home(request: Request, message: str = "") -> HTMLResponse:
    with db.transaction() as conn:
        overview = bills.admin_overview(conn)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            **overview,
            "message": message,
            "csrf": admin_csrf_token(request.state.staff["id"]),
            "max_live": get_settings().max_live_bills,
        },
    )


@admin.get("/uploads/{bill_id}")
def admin_upload(bill_id: UUID, request: Request) -> Response:
    if request.state.staff["id"]:
        staff.require_role(request.state.staff, "reviewer", "payments", "treasury")
    with db.transaction() as conn:
        upload = conn.execute("select d.* from bills b join documents d on d.id = b.document_id where b.id = %s", (bill_id,)).fetchone()
        if upload:
            trust.audit(conn, request.state.staff, "bill.document_accessed", bill_id)
    if upload is None or upload["scan_status"] != "clean":
        raise HTTPException(409, "Document must pass malware scanning before opening")
    return Response(documents.read(upload), media_type=upload["content_type"],
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                             "Content-Disposition": 'attachment; filename="bill"', "Content-Security-Policy": "sandbox"})


@admin.post("/logout")
def staff_logout(request: Request):
    actor = request.state.staff
    if actor["id"]:
        with db.transaction() as conn:
            conn.execute("update staff set session_version = session_version + 1 where id = %s", (actor["id"],))
            trust.audit(conn, actor, "staff.signed_out", actor["id"])
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(staff.COOKIE, path="/admin")
    return response


@admin.post("/bills/{bill_id}/approve")
def admin_approve(
    bill_id: UUID,
    request: Request,
    csrf: Annotated[str, Form()],
    amount_due: Annotated[str, Form()],
    biller_name: Annotated[str, Form()],
    story: Annotated[str, Form()],
    checked: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    if not checked:
        return _back_to_admin("Not approved: confirm you checked the bill first.")
    try:
        amount_due_cents = parse_dollars(amount_due)
    except ValueError as error:
        return _back_to_admin(f"Not approved: {error}")
    try:
        with db.transaction() as conn:
            approved = bills.approve(
                conn, bill_id, amount_due_cents=amount_due_cents, biller_name=biller_name.strip(),
                story=story.strip(), max_live=get_settings().max_live_bills, actor=request.state.staff,
            )
    except trust.TrustError as error:
        return _back_to_admin(f"Not approved: {error}")
    return _back_to_admin("Approved." if approved else "That bill was already reviewed.")


@admin.post("/bills/{bill_id}/reject")
def admin_reject(bill_id: UUID, request: Request, csrf: Annotated[str, Form()], note: Annotated[str, Form()] = "") -> RedirectResponse:
    if request.state.staff["id"]:
        staff.require_role(request.state.staff, "reviewer")
    with db.transaction() as conn:
        rejected = bills.reject(conn, bill_id, note.strip())
        if rejected:
            trust.audit(conn, request.state.staff, "bill.rejected", bill_id)
    return _back_to_admin("Rejected." if rejected else "That bill was already reviewed.")


@admin.post("/bills/{bill_id}/remove")
def admin_remove(
    bill_id: UUID, request: Request, csrf: Annotated[str, Form()], note: Annotated[str, Form()] = ""
) -> RedirectResponse:
    if request.state.staff["id"]:
        staff.require_role(request.state.staff, "reviewer")
    with db.transaction() as conn:
        refunds = bills.remove(conn, bill_id, note.strip(), get_settings().max_live_bills)
        trust.audit(conn, request.state.staff, "bill.removed", bill_id)
    failed = _refund_all(request.app.state.payments, refunds)
    message = f"Removed. Refunded {len(refunds) - failed} contribution(s)."
    if failed:
        message += f" {failed} refund(s) failed; retry below."
    return _back_to_admin(message)


@admin.post("/bills/{bill_id}/paid")
def admin_mark_paid(
    bill_id: UUID, csrf: Annotated[str, Form()], confirmation: Annotated[str, Form()]
) -> RedirectResponse:
    raise HTTPException(409, "Use /admin/ops: settlement and account posting evidence are required")


@admin.post("/contributions/{contribution_id}/refund")
def admin_retry_refund(contribution_id: UUID, request: Request, csrf: Annotated[str, Form()]) -> RedirectResponse:
    if request.state.staff["id"]:
        staff.require_role(request.state.staff, "treasury")
    with db.transaction() as conn:
        row = conn.execute(
            "select id, payment_ref from contributions where id = %s and status = 'refund_pending'",
            (contribution_id,),
        ).fetchone()
    if row is None:
        return _back_to_admin("Nothing to refund.")
    failed = _refund_all(request.app.state.payments, [row])
    return _back_to_admin("Refund failed again; check the logs." if failed else "Refunded.")


@admin.post("/reports/{report_id}/resolve")
def admin_resolve_report(report_id: UUID, request: Request, csrf: Annotated[str, Form()]) -> RedirectResponse:
    if request.state.staff["id"]:
        staff.require_role(request.state.staff, "reviewer")
    with db.transaction() as conn:
        bills.resolve_report(conn, report_id)
        trust.audit(conn, request.state.staff, "report.resolved", report_id)
    return _back_to_admin("Report dismissed.")


def _refund_all(provider, contributions: list[dict]) -> int:
    failed = 0
    for contribution in contributions:
        try:
            refunds.process(provider, contribution["id"])
        except Exception:
            log.warning("Refund awaiting reconciliation for contribution %s", contribution["id"])
            failed += 1
            continue
    return failed


router.include_router(admin)
