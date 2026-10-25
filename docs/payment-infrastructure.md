# Paylee payment infrastructure

This implementation is a closed-network, human-operated bill-payment MVP. It adds the controls needed to verify debt, restrict destinations, release payment under two-person control, and distinguish collected money from confirmed biller posting.

**The supported execution rail is manual payment through an independently enrolled biller's official portal.** Claiming an instruction in Paylee does not send money. An authorized operator pays through that portal using the approved program's payment instrument, then submits evidence for independent reconciliation. There is no invented bill-pay API, recipient payout account, card issuer integration, or claim of existing commercial approval.

Stripe collects giver payments. A separate, approved banking/payment arrangement must supply the segregated program funds and the biller payment instrument. The `LIVE_PAYMENTS_APPROVED` setting records a deployment prerequisite; it does not obtain approval, create an account, or establish a licensing exemption.

## Running locally

The complete local topology has Postgres, the API, a durable worker, and ClamAV:

```sh
docker compose --profile app up -d --build
```

ClamAV can take several minutes to initialize its signatures and needs approximately 4 GB of memory. Its port is not exposed to the host. The official 1.4 image runs as amd64, including through Docker emulation on Apple Silicon.

The local Compose project keeps its historical `swipegofundme` resource name so renaming the repository to `paylee` does not create an empty replacement database or collide with the existing stack. Production uses its separately configured managed database.

If port 8000 is already occupied:

```sh
PAYLEE_HTTP_PORT=18080 docker compose --profile app up -d
```

Use the same port setting on subsequent Compose commands that recreate the API. The API listens on localhost only.

To populate a simulator's development account, read its `PayleeDevDeviceID` preference and run
`python -m app.seed_demo --device-id DEVICE_ID` in the API container. This repeatable command adds six
swipe bills, two open contributions, three applied-payment examples, one funded case, and one refund.
Everything is explicitly labeled Demo and uses synthetic, development-unverified evidence and disabled
fixture operators. It refuses production, real collection providers, external email/push delivery, or S3.
The ordinary `app.seed` command still creates unverified intake submissions only.

Provision three different people using the same runtime environment/encryption keys as the application:

```sh
docker compose --profile app run --rm api /srv/.venv/bin/python -m app.staff reviewer --roles reviewer
docker compose --profile app run --rm api /srv/.venv/bin/python -m app.staff payer --roles payments
docker compose --profile app run --rm api /srv/.venv/bin/python -m app.staff treasury --roles treasury
```

Each command prompts for a password and displays a one-time TOTP enrollment secret. Do not place passwords or authenticator secrets in command arguments or commit them. Staff passwords require 14 characters. Production sign-in requires MFA. TOTP codes cannot be reused. Session revocation is available with `--revoke-sessions --roles ROLE`.

Open `/admin/login`, then `/admin/ops`. The legacy shared development admin can inspect the old review console but cannot enroll billers, verify debt, release payments, or mark a bill paid.

For native development instead of containerized API/worker:

```sh
docker compose up -d --wait db
cd backend
uv sync
uv run python -m app.migrate
uv run uvicorn app.main:app --reload --port 8000
```

In a separate terminal, run `uv run python -m app.worker`. Configure a local `clamscan` binary with current signatures or `CLAMD_HOST` pointing to a private scanner. An explicit `MALWARE_SCANNER=development` can simulate scanning locally; production rejects it. `OCR_PROVIDER=manual` records that extraction needs a person. It never claims OCR or verification took place.

## Operator sequence

1. **Intake:** the requester uploads a bill and authorizes verification/payment. The file is encrypted, hashed, quarantined, and queued. No uploaded remittance instruction becomes a payment destination.
2. **Scan:** the worker performs ClamAV scanning. Failures stay quarantined and retry. Infected documents cannot be downloaded or used as evidence.
3. **Extract:** optional Textract extracts text/forms/tables and query results, preserving confidence, relationships, and source geometry in encrypted storage. HEIC and unsupported PDFs go to manual extraction. OCR never publishes a bill.
4. **Enroll the creditor:** a reviewer uploads evidence of legal identity, independently established official domain/portal, and the creditor's assistance/refund agreement. Record `biller` evidence with the official domain as its subject. Create the biller and its first immutable destination version.
5. **Approve the creditor:** a different payments operator independently checks and approves enrollment. Only institutional creditors with a documented return-to-origin assistance process are eligible. Individual landlords, related-party creditors, refundable deposits, and unverified merchants are outside the pilot.
6. **Verify debt:** a reviewer establishes requester identity/account relationship and independently contacts the biller or uses an authorized source. Record `debt` evidence scoped to the bill ID and eligible balance. Enter the verified account reference and canonical invoice/billing period. This creates an immutable verification expiring 48 hours after the source observation.
7. **Approve publication:** the reviewer removes private details from the public story and approves exactly the verified balance. Unverified, stale, unreviewed, suspended, or destination-changed bills cannot accept new contributions. Reverification requires a new review decision.
8. **Collect:** giver checkout reserves capacity under a bill lock. Completed collections post to `collection_receivable`, not available cash. Late, excess, or mismatched payments go to original-source refund handling.
9. **Reconcile funding:** treasury checks the provider settlement and segregated program account. Record `funding` evidence scoped to each contribution ID for its actual amount. Confirm that the full promised amount is available after fees/reserves. Reconciliation moves receivable into `program_cash`.
10. **Prepare:** a reviewer or payments operator prepares an immutable instruction. The full collected balance must be reconciled. Preparation freezes bill, verification, destination version, amount, currency, and a stable operation ID. Only one unresolved instruction can exist per bill.
11. **Release:** another payments operator approves using `balance` evidence scoped to the bill ID and observed within 15 minutes. They must differ from both the preparer and debt verifier. Reduced balances cannot be overpaid.
12. **Claim execution:** a payments operator claims the released instruction once. This exposes the approved portal and decrypted customer account. Claiming rechecks balance freshness, destination, verification, and disputes. The instruction becomes `executing` and blocks removal, refunds, or duplicate execution claims.
13. **Pay:** that operator pays the exact approved amount/account/period through the official portal with an approved program instrument. Do not give instruments or credentials to the requester. Upload the biller receipt and record `submission` evidence scoped to the payment ID and exact amount. The external biller reference must be unique.
14. **Settle:** independent treasury uploads rail/bank settlement evidence. An executor cannot reconcile their own payment.
15. **Confirm posting:** independent treasury obtains biller evidence that the exact amount was applied to the intended account. Only this transition sets the bill to `paid`, sends payment confirmation, and credits completion metrics for fully funded bills.

The JSON operations endpoints are under `/admin/ops`; their models appear in `/docs`. Browser mutations require the per-staff CSRF token supplied by the console. Donor endpoints retain their existing authentication and contribution contract.

Authenticated requesters use `/api/requests` for multipart upload and their own history/detail, with
`/{id}/withdraw` and `/{id}/balance` for lifecycle updates. Ownership is immutable once assigned; a
role switch never permits funding an owned bill. Web and mobile intake share validation and encrypted
quarantine. A reported lower balance pauses giving and payment release and requires fresh independent
evidence plus review. Reviewers see these cases and encrypted report notes in the operations console.
The additive `requester_schema.sql` migration must be applied before the updated runtime grants.

## Evidence and privacy

Every observation requires a clean supporting document, evidence type, subject, observed amount, observation time, source, and staff identity. Evidence is append-only. A corrected observation is a new record.

Subjects are deliberately specific:

| Evidence | Subject | Amount |
|---|---|---|
| Biller enrollment/destination | Official domain | Omitted |
| Debt or fresh balance | Bill ID | Confirmed eligible outstanding balance |
| Funding settlement | Contribution ID | Actual collected/available amount |
| Submission, settlement, posting, full return | Payment ID | Exact instruction amount |
| Non-execution | Payment ID | Instruction amount confirmed unexecuted |

Human reviewers must inspect the document and verify the assertions; a scanned receipt alone does not establish its truth. The software checks scope, amount, chronology, identity, and state. It cannot independently establish a landlord's honesty or enforce a utility's refund agreement.

Files are encrypted before local/S3 storage, with SHA-256 provenance. Original bytes, storage key, size, and content type cannot be changed in place. S3 additionally uses a configured customer-managed KMS key. Review downloads require authentication, cannot bypass scanning, are served as attachments with sandbox/no-store headers, and are audited.

Account references and OCR output use application encryption. Account/debt fingerprints use keyed HMACs. Keep encryption and fingerprint keys separate and stable; retain their recoverable backups. Rotating a fingerprint key without migration defeats duplicate detection. Key rotation and retention deletion require an explicit reviewed migration.

Givers never receive uploads, evidence documents, source contacts, internal notes, full account references, or destination details. `GET /api/contributions/{id}/receipt` is owner-only and exposes contribution amount, total bill payment, masked account ending, payment stage, and timestamps. It is a status receipt, not a tax-deductible charity acknowledgment. Independent evidence remains available to authorized operations/auditors.

## Payment and exception states

```text
prepared → released → executing → submitted → settled → applied
    ↓           ↓          ↓                       ↓        ↓
canceled   independently confirmed          full return → returned
             non-execution → canceled                     ↓
                                                 original-source refunds
```

- A released/claimed instruction is never automatically retried, failed over, or canceled on timeout. Treasury must first establish that no payment or pending instruction exists. `resolve-not-executed` requires independent evidence and an explicit assertion. A missing receipt is insufficient.
- Recording submission is allowed after actual execution even if the portal response was delayed. Do not invent a second instruction to work around a stale screen.
- Original-source refunds use stable provider idempotency keys and recover existing provider refunds after a crash. Pending provider refunds remain pending; they do not generate a completed refund event.
- Removing a bill cannot refund funding while payment may be executing. Unreleased preparations are canceled before removal.
- A full biller return requires independent confirmation that the program received the money, including returns after posting. It reopens the restricted obligation and queues giver refunds. It does not route funds to the requester or another bill.
- A changed balance below collected funds blocks execution. For the MVP, remove the unexecuted bill, return contributions, and start a newly verified case. Partial refunds/reallocation are not silently performed.
- A changed destination requires another enrollment approval and bill reverification. Existing instructions cannot edit their destination or amount.
- Dispute webhooks create explicit funding holds that block collection and new payment execution. Closed disputes cannot be reopened by an out-of-order creation event. A `won` notice still requires treasury funding evidence observed after the latest dispute update before execution can resume.
- **Lost/open chargebacks, partial biller returns, and overlapping chargeback/return cases require treasury investigation.** These are held; Paylee does not automatically net them, book a fictitious recovery, debit the requester, or issue a second refund. Treasury must reconcile the provider statement, operating loss reserve, and any bank debits before fulfillment resumes. The MVP ledger does not automate chargeback-loss/reserve accounting.
- An applied bill with only a partial payment is described as a payment applied, rather than paid off. Only full applied bills count toward completed-bill metrics.

## Accounting and durability

Each immutable journal has a debit account, credit account, positive integer amount, currency, and unique business event key. The database cannot represent an unbalanced single journal. `ledger_lines` exposes both sides.

| Event | Debit | Credit |
|---|---|---|
| Confirmed collection | collection_receivable | restricted_payable |
| Treasury funding reconciliation | program_cash | collection_receivable |
| Submitted biller payment | payout_in_transit | program_cash |
| Applied to the account | restricted_payable | payout_in_transit |
| Return before posting | program_cash | payout_in_transit |
| Return after posting | program_cash | restricted_payable |
| Original-source refund | restricted_payable | program_cash or collection_receivable |

Fees, chargeback losses, reserves, and Paylee revenue must be separately financed and reconciled. These journals are an operational subledger, not a complete company general ledger or a bank-generated proof of available balance.

Stripe signatures are verified before an event enters a durable inbox. Only normalized payment fields and a payload hash are retained. Stable event IDs prevent duplicate processing; conflicting data for the same ID is rejected. Processing failures remain retryable after acknowledgment.

The Postgres work queue uses `FOR UPDATE SKIP LOCKED`, five-minute leases, lease tokens, retry backoff, and terminal failed jobs. A stale worker cannot complete a new worker's lease. Provider settlement is independently reconciled rather than inferred from webhook delivery. Job errors store categories instead of potentially sensitive exception text.

After investigating the cause, an authorized operator can requeue a failed/waiting job in the console. Reviewers handle documents; treasury handles provider events/refunds. The reason is encrypted and the action audited. This does not reissue a manual biller payment or bypass an uncertain instruction.

## Production deployment

1. Obtain written approval for the complete collection, holding, third-party bill payment, refund, and loss model. Establish the actual segregated funds program and biller instrument. Clear applicable legal, privacy, and medical-data requirements.
2. Use a separate production database with encryption, private networking, TLS, point-in-time recovery, and tested restores. Do not reuse development evidence, staff secrets, or fabricated sample balances.
3. Run `uv run python -m app.migrate` using database-owner credentials before deployment. Existing tables are migrated additively. API/worker runtime uses `SCHEMA_AUTO_APPLY=false`.
4. Create a non-owner runtime login and apply `backend/runtime-grants.sql` using the migration owner. The runtime role must not inherit the owner role, disable triggers, or possess superuser/CREATEROLE privileges. It cannot update/delete journals, evidence, destinations, verifications, reviews, or audit records. Provision staff with owner credentials, not the public application.
5. Configure a private S3 bucket with block-public-access, bucket-owner-enforced ownership, versioning, TLS-only policy, SSE-KMS, and scoped task-role access. The runtime needs private-object Get/Put, KMS Encrypt/Decrypt/GenerateDataKey, and optional Textract AnalyzeDocument. Do not give it bucket-policy or IAM administration rights. Enable audit access logging and organization-appropriate retention.
6. Distribute `SESSION_SECRET`, `DATA_ENCRYPTION_KEY`, and `ACCOUNT_FINGERPRINT_KEY` consistently to API, worker, and staff provisioning through a secret manager. Do not bake secrets into images.
7. Set approved Stripe credentials/webhook secret, SMTP configuration, HTTPS `PUBLIC_BASE_URL`, `LIVE_PAYMENTS_APPROVED=true`, and the actual `FUNDS_PROGRAM_REFERENCE`. These are explicit attestations of completed external onboarding.
8. Place a trusted HTTPS ingress/WAF in front of the API. The included container command trusts proxy headers; the app port must not be publicly reachable outside that ingress. Keep the MVP at one API replica because request rate limits are in memory. Move rate limits to shared storage before increasing replicas.
9. Use `docker-compose.production.yml` as a standalone container topology, with `backend/.env.production` or your orchestrator's secret mechanism. It uses external Postgres/S3, read-only application filesystems, private ClamAV, and bounded logs. No infrastructure is deployed to a cloud account by this repository change.
10. Register Stripe events: `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired`, `charge.dispute.created`, and `charge.dispute.closed`. The current checkout intentionally permits cards/Apple Pay; ACH collection is not enabled merely because async events are handled.
11. Exercise a small live pilot with each approved biller: ordinary posting, rejection, uncertain portal response, full return, closed account, and refund routing. Only then admit it for real gifts.

Production startup rejects development login, development scanning, local document storage, missing secrets/SMTP/Stripe configuration, automatic schema ownership, and an undeclared funds program. These checks are necessary deployment guards, not evidence of provider approval.

## Existing data

The migration does not invent verification, backfill financial evidence, or relabel old manual confirmations as independently reconciled payments. Legacy live bills without a verified route disappear from the eligible feed and cannot receive contributions.

For an unspent legacy collection, treasury can record independent provider evidence and use `/admin/ops/contributions/{id}/import-legacy`. It requires the original payment reference and confirmed actual amount. Then reconcile funding or issue the original-source refund. Historical paid bills require separate finance review; importing them as unspent cash is blocked. Preserve financial records and recover old upload evidence through an authorized migration instead of bypassing quarantine.

## Daily operations and monitoring

At least daily, treasury compares provider collections/refunds, program bank balances, outgoing transfers, the operational ledger, and biller posting confirmations. Attach statement evidence to reconciliation. Reconcile fees and reserves in the company's financial books, with explicit investigation of discrepancies.

Monitor `/admin/ops` records for:

- Failed/retrying document, provider-event, and refund jobs.
- Unprocessed provider events, open/lost disputes, and funding awaiting settlement.
- Released or executing instructions whose outcome is uncertain.
- Submitted payments without settlement, settled payments without posting, and returned payments without completed refunds.
- Suspended billers, stale verifications, and destination changes.

Use `/health` for process health; it does not certify payment readiness. Set external alerts on worker/job age and reconciliation exceptions. Export append-only audit records to separately controlled storage and have an independent reviewer examine controls. A database owner can alter a database; append-only runtime permissions do not replace external audit retention.

## Verification

```sh
cd backend
uv run pytest -q
uv run ruff check --isolated --select E4,E7,E9,F,I app tests
```

Tests cover the original application plus document quarantine/integrity, OCR provenance, encrypted S3 behavior, individual staff/MFA/CSRF, debt deduplication, funding/ledger boundaries, concurrent reservations/preparations, self-release prevention, destination changes, one-time execution claims, immutable instructions/evidence, posting receipts, pending refunds, full returns, durable provider-event recovery, and dispute holds. AWS/Stripe unit integrations use test doubles; their live commercial integrations require separate acceptance testing. ClamAV has also been exercised against a real local daemon with clean content and the standard EICAR test signature.
