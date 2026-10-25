# API contract: requestor side, roles, and profile settings

The requester endpoints are implemented in `backend/app/requester_api.py`. The app uses the live API;
preview data requires an explicit debug launch argument and is never activated by a failed request.
JSON keys are snake_case; money is integer cents; timestamps are
ISO 8601 with offset; dates are `YYYY-MM-DD`. Auth is the existing `Authorization: Bearer <session token>`.

The invariants in `CLAUDE.md` apply unchanged. In particular: money only ever goes to the verified biller,
requesters never see passes, skips, or who gave, and payers never see requester contact details.

## 1. Roles

One account, one active side at a time. The side only changes what the app shows; it is not a permission
boundary by itself. The backend must still refuse a contribution from a user to a bill that user submitted.

### `GET /api/me` — add one field

```json
{ "role": "payer" }        // "payer" | "requestor" | null (not chosen yet)
```

### `PUT /api/me/role`

Body `{ "role": "payer" | "requestor" }`. Returns `204`. Idempotent.

## 2. Requests (a requestor's own bills)

A "request" is a bill seen from the requestor's side. Same `bills` row; a different projection.

### `GET /api/requests`

```json
{
  "requests": [
    {
      "id": "uuid",
      "status": "submitted | in_review | needs_info | rejected | queued | live | funded | paid | expired | withdrawn | removed",
      "category": "utilities",
      "biller_name": "Kentucky Utilities",
      "claimed_amount_cents": 21250,
      "verified_amount_cents": 21250,      // null until a reviewer verifies the balance
      "amount_raised_cents": 8000,          // 0 until live
      "due_date": "2026-10-29",
      "story": "My hours got cut ...",       // the reviewed story givers see once approved
      "first_name": "Maria",
      "city": "Lexington",
      "state": "KY",
      "submitted_at": "2026-10-24T19:10:11Z",
      "live_at": null,
      "funded_at": null,
      "paid_at": null,                        // set only when payment to the biller is confirmed by evidence
      "account_ending": "4821",               // last 4 of the biller account, once verified; else null
      "status_note": null,                    // short requester-safe text, e.g. "The photo was blurry. Please send a clearer one."
      "can_withdraw": true,
      "can_update_balance": true
    }
  ]
}
```

Rules:
- Never include who contributed, how many passes or swipes the bill got, its feed position, report details,
  or internal review notes. `status_note` is written for the requester, not copied from staff notes.
- `amount_raised_cents` is fine to show the requester (it's their progress).

### `GET /api/requests/{id}`

The same object. `404` unless the bill belongs to the caller.

### `POST /api/requests` (multipart/form-data)

Fields: `first_name`, `email`, `city`, `state` (two-letter), `category`, `biller_name`, `amount_cents`
(integer), `due_date`, `story` (40–800 chars), `consent` (`true`), `bill_file` (PDF/JPG/PNG/HEIC, ≤ 10 MB).
Same validation as the web form. Returns `201` with the request object. Validation errors: `422` with
`{"detail": {"field": "message", ...}}` so the app can show them next to each field.

### `POST /api/requests/{id}/withdraw`

Allowed while `submitted`, `in_review`, `needs_info`, `queued`, or `live`. Any contributions already made are
queued for durable refunds to their original payment methods (same path as admin removal). Returns the
updated request immediately; it does not claim a pending refund has completed. Repeating a completed
withdrawal is idempotent. A released, executing, submitted, or settled payment blocks withdrawal until
treasury resolves it. Late collections for canceled checkouts are refunded rather than applied.

### `POST /api/requests/{id}/balance`

Body `{ "amount_cents": 15000, "note": "I paid $62.50 myself" }`. The requester reports a lower balance;
zero is allowed when they paid the whole bill. Increases are rejected, and duplicate equal reports are
idempotent. The backend preserves `amount_due_cents` and all collected amounts until independent review.
It moves the bill to `in_review` and pauses all new gifts and payment release. Unreleased preparations
are canceled, but uncertain outgoing payments are preserved for treasury investigation.

Resuming requires fresh biller evidence observed after the latest report and a new reviewer approval.
The amount cannot be lowered below collected funds until excess funding is resolved. The MVP can close
the case and refund all eligible gifts; it does not silently redistribute or partially refund them.
Expired postings do not reopen for funding after a balance review. Notes are encrypted, and staff can
read them through the authenticated operations console; private staff notes never become `status_note`.

Ownership comes from the authenticated account, never from an uploaded field, role choice, or matching
email address. Anonymous web/legacy submissions are not automatically claimed by an app account.
Duplicate-debt protection still applies to a resubmission: a closed prior case releases its active claim
only after all collections/refunds are resolved and no outgoing payment remains active.

## 3. Profile settings

### `GET /api/me/settings` / `PUT /api/me/settings`

```json
{
  "show_on_leaderboards": true,     // false = excluded from /api/leaderboard entirely (still ranked privately)
  "show_city_on_share_cards": true,
  "notify_payment_facts": true,     // pushes about bills you funded (covered, paid, refunded)
  "notify_open_bills": true,        // pushes like "$40 left on a bill you started, due Thursday"
  "practice": {                     // a giving rhythm the payer chose; null = none
    "cadence": "weekly | monthly",
    "bills_per_period": 1,
    "monthly_limit_cents": 10000,   // optional ceiling; unused money is never "owed"
    "start_day": "sunday | payday | first_of_month"
  }
}
```

`PUT` takes the whole object and returns it. The app also keeps a local copy, so this can land late.

### `GET /api/me/rank`

```json
{ "rank": 12, "of": 340, "period": "month", "bills_completed": 4, "visible": true }
```

Rank among givers by bills helped to paid off (never dollars). `rank` is present even when
`visible` is false; only the giver sees their own private rank.

## 4. Open bills (a payer's unfinished bills)

Nice to have; the app currently derives this from `GET /api/me/contributions` plus `GET /api/bills/{id}`.
A single call would save N requests:

### `GET /api/me/open`

Bills the caller has a succeeded contribution on that are still `live`, with `due_date`, `city`,
`amount_due_cents`, `amount_raised_cents`, and `my_total_cents`, ordered by due date.

## 5. Notifications the app expects (pushes, payload carries `bill_id`)

- Payer, fact-only: "$40 left on the Atmos Energy bill you started. Due Thursday." Only when the remainder
  is small and the due date is within 3 days. At most one per bill.
- Payer, second beat: "Kentucky Utilities confirmed the $212.50 payment." Only from posting evidence.
- Requestor: status changes (in review, needs info, live, covered, paid). Same moments as the existing emails.

Never send pushes that use the requester as pressure ("Maria is counting on you").
