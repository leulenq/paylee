-- Idempotent schema, applied at startup. Money is always integer cents.

create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    -- "apple:<sub>" from Sign in with Apple, or "dev:<device>" for local development.
    auth_subject text unique,
    -- Public pseudonym shown on leaderboards. Real names are never collected from givers.
    handle text not null unique,
    created_at timestamptz not null default now(),
    deleted_at timestamptz
);

create table if not exists bills (
    id uuid primary key default gen_random_uuid(),
    status text not null default 'submitted' check (status in (
        'submitted',  -- waiting for review
        'rejected',
        'queued',     -- approved, waiting for a live slot
        'live',       -- in the feed, accepting contributions
        'funded',     -- fully raised, waiting to be paid to the biller
        'paid',       -- paid to the biller
        'expired',    -- past due date plus grace period
        'removed'     -- taken down after review or reports
    )),
    category text not null check (category in ('rent', 'utilities', 'medical', 'phone', 'internet', 'other')),
    biller_name text not null,
    requester_first_name text not null,
    requester_email text not null,
    city text not null,
    story text not null,
    -- What the requester typed. amount_due_cents is what a reviewer confirmed on the bill itself.
    claimed_amount_cents integer not null check (claimed_amount_cents > 0),
    amount_due_cents integer check (amount_due_cents > 0),
    amount_raised_cents integer not null default 0 check (amount_raised_cents >= 0),
    due_date date not null,
    upload_path text not null,
    upload_content_type text not null,
    times_shown integer not null default 0,
    review_note text,
    payout_confirmation text,
    closer_user_id uuid references users (id),
    created_at timestamptz not null default now(),
    live_at timestamptz,
    funded_at timestamptz,
    paid_at timestamptz,
    -- Backstop for the balance cap: the database itself refuses an overpaid bill.
    check (amount_due_cents is null or amount_raised_cents <= amount_due_cents)
);

create index if not exists bills_status_idx on bills (status);

create table if not exists contributions (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills (id),
    user_id uuid not null references users (id),
    amount_cents integer not null check (amount_cents > 0),
    status text not null default 'pending' check (status in (
        'pending',         -- amount reserved, payment not finished
        'succeeded',       -- counted toward the bill
        'canceled',        -- payment abandoned or failed to start
        'refund_pending',  -- money arrived but the bill couldn't take it; must go back
        'refunded'
    )),
    provider text not null,
    provider_ref text,   -- checkout session id
    payment_ref text,    -- payment id, used for refunds
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    completed_at timestamptz
);

create index if not exists contributions_bill_status_idx on contributions (bill_id, status);
create index if not exists contributions_user_idx on contributions (user_id, created_at);
create unique index if not exists contributions_provider_ref_idx
    on contributions (provider_ref) where provider_ref is not null;

-- Left swipes. Only used to rest a bill out of one giver's feed; never shown to requesters.
create table if not exists passes (
    user_id uuid not null references users (id),
    bill_id uuid not null references bills (id),
    created_at timestamptz not null default now(),
    primary key (user_id, bill_id)
);

-- Outbox for requester emails. Rows are written in the same transaction as the change they describe,
-- so an email is never lost or sent for a change that rolled back.
create table if not exists emails (
    id uuid primary key default gen_random_uuid(),
    to_address text not null,
    subject text not null,
    body text not null,
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    attempts integer not null default 0,
    last_error text
);

create index if not exists emails_unsent_idx on emails (created_at) where sent_at is null;

-- iOS devices that accept push notifications. A token moves to whoever signed in on that device last.
create table if not exists device_tokens (
    token text primary key,
    user_id uuid not null references users (id),
    environment text not null default 'sandbox' check (environment in ('sandbox', 'production')),
    updated_at timestamptz not null default now()
);

-- Outbox for giver push notifications, written in the same transaction as the change they describe.
create table if not exists pushes (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users (id),
    title text not null,
    body text not null,
    bill_id uuid references bills (id),
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    attempts integer not null default 0,
    last_error text
);

create index if not exists pushes_unsent_idx on pushes (created_at) where sent_at is null;

-- Additive changes to existing tables. Keep these idempotent.
alter table users add column if not exists activity_seen_at timestamptz;

-- Two-letter US state, for "near me" filtering. Backfilled from the "City, ST" display string.
alter table bills add column if not exists state text;
update bills set state = upper(trim(split_part(city, ',', 2)))
where state is null and city like '%,%' and length(trim(split_part(city, ',', 2))) = 2;
create index if not exists bills_live_state_idx on bills (state) where status = 'live';

-- Which side of the app this account is using. Null until they choose.
alter table users add column if not exists role text;
alter table users drop constraint if exists users_role_check;
alter table users add constraint users_role_check check (role is null or role in ('payer', 'requestor'));

alter table users add column if not exists show_on_leaderboards boolean not null default true;
alter table users add column if not exists show_city_on_share_cards boolean not null default true;
alter table users add column if not exists notify_payment_facts boolean not null default true;
alter table users add column if not exists notify_open_bills boolean not null default true;

-- A rhythm the payer chose. Null cadence means they didn't set one. The monthly limit is a
-- ceiling they picked; unused money is never collected or owed.
alter table users add column if not exists practice_cadence text;
alter table users add column if not exists practice_bills_per_period integer;
alter table users add column if not exists practice_monthly_limit_cents integer;
alter table users add column if not exists practice_start_day text;
alter table users drop constraint if exists users_practice_cadence_check;
alter table users add constraint users_practice_cadence_check
    check (practice_cadence is null or practice_cadence in ('weekly', 'monthly'));
alter table users drop constraint if exists users_practice_count_check;
alter table users add constraint users_practice_count_check
    check (practice_bills_per_period is null or practice_bills_per_period between 1 and 4);
alter table users drop constraint if exists users_practice_limit_check;
alter table users add constraint users_practice_limit_check
    check (practice_monthly_limit_cents is null or practice_monthly_limit_cents >= 500);
alter table users drop constraint if exists users_practice_start_check;
alter table users add constraint users_practice_start_check
    check (practice_start_day is null or practice_start_day in ('sunday', 'payday', 'first_of_month'));

-- One factual nudge per bill a payer already started: remainder, company, real due date.
create table if not exists open_bill_notices (
    user_id uuid not null references users (id),
    bill_id uuid not null references bills (id),
    created_at timestamptz not null default now(),
    primary key (user_id, bill_id)
);

create table if not exists reports (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills (id),
    user_id uuid not null references users (id),
    reason text not null,
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    unique (bill_id, user_id)
);
