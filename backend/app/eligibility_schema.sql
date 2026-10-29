-- Additive person/household and hardship gates. Bills published before this migration are NOT
-- implicitly eligible: they stop taking gifts until a reviewer records eligibility and republishes.
-- Evidence kinds account_holder, identity, and hardship are added in trust_schema.sql.

-- The requester's own statement. Append-only: a correction is a new claim, and the latest one counts.
create table if not exists eligibility_claims (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    requester_user_id uuid references users(id),
    holder_name_ciphertext text not null,
    service_address_ciphertext text not null,
    household_fingerprint text not null,
    hardship_cause text not null check (hardship_cause in (
        'job_loss', 'reduced_income', 'medical', 'disability', 'benefits_gap', 'household_change', 'other'
    )),
    signature_ciphertext text not null,
    created_at timestamptz not null default clock_timestamp()
);
create index if not exists eligibility_claims_bill_idx on eligibility_claims (bill_id, created_at desc);

-- The biller's confirmation that the verified account belongs to the claimed holder at the claimed address.
create table if not exists account_holder_bindings (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    verification_id uuid not null references bill_verifications(id),
    claim_id uuid not null references eligibility_claims(id),
    evidence_id uuid not null references evidence(id),
    holder_name_ciphertext text not null,
    household_fingerprint text not null,
    recorded_by uuid not null references staff(id),
    created_at timestamptz not null default clock_timestamp()
);
create index if not exists account_holder_bindings_bill_idx on account_holder_bindings (bill_id, created_at desc);

-- Identity and person-level hardship checks are reusable across one requester account's bills until
-- they expire. Web submissions have no account, so their checks are scoped to the bill.
create table if not exists identity_checks (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    requester_user_id uuid references users(id),
    evidence_id uuid not null references evidence(id),
    legal_name_ciphertext text not null,
    person_fingerprint text not null,
    recorded_by uuid not null references staff(id),
    observed_at timestamptz not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp()
);
create index if not exists identity_checks_user_idx on identity_checks (requester_user_id, expires_at desc);
create index if not exists identity_checks_bill_idx on identity_checks (bill_id, expires_at desc);

create table if not exists hardship_checks (
    id uuid primary key default gen_random_uuid(),
    bill_id uuid not null references bills(id),
    requester_user_id uuid references users(id),
    method text not null check (method in ('biller_program', 'benefits', 'income', 'shock')),
    evidence_id uuid not null references evidence(id),
    -- Person-level methods must name the person so they can be bound to the account holder and identity.
    legal_name_ciphertext text,
    recorded_by uuid not null references staff(id),
    observed_at timestamptz not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default clock_timestamp(),
    check ((method = 'biller_program') = (legal_name_ciphertext is null))
);
create index if not exists hardship_checks_user_idx on hardship_checks (requester_user_id, expires_at desc);
create index if not exists hardship_checks_bill_idx on hardship_checks (bill_id, expires_at desc);

-- Frozen at publication. Every key the bill counts against is stored so caps cannot be split
-- across accounts, addresses, or people.
create table if not exists bill_eligibility (
    bill_id uuid primary key references bills(id),
    level text not null check (level in ('attested', 'identified', 'documented')),
    amount_cents integer not null check (amount_cents > 0),
    trailing_cents integer not null check (trailing_cents >= 0),
    claim_id uuid not null references eligibility_claims(id),
    binding_id uuid not null references account_holder_bindings(id),
    identity_check_id uuid references identity_checks(id),
    hardship_check_id uuid references hardship_checks(id),
    hardship_method text not null check (hardship_method in ('attestation', 'biller_program', 'benefits', 'income', 'shock')),
    household_fingerprint text not null,
    account_fingerprint text not null,
    person_fingerprint text,
    requester_user_id uuid references users(id),
    audit_status text not null default 'none' check (audit_status in ('none', 'selected', 'passed', 'failed', 'abandoned')),
    audit_hardship_check_id uuid references hardship_checks(id),
    audit_resolved_by uuid references staff(id),
    audit_resolved_at timestamptz,
    published_by uuid not null references staff(id),
    created_at timestamptz not null default clock_timestamp(),
    check ((hardship_method = 'attestation') = (hardship_check_id is null)),
    check (level = 'attested' or identity_check_id is not null),
    check (audit_status = 'none' or hardship_method = 'attestation')
);
create index if not exists bill_eligibility_household_idx on bill_eligibility (household_fingerprint, created_at);
create index if not exists bill_eligibility_account_idx on bill_eligibility (account_fingerprint, created_at);
create index if not exists bill_eligibility_person_idx on bill_eligibility (person_fingerprint, created_at) where person_fingerprint is not null;
create index if not exists bill_eligibility_user_idx on bill_eligibility (requester_user_id, created_at) where requester_user_id is not null;

-- Failed audits bar every key on the bill. Abandoning an audit (withdrawing) bars attestation only.
create table if not exists eligibility_bars (
    id uuid primary key default gen_random_uuid(),
    fingerprint text not null,
    scope text not null check (scope in ('all', 'attestation')),
    bill_id uuid not null references bills(id),
    barred_until timestamptz not null,
    created_at timestamptz not null default clock_timestamp()
);
create index if not exists eligibility_bars_fingerprint_idx on eligibility_bars (fingerprint, barred_until desc);

do $$ declare t text; begin
    foreach t in array array['eligibility_claims', 'account_holder_bindings', 'identity_checks', 'hardship_checks', 'eligibility_bars'] loop
        if not exists (select 1 from pg_trigger where tgname = t || '_immutable') then
            execute format('create trigger %I before update or delete on %I for each row execute function forbid_financial_mutation()', t || '_immutable', t);
        end if;
    end loop;
end $$;

-- Only the audit outcome may change after publication, and only once.
create or replace function freeze_bill_eligibility() returns trigger language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'Bill eligibility is permanent';
    end if;
    if row(new.bill_id, new.level, new.amount_cents, new.trailing_cents, new.claim_id, new.binding_id, new.identity_check_id,
           new.hardship_check_id, new.hardship_method, new.household_fingerprint, new.account_fingerprint,
           new.person_fingerprint, new.requester_user_id, new.published_by, new.created_at)
       is distinct from row(old.bill_id, old.level, old.amount_cents, old.trailing_cents, old.claim_id, old.binding_id, old.identity_check_id,
           old.hardship_check_id, old.hardship_method, old.household_fingerprint, old.account_fingerprint,
           old.person_fingerprint, old.requester_user_id, old.published_by, old.created_at) then
        raise exception 'Bill eligibility is frozen at publication';
    end if;
    if old.audit_status <> 'selected' and new.audit_status is distinct from old.audit_status then
        raise exception 'Only a pending audit can be resolved';
    end if;
    return new;
end $$;
do $$ begin
    if not exists (select 1 from pg_trigger where tgname = 'bill_eligibility_frozen') then
        create trigger bill_eligibility_frozen before update or delete on bill_eligibility
            for each row execute function freeze_bill_eligibility();
    end if;
end $$;
