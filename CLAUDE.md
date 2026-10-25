# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Product

Paylee: a Tinder-style app where givers swipe through real bills (rent, utilities, medical, phone) and chip in. Money is paid to the biller on the bill, never to the requester. iOS first for givers; requesters use a web form (they skew Android). Android comes later as another thin client.

Invariants the code must preserve:
- Money goes to the biller on the bill, never to the requester's bank account.
- A contribution never exceeds the bill's verified remaining balance. Overpaying a biller gets refunded to the account holder, which turns the app into a cash channel.
- Requester email, bill uploads, and review notes never reach givers. Givers never see the bill image at all; a reviewer edits PII out of the story before approval.
- Requesters never see skip/left-swipe counts or rankings. Leaderboards rank bills completed, never dollar totals.

## Commands

Postgres runs in Docker (`docker compose up -d --wait` from the repo root) on 127.0.0.1:5433. The test database `swipebills_test` is created by `backend/db-init/` on first volume creation only; if the volume predates that, run `docker compose exec db createdb -U swipebills swipebills_test`.

Backend (from `backend/`, uses uv):

```
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
uv run pytest                                   # all tests (needs Postgres running)
uv run pytest tests/test_contributions.py -k concurrent
uv run python -m app.seed                       # unverified sample submissions for development
```

iOS (from `SwipeBills/`):

```
xcodebuild -project SwipeBills.xcodeproj -scheme SwipeBills -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
```

Debug launch arguments: `-PayleeDevAutoLogin YES` skips sign-in, `-PayleeShowOnboarding YES|NO` replays or skips onboarding, `-PayleeInitialTab bills|impact|leaders`, `-PayleeOpenSheet detail|contribute|closeit|filters|sharecard`, `-PayleeAPIBaseURL <url>` points at another server. Test a push with `xcrun simctl push "iPhone 17 Pro" enquanhone.SwipeBills payload.json` (include `bill_id` to open that bill on tap). There is no iOS test target.

## Architecture

All rules live in the backend; the iOS app is a thin client so Android can be added without re-implementing logic.

**Trust infrastructure:** Read `docs/payment-infrastructure.md` before changing funding or biller payment behavior. `trust.py`, `operations.py`, `documents.py`, `ledger.py`, `events.py`, `jobs.py`, `refunds.py`, and `staff.py` implement the closed biller network, evidence, individual MFA accounts, and controlled manual payment workflow. `trust_schema.sql` is an additive migration. Never restore a checkbox-only approval or typed-confirmation-only payment path. Donor receipts are owner-only, and public columns include verification timestamps but no private evidence.

**Requester backend:** `requester_api.py`, `requests.py`, and `intake.py` implement the authenticated contract in `docs/api/requestor-and-profile.md`; `requester_schema.sql` adds ownership, review pauses, and immutable encrypted balance reports. Never link legacy requests by email or trust a submitted owner ID. Requesters cannot fund their own bills, even after switching roles. Balance reports pause all giving/release and require fresh evidence plus approval to resume. Withdrawals queue refunds and preserve uncertain outgoing payments. iOS clears requester caches on account changes and never silently falls back to preview data.

**Money flow** (`backend/app/bills.py`, `api.py`, `payments.py`):
1. `reserve_contribution` locks the bill row (`SELECT ... FOR UPDATE`), subtracts raised amounts and unexpired `pending` holds, and inserts a `pending` contribution with `expires_at`. That transaction commits before any payment API call, so a slow provider never holds the lock.
2. The provider starts checkout. `FakeProvider` (dev) settles immediately; `StripeProvider` returns a Checkout URL the app opens in `ASWebAuthenticationSession`, which redirects through `/checkout/return` to `paylee://checkout`.
3. `_settle` in `api.py` calls `confirm_payment` (from the fake path or the Stripe webhook). It is idempotent, locks bill then contribution (same order as reserve, to avoid deadlocks), and if the bill can no longer take the money it marks `refund_pending` and refunds instead of overpaying. A DB check constraint (`amount_raised_cents <= amount_due_cents`) is the backstop.
4. Closing fundraising sets `funded` and `closer_user_id`. Treasury reconciles actual available funding before an immutable payment instruction can be prepared. A second person releases it against a fresh balance. An operator claims execution once and pays the enrolled biller portal manually. Independent settlement and account-posting evidence are required before setting `paid`. Funded is not paid; only full applied payments count toward completed-bill metrics.

**Bill lifecycle:** `submitted` → (admin) `queued` → `live` → `funded` → `paid`; also `rejected`, `expired`, `removed`. `promote_queued` caps live bills at `MAX_LIVE_BILLS` under an advisory lock (supply control). `expire_stale` runs on every feed request. Removing a bill refunds its succeeded contributions.

**Feed:** live bills, excluding the giver's recent passes and bills they already funded, ordered by `times_shown` (fair rotation) then due date. `PUBLIC_COLUMNS` in `bills.py` is the only column list exposed to givers.

**Requester emails** (`notify.py`): lifecycle functions in `bills.py` call `notify.bill_event(conn, bill_id, event)`, which writes to the `emails` outbox in the same transaction (events: submitted, queued, live, rejected, funded, paid, expired, removed). `OutboxWorker` (a thread started in `main.lifespan`) sends them via `ConsoleMailer` (dev, logs) or `SMTPMailer`, retrying up to 5 times. Tests disable the worker and call `deliver_pending` directly. When adding a bill state change, add its email here too.

**Giver pushes:** `notify.givers_of_bill` (funded, paid) and `notify.giver_refunded` queue rows in the `pushes` outbox; the same `OutboxWorker` sends them to every `device_tokens` row for the user via `LogPusher` (dev) or `APNsPusher` (token-auth JWT over HTTP/2), deleting tokens Apple reports dead. The payload carries `bill_id`, which the app opens on tap.

**Discovery:** the feed accepts `category` (repeatable), `state`, `max_remaining_cents`, and `exclude` (bills already in the deck, for paging). `bills.state` is a two-letter code; the intake form collects it and `schema.sql` backfills it from the "City, ST" display string. `DELETE /api/bills/<id>/pass` is Rewind. Member tiers (`bills.TIERS`) count bills helped to paid off, never dollars.

**Giver activity:** `bills.activity` derives events (funded, paid, refunded) from bill and contribution timestamps rather than storing them; `users.activity_seen_at` drives the unread count.

**Other backend pieces:** `web.py` serves intake, public bill pages, and the legacy review console. Uploads are encrypted and quarantined; the separate `python -m app.worker` scans them and optionally extracts with Textract. Production operations use individual staff sessions, TOTP MFA, CSRF, and explicit roles. Shared Basic auth exists only in development and cannot perform trust operations. Rate limiters remain in memory, so keep one API replica. Production configuration requires an approved funds program, private S3/KMS, real ClamAV, independent encryption/fingerprint keys, and `SCHEMA_AUTO_APPLY=false`. Apply additive SQL with `python -m app.migrate` under a separate owner; `runtime-grants.sql` restricts the app role. Development still applies idempotent schema at startup. Web pages self-host fonts from `app/static/fonts`.

**iOS** (`SwipeBills/SwipeBills/`): `AppDelegate` owns the `AppModel` (session in Keychain, `APIClient`, push token, onboarding flag) because push callbacks arrive through UIKit. `FeedModel` owns the deck: it pages in more bills when three are left, applies `DiscoveryPreferences` (stored on device, set in `OnboardingView` and `PreferencesSheet`), and remembers the last pass for Rewind. Swipes are right = give, up = close it (contribute sheet pre-filled with the full balance), left = pass; the detail screen is a full-screen cover that zooms out of the card via `matchedTransitionSource`. `SwipeDeckView` only handles gestures and reports swipes; `FeedView` decides what a swipe means (left = pass, right = contribute sheet; backing out puts the card back). Money is integer cents everywhere (`amountDueCents`), formatted only for display via `Money`. The project uses synchronized folders, so new files need no `project.pbxproj` edits, and default actor isolation is MainActor.

## Design system

Bold fintech: always dark (`.preferredColorScheme(.dark)` in `PayleeApp`), a tinted near-black canvas, off-white ink, huge wide numbers, and one accent (lime #C8F560). The user explicitly wants nothing that reads as AI-generated, so these rules are deliberate:
- Lime only for actions and progress (give, close it, primary buttons, progress bars, "You"/"New" markers). Selected states for choices (segments, filter chips) are ink, not lime.
- No gradients, glows, blur, drop shadows, confetti, or bounce/elastic easing (`Motion.settle` / `Motion.quick` decelerate without overshoot). Depth comes from canvas → surface → raised tones.
- No nested cards; group with hairlines (`Hairline`) and spacing. Left-aligned layouts; center only the amount entry. Radii vary by role (`Radius`).
- No tracked-caps eyebrows, italic for decoration, status dots without a state, or "big number over small label" stat rows (Impact writes its numbers into a sentence).
- Celebrations are typographic: the lime full-bleed success screen and `TierUpView`.

Build screens from `SwipeBills/SwipeBills/Views/DesignSystem/`:
- `Typeface`: Archivo Expanded (`display(size, weight)`, ExtraBold/Black) for money, headlines, and stamps; Archivo (`ui(size, weight)`) for everything else. Static instances generated from the OFL variable font live in `Fonts/` and are registered in `PayleeApp.init`. Always pass `relativeTo:` so Dynamic Type works.
- `Theme`: `CanvasBackground`, `SurfaceBackground`, `TierStyle`/`TierCardBackground` (flat card finishes per tier), `Radius`, `Motion`, `.statusBarBackdrop()` (required on every scrolling screen so content doesn't run under the clock).
- `Components`: `AmountText` (dollars in ink, cents dimmed), `ScreenTitle`, `SectionLabel`, `PayleeMark`/`PayleeWordmark`, `.primary`/`.secondary`/`.pressable` button styles, `CircleIconButton`, `SegmentedPicker`, `ProgressBar`, `Hairline`.

Colors are single-value asset-catalog colors used through generated symbols: `Color.canvas`, `.surface`, `.raised`, `.line`, `.ink`, `.inkMuted`, `.inkFaint` (decorative only, fails text contrast), `.lime`, `.onLime`, `.danger`, `.tierGold`, `.tierPlatinum`.

`MainTabView` keeps all three tabs alive in a `ZStack` above a flat bottom `FloatingTabBar`; Impact and Leaders take `isActive` and reload when shown. Debug launch argument `-PayleeSeenTier Member` replays the tier-up screen.

**Links and sharing:** the app's URL scheme is `paylee` (declared in `SwipeBills/Paylee-Info.plist`, which is merged with Xcode's generated Info.plist and also defines the launch screen). `paylee://bill/<id>` sets `AppModel.linkedBillID`, and `FeedView` opens that bill. The detail sheet shares `AppConfig.shareURL` (the backend's `/b/<id>` page). After giving, `ShareCard` renders a 1080x1350 image that names only the category and city, never the person or the company. `PrivacyInfo.xcprivacy` must be updated if the app starts collecting new data types or using other required-reason APIs.

The web templates (`backend/app/templates/base.html`) still use the previous navy/gold palette and need the same redesign.

## Repo note

The project now lives at `/Users/lenquanhone/Projects/paylee` and has its own `.git` directory. Respect staged and concurrent changes. Local Compose retains the `swipegofundme` project name to preserve the existing Postgres volume across the rename; do not change that resource identity without a data migration.
