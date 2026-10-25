-- Authenticated ownership is explicit. Never claim legacy uploads by matching email addresses.
alter table bills add column if not exists requester_user_id uuid references users(id);
alter table bills add column if not exists requester_status_note text;
alter table bills add column if not exists verification_paused boolean not null default false;
alter table bills add column if not exists reported_balance_cents integer check (reported_balance_cents >= 0);
alter table bills add column if not exists balance_reported_at timestamptz;
create index if not exists bills_requester_idx on bills (requester_user_id, created_at desc)
    where requester_user_id is not null;
alter table bills drop constraint if exists bills_status_check;
alter table bills add constraint bills_status_check check (status in (
    'submitted', 'in_review', 'needs_info', 'rejected', 'queued', 'live',
    'funded', 'paid', 'expired', 'removed', 'withdrawn'
));

create table if not exists balance_reports (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    requester_user_id uuid not null references users(id),
    amount_cents integer not null check (amount_cents >= 0),
    note_ciphertext text not null,
    created_at timestamptz not null default clock_timestamp()
);
create index if not exists balance_reports_bill_idx on balance_reports (bill_id, created_at desc);
do $$ begin
    if not exists (select 1 from pg_trigger where tgname = 'balance_reports_immutable') then
        create trigger balance_reports_immutable before update or delete on balance_reports
            for each row execute function forbid_financial_mutation();
    end if;
end $$;

create or replace function prevent_self_contribution() returns trigger language plpgsql as $$
begin
    if exists (select 1 from bills where id = new.bill_id and requester_user_id = new.user_id) then
        raise check_violation using message = 'A requester cannot fund their own bill';
    end if;
    return new;
end $$;
create or replace function freeze_request_ownership() returns trigger language plpgsql as $$
begin
    if old.requester_user_id is not null and new.requester_user_id is distinct from old.requester_user_id then
        raise exception 'Request ownership cannot be transferred';
    end if;
    return new;
end $$;
do $$ begin
    if not exists (select 1 from pg_trigger where tgname = 'contributions_no_self_funding') then
        create trigger contributions_no_self_funding before insert or update of user_id, bill_id on contributions
            for each row execute function prevent_self_contribution();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'request_owner_immutable') then
        create trigger request_owner_immutable before update of requester_user_id on bills
            for each row execute function freeze_request_ownership();
    end if;
end $$;
