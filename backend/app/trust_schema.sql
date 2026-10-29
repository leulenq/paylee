-- Additive trust/payment migration. Existing bills are NOT implicitly verified.
create table if not exists staff (
    id uuid primary key default gen_random_uuid(),
    username text not null unique,
    password_hash text not null,
    roles text[] not null,
    totp_ciphertext text,
    last_totp_counter bigint not null default -1,
    session_version integer not null default 1,
    disabled_at timestamptz,
    created_at timestamptz not null default now(),
    check (roles <@ array['reviewer', 'payments', 'treasury', 'administrator']::text[])
);

create table if not exists documents (
    id uuid primary key,
    storage_key text not null unique,
    sha256 text not null check (length(sha256) = 64),
    content_type text not null,
    size_bytes integer not null check (size_bytes > 0),
    purpose text not null check (purpose in ('bill', 'evidence')),
    scan_status text not null default 'pending' check (scan_status in ('pending', 'clean', 'infected', 'failed')),
    scan_provider text,
    scanned_at timestamptz,
    extraction_status text not null default 'pending'
        check (extraction_status in ('pending', 'complete', 'manual', 'failed')),
    extraction_ciphertext text,
    created_at timestamptz not null default now()
);
create index if not exists documents_hash_idx on documents (sha256);
alter table bills add column if not exists document_id uuid references documents(id);
alter table bills add column if not exists consented_at timestamptz;

create table if not exists evidence (
    id uuid primary key default gen_random_uuid(),
    kind text not null check (kind in ('biller', 'debt', 'balance', 'funding', 'submission', 'settlement', 'posting', 'return', 'non_execution')),
    document_id uuid not null references documents(id),
    reference text not null check (length(reference) between 3 and 200),
    subject text not null,
    amount_cents integer check (amount_cents > 0),
    source text not null check (length(source) between 3 and 500),
    observed_at timestamptz not null,
    recorded_by uuid not null references staff(id),
    created_at timestamptz not null default now()
);

create table if not exists billers (
    id uuid primary key default gen_random_uuid(),
    legal_name text not null,
    category text not null check (category in ('utilities', 'internet', 'phone', 'rent', 'medical', 'other')),
    official_domain text not null,
    status text not null default 'pending' check (status in ('pending', 'approved', 'suspended')),
    refund_policy text not null check (refund_policy = 'return_to_origin'),
    evidence_id uuid not null references evidence(id),
    active_destination_id uuid,
    created_by uuid not null references staff(id),
    approved_by uuid references staff(id),
    approved_at timestamptz,
    created_at timestamptz not null default now(),
    check (approved_by is null or approved_by <> created_by)
);
create table if not exists payment_destinations (
    id uuid primary key default gen_random_uuid(),
    biller_id uuid not null references billers(id),
    version integer not null check (version > 0),
    rail text not null check (rail = 'manual_portal'),
    portal_url text not null,
    evidence_id uuid not null references evidence(id),
    created_by uuid not null references staff(id),
    created_at timestamptz not null default now(),
    unique (biller_id, version),
    unique (biller_id, id)
);
-- Composite FK prevents selecting another biller's payment destination.
do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'billers_destination_fk') then
        alter table billers add constraint billers_destination_fk
            foreign key (id, active_destination_id) references payment_destinations(biller_id, id);
    end if;
end $$;

create table if not exists bill_verifications (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    biller_id uuid not null references billers(id),
    destination_id uuid not null references payment_destinations(id),
    account_ciphertext text not null,
    account_fingerprint text not null,
    account_last4 text not null,
    invoice_fingerprint text not null,
    period_label text not null,
    verified_balance_cents integer not null check (verified_balance_cents > 0),
    evidence_id uuid not null references evidence(id),
    verified_by uuid not null references staff(id),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null
);
create table if not exists debt_claims (
    fingerprint text primary key,
    bill_id uuid not null references bills(id),
    created_at timestamptz not null default now()
);
alter table bills add column if not exists verification_id uuid references bill_verifications(id);
create table if not exists review_decisions (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    verification_id uuid not null references bill_verifications(id),
    actor_id uuid not null references staff(id),
    decision text not null check (decision in ('approved', 'rejected')),
    created_at timestamptz not null default now()
);

create table if not exists payment_orders (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    verification_id uuid not null references bill_verifications(id),
    destination_id uuid not null references payment_destinations(id),
    amount_cents integer not null check (amount_cents > 0),
    currency text not null default 'usd' check (currency = 'usd'),
    status text not null default 'prepared' check (status in (
        'prepared', 'released', 'executing', 'submitted', 'settled', 'applied', 'returned', 'canceled', 'exception'
    )),
    idempotency_key uuid not null unique default gen_random_uuid(),
    prepared_by uuid not null references staff(id),
    released_by uuid references staff(id),
    executed_by uuid references staff(id),
    execution_claimed_by uuid references staff(id),
    execution_claimed_at timestamptz,
    balance_evidence_id uuid references evidence(id),
    submission_evidence_id uuid references evidence(id),
    settlement_evidence_id uuid references evidence(id),
    posting_evidence_id uuid references evidence(id),
    return_evidence_id uuid references evidence(id),
    external_ref text unique,
    created_at timestamptz not null default now(),
    released_at timestamptz,
    submitted_at timestamptz,
    settled_at timestamptz,
    applied_at timestamptz,
    returned_at timestamptz,
    check (released_by is null or released_by <> prepared_by)
);
-- Unknown outcomes and applied payments continue blocking a second payment.
create unique index if not exists payment_orders_one_active_bill
    on payment_orders (bill_id) where status not in ('canceled', 'returned');

alter table contributions add column if not exists received_amount_cents integer;
alter table contributions add column if not exists funds_evidence_id uuid references evidence(id);
alter table contributions add column if not exists funds_reconciled_at timestamptz;
alter table contributions add column if not exists funds_reverification_required boolean not null default false;
alter table contributions add column if not exists refund_ref text;
alter table contributions add column if not exists refunded_at timestamptz;
create unique index if not exists contributions_unique_payment
    on contributions (provider, payment_ref) where payment_ref is not null;

-- Each immutable journal row has both sides: an unbalanced posting is unrepresentable.
create table if not exists ledger_journals (
    id uuid primary key default gen_random_uuid(),
    event_key text not null unique,
    bill_id uuid not null references bills(id),
    contribution_id uuid references contributions(id),
    payment_order_id uuid references payment_orders(id),
    debit_account text not null check (debit_account in ('collection_receivable', 'program_cash', 'restricted_payable', 'payout_in_transit')),
    credit_account text not null check (credit_account in ('collection_receivable', 'program_cash', 'restricted_payable', 'payout_in_transit')),
    amount_cents bigint not null check (amount_cents > 0),
    currency text not null default 'usd' check (currency = 'usd'),
    created_at timestamptz not null default now(),
    check (debit_account <> credit_account)
);
create or replace view ledger_lines as
    select id as journal_id, bill_id, debit_account as account, amount_cents as signed_cents from ledger_journals
    union all
    select id, bill_id, credit_account, -amount_cents from ledger_journals;

create table if not exists audit_events (
    id bigint generated always as identity primary key,
    actor_id uuid references staff(id),
    action text not null,
    object_id text not null,
    details jsonb not null default '{}',
    created_at timestamptz not null default now()
);
create table if not exists provider_events (
    id uuid primary key default gen_random_uuid(),
    provider text not null,
    external_id text not null,
    payload_sha256 text not null,
    event jsonb not null,
    processed_at timestamptz,
    created_at timestamptz not null default now(),
    unique (provider, external_id)
);
create table if not exists work_jobs (
    id uuid primary key default gen_random_uuid(),
    kind text not null check (kind in ('document', 'provider_event', 'refund')),
    object_id uuid not null,
    status text not null default 'pending' check (status in ('pending', 'running', 'done', 'failed')),
    attempts integer not null default 0,
    available_at timestamptz not null default now(),
    lease_until timestamptz,
    lease_token uuid,
    last_error text,
    created_at timestamptz not null default now(),
    unique (kind, object_id)
);
create table if not exists funding_disputes (
    external_id text primary key,
    contribution_id uuid not null references contributions(id),
    status text not null check (status in ('open', 'won', 'lost')),
    amount_cents integer not null check (amount_cents > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Forward-compatible additions for installations of the initial trust schema.
alter table evidence add column if not exists subject text not null default 'unbound-legacy';
alter table evidence add column if not exists amount_cents integer check (amount_cents > 0);
alter table payment_orders add column if not exists execution_claimed_by uuid references staff(id);
alter table payment_orders add column if not exists execution_claimed_at timestamptz;
alter table payment_orders drop constraint if exists payment_orders_status_check;
alter table payment_orders add constraint payment_orders_status_check check (status in (
    'prepared', 'released', 'executing', 'submitted', 'settled', 'applied', 'returned', 'canceled', 'exception'
));
alter table evidence drop constraint if exists evidence_kind_check;
-- The one list of evidence kinds; this file re-applies it on every migration, so add new kinds here.
alter table evidence add constraint evidence_kind_check check (kind in (
    'biller', 'debt', 'balance', 'funding', 'submission', 'settlement', 'posting', 'return', 'non_execution',
    'account_holder', 'identity', 'hardship'
));
alter table bill_verifications drop constraint if exists bill_verifications_invoice_fingerprint_key;
create unique index if not exists bill_verifications_bill_binding_idx on bill_verifications (bill_id, id);
create unique index if not exists bill_verifications_instruction_binding_idx on bill_verifications (bill_id, id, destination_id);
do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'bills_verification_binding_fk') then
        alter table bills add constraint bills_verification_binding_fk foreign key (id, verification_id)
            references bill_verifications (bill_id, id);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'verification_destination_binding_fk') then
        alter table bill_verifications add constraint verification_destination_binding_fk
            foreign key (biller_id, destination_id) references payment_destinations (biller_id, id);
    end if;
    if not exists (select 1 from pg_constraint where conname = 'payment_verification_binding_fk') then
        alter table payment_orders add constraint payment_verification_binding_fk
            foreign key (bill_id, verification_id, destination_id) references bill_verifications (bill_id, id, destination_id);
    end if;
end $$;
create index if not exists work_jobs_pending_idx on work_jobs (available_at) where status in ('pending', 'running');

create or replace function forbid_financial_mutation() returns trigger language plpgsql as $$
begin raise exception 'Immutable financial evidence: append a correction instead'; end $$;
do $$ declare t text; begin
    foreach t in array array['evidence', 'payment_destinations', 'bill_verifications', 'review_decisions', 'ledger_journals', 'audit_events'] loop
        if not exists (select 1 from pg_trigger where tgname = t || '_immutable') then
            execute format('create trigger %I before update or delete on %I for each row execute function forbid_financial_mutation()', t || '_immutable', t);
        end if;
    end loop;
end $$;

create or replace function freeze_payment_instruction() returns trigger language plpgsql as $$
begin
    if row(new.bill_id, new.verification_id, new.destination_id, new.amount_cents, new.currency, new.prepared_by, new.idempotency_key)
       is distinct from row(old.bill_id, old.verification_id, old.destination_id, old.amount_cents, old.currency, old.prepared_by, old.idempotency_key) then
        raise exception 'Payment instructions are immutable; cancel and prepare again';
    end if;
    return new;
end $$;

create or replace function freeze_document_provenance() returns trigger language plpgsql as $$
begin
    if row(new.storage_key, new.sha256, new.content_type, new.size_bytes, new.purpose)
       is distinct from row(old.storage_key, old.sha256, old.content_type, old.size_bytes, old.purpose) then
        raise exception 'Document provenance is immutable; upload a new artifact';
    end if;
    return new;
end $$;
do $$ begin
    if not exists (select 1 from pg_trigger where tgname = 'document_provenance_immutable') then
        create trigger document_provenance_immutable before update on documents
            for each row execute function freeze_document_provenance();
    end if;
end $$;
do $$ begin
    if not exists (select 1 from pg_trigger where tgname = 'payment_instruction_immutable') then
        create trigger payment_instruction_immutable before update on payment_orders
            for each row execute function freeze_payment_instruction();
    end if;
end $$;
