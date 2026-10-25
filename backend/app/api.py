import logging
from typing import Literal
from uuid import UUID

import jwt
import stripe
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import accounts, bills, db, events, profile, refunds, trust
from .config import get_settings
from .money import MAX_BILL_CENTS
from .security import CurrentUser, issue_session_token, verify_apple_identity_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class AppleLogin(BaseModel):
    identity_token: str


class DevLogin(BaseModel):
    device_id: str = Field(min_length=8, max_length=100)


class ContributionRequest(BaseModel):
    amount_cents: int = Field(gt=0, le=MAX_BILL_CENTS)


class ReportRequest(BaseModel):
    reason: Literal["fake_or_altered", "not_their_bill", "offensive", "personal_info", "other"]


class RoleUpdate(BaseModel):
    role: Literal["payer", "requestor"]


class PracticeInput(BaseModel):
    cadence: Literal["weekly", "monthly"]
    bills_per_period: int = Field(ge=1, le=4)
    monthly_limit_cents: int | None = Field(default=None, ge=500, le=1_000_000_00)
    start_day: Literal["sunday", "payday", "first_of_month"]


class SettingsInput(BaseModel):
    show_on_leaderboards: bool
    show_city_on_share_cards: bool
    notify_payment_facts: bool
    notify_open_bills: bool
    practice: PracticeInput | None = None


def _session(user: dict) -> dict:
    return {"token": issue_session_token(user["id"]), "user": {"id": user["id"], "handle": user["handle"]}}


def _settle(provider, contribution_id, *, payment_ref, paid_amount_cents) -> bills.Confirmation:
    """Applies a completed payment, refunding it instead if the bill can no longer take it."""
    with db.transaction() as conn:
        confirmation = bills.confirm_payment(
            conn,
            contribution_id,
            payment_ref=payment_ref,
            paid_amount_cents=paid_amount_cents,
            max_live=get_settings().max_live_bills,
        )
    if confirmation.outcome == "refund":
        try:
            refunds.process(provider, contribution_id)
        except Exception:
            # Stays refund_pending and shows up on the admin page for a retry.
            log.warning("Refund awaiting reconciliation for contribution %s", contribution_id)
    return confirmation


# --- Auth --------------------------------------------------------------------


@router.post("/auth/apple")
def login_with_apple(body: AppleLogin) -> dict:
    try:
        subject = verify_apple_identity_token(body.identity_token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Apple sign-in couldn't be verified") from None
    with db.transaction() as conn:
        user = accounts.get_or_create_user(conn, f"apple:{subject}")
    return _session(user)


@router.post("/auth/dev")
def login_for_development(body: DevLogin) -> dict:
    if not get_settings().allow_dev_login:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    with db.transaction() as conn:
        user = accounts.get_or_create_user(conn, f"dev:{body.device_id}")
    return _session(user)


# --- Me ----------------------------------------------------------------------


@router.get("/me")
def get_me(user: CurrentUser) -> dict:
    with db.transaction() as conn:
        stats = bills.impact(conn, user["id"])
        role = profile.role_of(conn, user["id"])
    return {
        "id": user["id"],
        "handle": user["handle"],
        "member_since": user["created_at"],
        "impact": stats,
        "tier": bills.tier(stats["bills_completed"]),
        "badges": bills.badges(stats),
        "role": role,
    }


@router.put("/me/role", status_code=status.HTTP_204_NO_CONTENT)
def put_role(body: RoleUpdate, user: CurrentUser) -> Response:
    with db.transaction() as conn:
        profile.set_role(conn, user["id"], body.role)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/settings")
def get_settings_view(user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return profile.settings_view(conn, user["id"])


@router.put("/me/settings")
def put_settings(body: SettingsInput, user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return profile.save_settings(conn, user["id"], body)


@router.get("/me/rank")
def get_rank(user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return profile.private_rank(conn, user["id"])


@router.get("/me/open")
def get_open_bills(user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return {"bills": profile.open_bills(conn, user["id"])}


class DeviceRegistration(BaseModel):
    token: str = Field(min_length=32, max_length=200, pattern=r"^[0-9a-fA-F]+$")
    environment: Literal["sandbox", "production"] = "production"


@router.post("/me/devices", status_code=status.HTTP_204_NO_CONTENT)
def register_device(body: DeviceRegistration, user: CurrentUser) -> Response:
    with db.transaction() as conn:
        conn.execute(
            """
            insert into device_tokens (token, user_id, environment) values (%s, %s, %s)
            on conflict (token) do update
            set user_id = excluded.user_id, environment = excluded.environment, updated_at = now()
            """,
            (body.token.lower(), user["id"], body.environment),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me/devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(token: str, user: CurrentUser) -> Response:
    """Called on sign-out, so the device stops getting this giver's notifications."""
    with db.transaction() as conn:
        conn.execute("delete from device_tokens where token = %s and user_id = %s", (token.lower(), user["id"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(user: CurrentUser) -> Response:
    with db.transaction() as conn:
        accounts.delete_account(conn, user["id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/activity")
def get_my_activity(user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return bills.activity(conn, user["id"])


@router.post("/me/activity/seen", status_code=status.HTTP_204_NO_CONTENT)
def mark_my_activity_seen(user: CurrentUser) -> Response:
    with db.transaction() as conn:
        bills.mark_activity_seen(conn, user["id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/contributions")
def get_my_contributions(user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return {"contributions": bills.my_contributions(conn, user["id"])}


# --- Feed and bills ----------------------------------------------------------


@router.get("/feed")
def get_feed(
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=50),
    category: list[str] = Query(default=[]),
    state: str | None = Query(None, min_length=2, max_length=2),
    max_remaining_cents: int | None = Query(None, gt=0),
    exclude: list[UUID] = Query(default=[], max_length=100),
) -> dict:
    unknown = set(category) - set(bills.CATEGORIES)
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown category: {', '.join(sorted(unknown))}")
    if state is not None and state.upper() not in bills.US_STATES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown state: {state}")
    settings = get_settings()
    state_code = state.upper() if state else None
    with db.transaction() as conn:
        bills.expire_stale(conn, settings)
        return {
            "bills": bills.feed(
                conn,
                user["id"],
                limit=limit,
                settings=settings,
                categories=category,
                state=state_code,
                max_remaining_cents=max_remaining_cents,
                exclude_ids=exclude,
            ),
            "place": profile.place_this_week(conn, state_code) if state_code else None,
            "season": profile.current_season(),
        }


def _public_bill_or_404(conn, bill_id: UUID) -> dict:
    bill = bills.get_public_bill(conn, bill_id)
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found")
    return bill


@router.get("/bills/{bill_id}")
def get_bill(bill_id: UUID, user: CurrentUser) -> dict:
    with db.transaction() as conn:
        return _public_bill_or_404(conn, bill_id)


@router.post("/bills/{bill_id}/pass", status_code=status.HTTP_204_NO_CONTENT)
def pass_bill(bill_id: UUID, user: CurrentUser) -> Response:
    with db.transaction() as conn:
        _public_bill_or_404(conn, bill_id)
        bills.record_pass(conn, user["id"], bill_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/bills/{bill_id}/pass", status_code=status.HTTP_204_NO_CONTENT)
def undo_pass(bill_id: UUID, user: CurrentUser) -> Response:
    """Rewind: puts a passed bill back in the giver's feed."""
    with db.transaction() as conn:
        bills.undo_pass(conn, user["id"], bill_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/bills/{bill_id}/report", status_code=status.HTTP_204_NO_CONTENT)
def report_bill(bill_id: UUID, body: ReportRequest, user: CurrentUser) -> Response:
    with db.transaction() as conn:
        _public_bill_or_404(conn, bill_id)
        bills.report(conn, user["id"], bill_id, body.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/bills/{bill_id}/contributions", status_code=status.HTTP_201_CREATED)
def contribute(bill_id: UUID, body: ContributionRequest, user: CurrentUser, request: Request) -> dict:
    settings = get_settings()
    provider = request.app.state.payments

    try:
        with db.transaction() as conn:
            contribution = bills.reserve_contribution(
                conn,
                bill_id=bill_id,
                user_id=user["id"],
                amount_cents=body.amount_cents,
                provider_name=provider.name,
                settings=settings,
            )
            biller_name = bills.get_public_bill(conn, bill_id)["biller_name"]
    except bills.ContributionRejected as rejection:
        code = status.HTTP_429_TOO_MANY_REQUESTS if rejection.code == "rate_limited" else status.HTTP_409_CONFLICT
        raise HTTPException(
            code,
            {"code": rejection.code, "message": rejection.message, "available_cents": rejection.available_cents},
        ) from None

    # The hold is committed before calling the provider, so a slow payment API never blocks other givers.
    try:
        checkout = provider.start_checkout(
            contribution_id=contribution["id"],
            amount_cents=contribution["amount_cents"],
            biller_name=biller_name,
            expires_at=contribution["expires_at"],
        )
    except Exception:
        log.exception("Could not start checkout for contribution %s", contribution["id"])
        with db.transaction() as conn:
            bills.cancel_pending(conn, contribution["id"])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payments are unavailable right now") from None

    with db.transaction() as conn:
        bills.attach_checkout(conn, contribution["id"], checkout.provider_ref)

    closed_bill = False
    if checkout.checkout_url is None:
        closed_bill = _settle(
            provider, contribution["id"], payment_ref=checkout.payment_ref, paid_amount_cents=None
        ).closed_bill

    with db.transaction() as conn:
        return {
            "contribution": bills.get_contribution(conn, contribution["id"], user["id"]),
            "checkout_url": checkout.checkout_url,
            "closed_bill": closed_bill,
            "bill": bills.get_public_bill(conn, bill_id),
        }


@router.get("/contributions/{contribution_id}")
def get_contribution(contribution_id: UUID, user: CurrentUser) -> dict:
    with db.transaction() as conn:
        contribution = bills.get_contribution(conn, contribution_id, user["id"])
        if contribution is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Contribution not found")
        bill = bills.get_public_bill(conn, contribution["bill_id"])
        closed = conn.execute(
            "select closer_user_id = %s as closed from bills where id = %s", (user["id"], contribution["bill_id"])
        ).fetchone()["closed"]
    return {"contribution": contribution, "bill": bill, "closed_bill": bool(closed)}


@router.get("/contributions/{contribution_id}/receipt")
def payment_receipt(contribution_id: UUID, user: CurrentUser):
    with db.transaction() as conn:
        result = trust.receipt(conn, contribution_id, user["id"])
    if result is None:
        raise HTTPException(404, "Contribution not found")
    return result


@router.post("/contributions/{contribution_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_contribution(contribution_id: UUID, user: CurrentUser, request: Request) -> Response:
    """The giver backed out of checkout: release their hold now instead of when it times out."""
    with db.transaction() as conn:
        contribution = conn.execute(
            "select provider_ref, status from contributions where id = %s and user_id = %s",
            (contribution_id, user["id"]),
        ).fetchone()
        if contribution is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Contribution not found")
        if contribution["status"] != "pending":
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        bills.cancel_pending(conn, contribution_id)
    if contribution["provider_ref"]:
        try:
            request.app.state.payments.cancel_checkout(contribution["provider_ref"])
        except Exception:
            # Already paid or already closed. A late payment is still handled safely by _settle.
            log.warning("Could not close checkout for contribution %s", contribution_id, exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/leaderboard")
def get_leaderboard(user: CurrentUser, period: Literal["month", "all"] = "month") -> dict:
    with db.transaction() as conn:
        return bills.leaderboard(conn, user["id"], period=period)


# --- Payment webhooks --------------------------------------------------------


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    provider = request.app.state.payments
    if provider.name != "stripe":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    payload = await request.body()
    try:
        event = provider.parse_webhook(payload, request.headers.get("stripe-signature"))
    except (stripe.SignatureVerificationError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature") from None
    if event is None:
        return {"received": True}

    def accept():
        # Acknowledge only after the inbox and retry job commit. Processing can safely be retried.
        with db.transaction() as conn:
            event_id = events.receive(conn, provider.name, event, payload)
        try:
            events.process(provider, event_id)
            if event.kind == "paid":
                with db.transaction() as conn:
                    row = conn.execute("select status from contributions where id = %s", (event.contribution_id,)).fetchone()
                if row and row["status"] == "refund_pending":
                    refunds.process(provider, event.contribution_id)
        except Exception as error:
            log.warning("Provider event %s queued for retry (%s)", event_id, type(error).__name__)

    await run_in_threadpool(accept)
    return {"received": True}
