# Paylee

Swipe through real, verified bills and chip in. Money is paid directly to the company on the bill, never to a person's bank account.

- `SwipeBills/`: iOS app for givers (SwiftUI, iOS 26). The Xcode target is still named SwipeBills; the app's display name is Paylee.
- `backend/`: FastAPI + Postgres. JSON API for the app, the intake form for people asking for help, public bill pages for sharing, the admin console, and requester emails.

The controlled-payment MVP includes verified biller enrollment, private document quarantine, optional Textract OCR, staff MFA, two-person payment release, an append-only operational ledger, durable jobs/webhooks, posting evidence, and owner-only receipts. See the [payment infrastructure runbook](docs/payment-infrastructure.md) for the operator sequence, production configuration, accounting boundaries, and launch requirements.

## Run it locally

Requirements: Docker Desktop, [uv](https://docs.astral.sh/uv/), Xcode 26.

```sh
docker compose up -d --wait          # Postgres on 127.0.0.1:5433 (also creates the test database)
cd backend
uv sync
uv run python -m app.seed            # optional: six UNVERIFIED sample submissions
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Then open `SwipeBills/SwipeBills.xcodeproj`, run on an iPhone simulator, and tap **Continue in development mode**.

Run `uv run python -m app.worker` separately and configure ClamAV before opening uploads. Alternatively, `docker compose --profile app up -d --build` starts the API, worker, private ClamAV, and database together. Individual staff accounts are provisioned with `python -m app.staff`; follow the runbook. New and legacy bills do not enter the feed without independent verification.

| Page | URL |
|---|---|
| Intake form (people asking for help) | http://127.0.0.1:8000/submit |
| Admin console (user `admin`, password `admin` in development) | http://127.0.0.1:8000/admin |
| Individual staff sign-in / payment operations | http://127.0.0.1:8000/admin/login |
| Public page for a bill (what shared links open) | http://127.0.0.1:8000/b/&lt;bill id&gt; |
| API docs | http://127.0.0.1:8000/docs |

Typical loop: upload → scan/extract → independently enroll biller and verify debt → approve publication → collect gifts → reconcile available funding → prepare/release/claim payment → manually pay the approved portal → independently reconcile settlement and account posting. A typed confirmation number can no longer mark a bill paid. In development, requester emails are printed to the server log.

## Tests

```sh
cd backend && uv run pytest
```

## What happens to a bill

1. A requester submits a bill and photo on `/submit`. They get a "we received your bill" email.
2. Individual operators use `/admin/ops` to verify the creditor, controlled refund process, account, current debt, and supporting evidence. A reviewer edits private details out of the story and approves the verified balance. The bill goes live or onto a waitlist when `MAX_LIVE_BILLS` is full.
3. Givers swipe: right to chip in, up to close the whole bill, left to pass (with Rewind). Their deck follows the causes, state, and bill size they chose in onboarding or the filter sheet. Each contribution is capped at the remaining balance, with a short hold while they pay.
4. When fully funded, requesters and givers are notified that funding is covered. Funding alone is not a completed bill payment.
5. Treasury reconciles available money. A payment is prepared, independently released, and claimed once for manual execution through the verified biller portal. Independent settlement and account-posting evidence are required before the bill is reported paid. Only full applied payments count toward completed-bill metrics and tiers.

Bills past their due date plus `EXPIRY_GRACE_DAYS` expire. Partial funding is paid only if the debt and route remain eligible; otherwise contributions are refunded. Removal cannot refund funding while an outgoing payment may already be executing. Pending refunds remain pending until the provider confirms completion.

## Deploying

`backend/Dockerfile` builds both API and worker. The standalone `docker-compose.production.yml` uses external Postgres and private S3/KMS, read-only application containers, and private ClamAV. Keep one API process/replica until rate limits move to shared storage. Apply migrations with separate database-owner credentials and use `backend/runtime-grants.sql` for the non-owner runtime role.

The server refuses to start with `ENVIRONMENT=production` unless all of these are true:

- `SESSION_SECRET` is 32+ random characters; production staff use individual MFA accounts, not `ADMIN_PASSWORD`
- `ALLOW_DEV_LOGIN=false`
- `SCHEMA_AUTO_APPLY=false`
- `PAYMENT_PROVIDER=stripe` with `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`
- `EMAIL_PROVIDER=smtp` with `SMTP_HOST` and `EMAIL_FROM` (Postmark, SES, Resend, and others offer SMTP)
- `PUBLIC_BASE_URL` is `https://`
- Valid `DATA_ENCRYPTION_KEY`, a separate 32+ character `ACCOUNT_FINGERPRINT_KEY`, private S3/KMS, and real ClamAV scanning are configured
- `LIVE_PAYMENTS_APPROVED=true` and the actual `FUNDS_PROGRAM_REFERENCE` declare completed external onboarding

Stripe: point a webhook at `POST /api/stripe/webhook` for completed/expired checkout, async success, and dispute created/closed events. Live onboarding must approve this complete funding model. Biller payments currently use the controlled manual portal workflow; no universal electronic bill-pay network is connected.

Push notifications (optional): create an APNs auth key (.p8) in your Apple Developer account and set `PUSH_PROVIDER=apns`, `APNS_KEY_ID`, `APNS_TEAM_ID`, and `APNS_PRIVATE_KEY` (the file's contents). Without them, pushes are written to the server log. Before App Store release, change `aps-environment` in `SwipeBills/Paylee.entitlements` to `production`.

## Before submitting to the App Store

1. In Xcode, select your team and add the **Sign in with Apple** capability (the entitlement file is already in place).
2. Set the production API and web hosts in `SwipeBills/SwipeBills/Networking/AppConfig.swift`.
3. Fill in App Store Connect's privacy answers to match `SwipeBills/SwipeBills/PrivacyInfo.xcprivacy` (user ID, purchase history, and reports; no tracking).
4. Complete payments/legal onboarding and verify Apple's applicable payment rules. Exercise each biller's posting and return-to-origin process before admitting live gifts.
