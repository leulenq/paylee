-- Run as database owner AFTER migrations:
-- psql "$MIGRATION_DATABASE_URL" -v runtime_role=paylee_app -f runtime-grants.sql
-- Provision the LOGIN role/password through your database's secret-management process.
-- The runtime role must NOT own tables, inherit an owner role, or have superuser/CREATEROLE.
revoke create on schema public from public;
grant usage on schema public to :"runtime_role";
grant select, insert, update on users, bills, contributions, passes, emails, device_tokens,
    pushes, reports, documents, billers, payment_orders, provider_events, work_jobs, funding_disputes to :"runtime_role";
grant delete on passes, device_tokens to :"runtime_role";
grant select, insert on evidence, payment_destinations, bill_verifications, review_decisions,
    debt_claims, ledger_journals, audit_events to :"runtime_role";
grant select on ledger_lines, staff to :"runtime_role";
grant update (last_totp_counter, session_version) on staff to :"runtime_role";
grant usage, select on sequence audit_events_id_seq to :"runtime_role";
