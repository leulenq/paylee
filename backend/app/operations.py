"""Staff-only operations API and console. Every mutation is authenticated and CSRF-protected."""

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from . import bills, db, documents, staff, trust, vault
from .config import get_settings
from .money import MAX_BILL_CENTS
from .security import admin_csrf_token, require_admin
from .web import _detect_upload_type, templates

router = APIRouter(prefix="/admin/ops", dependencies=[Depends(require_admin)])


def actor(request):
    value = request.state.staff
    staff.require_role(value, "reviewer", "payments", "treasury", "administrator")
    return value


@router.get("")
def console(request: Request):
    current = actor(request)
    return templates.TemplateResponse(request, "operations.html",
                                      {"csrf": admin_csrf_token(current["id"]), "actor": current})


@router.get("/state")
def state(request: Request):
    actor(request)
    with db.transaction() as conn:
        result = {
            "billers": conn.execute("select id, legal_name, category, status, official_domain, active_destination_id from billers order by created_at desc limit 100").fetchall(),
            "bills": conn.execute("""select b.id, b.biller_name, b.status, b.category, b.amount_due_cents,
                b.amount_raised_cents, b.document_id, b.verification_id, d.scan_status, d.extraction_status
                from bills b left join documents d on d.id = b.document_id order by b.created_at desc limit 100""").fetchall(),
            "documents": conn.execute("select id, purpose, scan_status, extraction_status, created_at from documents order by created_at desc limit 100").fetchall(),
            "evidence": conn.execute("select id, kind, subject, amount_cents, observed_at from evidence order by created_at desc limit 100").fetchall(),
            "contributions": conn.execute("""select id, bill_id, amount_cents, received_amount_cents, status,
                funds_reconciled_at, funds_reverification_required, refunded_at from contributions order by created_at desc limit 100""").fetchall(),
            "payments": conn.execute("select * from payment_orders order by created_at desc limit 100").fetchall(),
            "jobs": conn.execute("select id, kind, object_id, status, attempts, last_error from work_jobs where status <> 'done' order by created_at limit 100").fetchall(),
            "disputes": conn.execute("select external_id, contribution_id, status, amount_cents, updated_at from funding_disputes order by updated_at desc limit 100").fetchall(),
            "provider_events": conn.execute("select id, provider, external_id, event->>'kind' as kind, processed_at, created_at from provider_events order by created_at desc limit 100").fetchall(),
            "ledger": conn.execute("select bill_id, account, sum(signed_cents)::bigint as balance_cents from ledger_lines group by bill_id, account order by bill_id").fetchall(),
            "audit": conn.execute("select id, actor_id, action, object_id, created_at from audit_events order by id desc limit 50").fetchall(),
        }
    return result


class RetryInput(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: UUID, body: RetryInput, request: Request):
    current = actor(request)
    with db.transaction() as conn:
        job = conn.execute("select * from work_jobs where id = %s for update", (job_id,)).fetchone()
        if not job or job["status"] not in ("failed", "pending"):
            raise HTTPException(409, "Only failed or waiting jobs can be retried")
        staff.require_role(current, "reviewer" if job["kind"] == "document" else "treasury")
        conn.execute("""update work_jobs set status = 'pending', attempts = 0, available_at = now(),
                        lease_until = null, lease_token = null, last_error = null where id = %s""", (job_id,))
        # Reason is private and may contain sensitive investigation details; store it encrypted.
        trust.audit(conn, current, "job.retry_requested", job_id, reason_ciphertext=vault.encrypt(body.reason))
    return {"queued": True}


@router.post("/documents", status_code=201)
def upload_evidence(request: Request, file: Annotated[UploadFile, File()]):
    actor(request)
    content = file.file.read(get_settings().max_upload_bytes + 1)
    detected = _detect_upload_type(content[:16])
    if not detected:
        raise HTTPException(400, "Upload a PDF, PNG, JPEG, or HEIC document")
    with db.transaction() as conn:
        document = documents.store(conn, content, detected[0], "evidence")
        trust.audit(conn, request.state.staff, "document.uploaded", document["id"])
    return {"id": document["id"], "scan_status": document["scan_status"]}


@router.get("/documents/{document_id}")
def download_evidence(document_id: UUID, request: Request):
    current = actor(request)
    staff.require_role(current, "reviewer", "payments", "treasury")
    with db.transaction() as conn:
        document = conn.execute("select * from documents where id = %s", (document_id,)).fetchone()
        if not document or document["scan_status"] != "clean":
            raise HTTPException(409, "Only clean documents can be downloaded")
        trust.audit(conn, current, "document.accessed", document_id)
    return Response(documents.read(document), media_type=document["content_type"],
                    headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="evidence"',
                             "Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})


@router.get("/documents/{document_id}/extraction")
def extraction(document_id: UUID, request: Request):
    current = actor(request)
    staff.require_role(current, "reviewer")
    with db.transaction() as conn:
        document = conn.execute("select * from documents where id = %s", (document_id,)).fetchone()
        if not document or document["scan_status"] != "clean":
            raise HTTPException(409, "Document is unavailable")
        trust.audit(conn, current, "extraction.accessed", document_id)
    return {"status": document["extraction_status"],
            "blocks": json.loads(vault.decrypt(document["extraction_ciphertext"])) if document["extraction_ciphertext"] else []}


class EvidenceInput(BaseModel):
    kind: Literal["biller", "debt", "balance", "funding", "submission", "settlement", "posting", "return", "non_execution"]
    document_id: UUID
    reference: str = Field(min_length=3, max_length=200)
    source: str = Field(min_length=3, max_length=500)
    subject: str = Field(min_length=3, max_length=200)
    amount_cents: int | None = Field(default=None, gt=0, le=MAX_BILL_CENTS)
    observed_at: datetime


@router.post("/evidence", status_code=201)
def add_evidence(body: EvidenceInput, request: Request):
    with db.transaction() as conn:
        return trust.record_evidence(conn, actor(request), **body.model_dump())


class BillerInput(BaseModel):
    legal_name: str = Field(min_length=3, max_length=120)
    category: Literal["utilities", "internet", "phone", "rent", "medical", "other"]
    official_domain: str = Field(min_length=3, max_length=200)
    portal_url: str = Field(min_length=10, max_length=500)
    evidence_id: UUID
    institutional_creditor_confirmed: Literal[True]
    return_to_origin_agreement_confirmed: Literal[True]


@router.post("/billers", status_code=201)
def add_biller(body: BillerInput, request: Request):
    with db.transaction() as conn:
        return trust.create_biller(conn, actor(request), **body.model_dump(exclude={
            "institutional_creditor_confirmed", "return_to_origin_agreement_confirmed"}))


@router.post("/billers/{biller_id}/approve")
def approve_biller(biller_id: UUID, request: Request):
    with db.transaction() as conn:
        trust.approve_biller(conn, actor(request), biller_id)
    return {"status": "approved"}


@router.post("/billers/{biller_id}/suspend")
def suspend_biller(biller_id: UUID, request: Request):
    with db.transaction() as conn:
        trust.suspend_biller(conn, actor(request), biller_id)
    return {"status": "suspended"}


class DestinationInput(BaseModel):
    portal_url: str = Field(min_length=10, max_length=500)
    evidence_id: UUID


@router.post("/billers/{biller_id}/destination")
def change_destination(biller_id: UUID, body: DestinationInput, request: Request):
    with db.transaction() as conn:
        return trust.replace_destination(conn, actor(request), biller_id, **body.model_dump())


class VerificationInput(BaseModel):
    biller_id: UUID
    account_reference: str = Field(min_length=4, max_length=100)
    period_label: str = Field(min_length=3, max_length=100)
    verified_balance_cents: int = Field(gt=0, le=MAX_BILL_CENTS)
    evidence_id: UUID
    identity_and_account_holder_confirmed: Literal[True]
    independently_confirmed_debt: Literal[True]
    third_party_payment_and_refunds_confirmed: Literal[True]
    no_pending_insurance_or_refundable_deposits: Literal[True]


@router.post("/bills/{bill_id}/verify", status_code=201)
def verify_bill(bill_id: UUID, body: VerificationInput, request: Request):
    fields = {k: v for k, v in body.model_dump().items() if k in {
        "biller_id", "account_reference", "period_label", "verified_balance_cents", "evidence_id"}}
    with db.transaction() as conn:
        row = trust.verify_bill(conn, actor(request), bill_id, **fields)
    return {"id": row["id"], "expires_at": row["expires_at"], "account_last4": row["account_last4"]}


class ReviewInput(BaseModel):
    story: str = Field(min_length=10, max_length=800)
    amount_due_cents: int = Field(gt=0, le=MAX_BILL_CENTS)
    private_information_removed: Literal[True]


@router.post("/bills/{bill_id}/approve")
def approve_bill(bill_id: UUID, body: ReviewInput, request: Request):
    with db.transaction() as conn:
        approved = bills.approve(conn, bill_id, amount_due_cents=body.amount_due_cents,
                                 biller_name="", story=body.story, max_live=get_settings().max_live_bills,
                                 actor=actor(request))
    if not approved:
        raise HTTPException(409, "Bill has already been reviewed")
    return {"approved": True}


class EvidenceReference(BaseModel):
    evidence_id: UUID


@router.post("/contributions/{contribution_id}/reconcile")
def funding(contribution_id: UUID, body: EvidenceReference, request: Request):
    with db.transaction() as conn:
        trust.reconcile_funding(conn, actor(request), contribution_id, body.evidence_id)
    return {"reconciled": True}


class LegacyCollectionInput(EvidenceReference):
    received_amount_cents: int = Field(gt=0, le=MAX_BILL_CENTS)
    original_provider_collection_and_unspent_funds_confirmed: Literal[True]


@router.post("/contributions/{contribution_id}/import-legacy")
def legacy_collection(contribution_id: UUID, body: LegacyCollectionInput, request: Request):
    with db.transaction() as conn:
        trust.import_legacy_collection(conn, actor(request), contribution_id, body.evidence_id, body.received_amount_cents)
    return {"imported": True}


@router.post("/bills/{bill_id}/payments", status_code=201)
def prepare(bill_id: UUID, request: Request):
    with db.transaction() as conn:
        return trust.prepare_payment(conn, actor(request), bill_id)


@router.post("/payments/{order_id}/release")
def release(order_id: UUID, body: EvidenceReference, request: Request):
    with db.transaction() as conn:
        trust.release_payment(conn, actor(request), order_id, body.evidence_id)
    return {"status": "released"}


@router.post("/payments/{order_id}/claim")
def instruction(order_id: UUID, request: Request):
    with db.transaction() as conn:
        return trust.claim_execution(conn, actor(request), order_id)


class SubmissionInput(EvidenceReference):
    external_ref: str = Field(min_length=3, max_length=200)


@router.post("/payments/{order_id}/submit")
def submission(order_id: UUID, body: SubmissionInput, request: Request):
    with db.transaction() as conn:
        trust.record_submission(conn, actor(request), order_id, body.evidence_id, body.external_ref)
    return {"status": "submitted"}


class NonExecutionInput(EvidenceReference):
    no_payment_or_pending_instruction_confirmed: Literal[True]


@router.post("/payments/{order_id}/resolve-not-executed")
def non_execution(order_id: UUID, body: NonExecutionInput, request: Request):
    with db.transaction() as conn:
        trust.confirm_not_executed(conn, actor(request), order_id, body.evidence_id)
    return {"status": "canceled"}


@router.post("/payments/{order_id}/{action}")
def payment_action(order_id: UUID, action: Literal["settle", "apply", "return", "cancel"], request: Request,
                   body: EvidenceReference | None = None):
    with db.transaction() as conn:
        current = actor(request)
        if action == "cancel":
            trust.cancel_payment(conn, current, order_id)
        else:
            if not body:
                raise HTTPException(422, "Evidence is required")
            function = {"settle": trust.record_settlement, "apply": trust.record_posting, "return": trust.record_return}[action]
            function(conn, current, order_id, body.evidence_id)
    return {"recorded": True}
