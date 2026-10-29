"""Person/household and hardship gates, bound to the verified debt and graded by cumulative amount.

The four gates (creditor, debt, person/household, hardship) are independent, so passing one never
implies another. Three bindings join them: the biller confirms the requester's claimed account holder
and service address; identity and person-level hardship evidence must name that same holder. Without
the bindings, a real hardship and a real debt could belong to different people.

Assurance scales with what the bill's household, account, requester, and person received in the
trailing year (any shared key counts, so new accounts or reformatted addresses cannot split a cap):
- attested: up to the attested limit. Biller-confirmed holder plus a signed hardship attestation,
  randomly audited before payment. A failed audit bars every key on the bill.
- identified: identity evidence plus third-party hardship (the biller's own program counts).
- documented: identity plus person-level hardship (benefits, income, or a documented shock).
"""

import re
import secrets
import unicodedata
from datetime import date, timedelta

from . import staff, trust, vault
from .config import get_settings

CAUSES = ("job_loss", "reduced_income", "medical", "disability", "benefits_gap", "household_change", "other")
PERSON_METHODS = ("benefits", "income", "shock")
METHODS = ("biller_program", *PERSON_METHODS)
# Third-party hardship each level accepts. Only the attested level may fall back to a signed attestation.
LEVELS = {"attested": METHODS, "identified": METHODS, "documented": PERSON_METHODS}
CLAIMABLE = ("submitted", "in_review", "needs_info")
# Statuses whose amount still counts toward the trailing-year total. Refunded bills count nothing.
COUNTED = ("queued", "live", "funded", "paid", "in_review", "needs_info", "submitted")

_NAME_NOISE = {"jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr"}
_ADDRESS_WORDS = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln", "boulevard": "blvd",
    "court": "ct", "place": "pl", "circle": "cir", "parkway": "pkwy", "highway": "hwy", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne", "northwest": "nw",
    "southeast": "se", "southwest": "sw",
}
# Unit markers vary ("Apt 2", "#2", "Unit 2"); the number alone identifies the unit.
_UNIT_WORDS = {"apt", "apartment", "unit", "suite", "ste", "no", "number", "fl", "floor", "rm", "room", "lot"}


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()


def name_tokens(name: str) -> frozenset:
    # Initials and suffixes are dropped: billers abbreviate middle names inconsistently.
    return frozenset(t for t in re.split(r"[^a-z]+", _ascii(name)) if len(t) > 1 and t not in _NAME_NOISE)


def names_match(a: str, b: str) -> bool:
    """Same person under reordering ("GARCIA MARIA L") or an omitted middle name, never a shared surname alone."""
    left, right = name_tokens(a), name_tokens(b)
    shared = left & right
    return len(shared) >= 2 and shared in (left, right)


def household_fingerprint(street: str, zip_code: str) -> str:
    tokens = [_ADDRESS_WORDS.get(t, t) for t in re.split(r"[^a-z0-9]+", _ascii(street.replace("#", " "))) if t]
    zip5 = re.sub(r"\D", "", zip_code)[:5]
    if len(zip5) != 5 or not tokens:
        raise trust.TrustError("A service street address and five-digit ZIP code are required")
    return vault.fingerprint("household", " ".join(t for t in tokens if t not in _UNIT_WORDS), zip5)


def person_fingerprint(legal_name: str, date_of_birth: date) -> str:
    # Date of birth separates people who share a name; it is never stored in readable form.
    return vault.fingerprint("person", " ".join(sorted(name_tokens(legal_name))), date_of_birth.isoformat())


def validate_claim(values: dict) -> dict:
    errors = {}
    if not 3 <= len(values.get("account_holder_name", "")) <= 120 or len(name_tokens(values.get("account_holder_name", ""))) < 2:
        errors["account_holder_name"] = "Enter the full name on the account, as the company has it."
    if not 3 <= len(values.get("service_street", "")) <= 120:
        errors["service_street"] = "Enter the street address where the service is."
    if not re.fullmatch(r"[0-9]{5}(-?[0-9]{4})?", values.get("service_zip", "")):
        errors["service_zip"] = "Enter a five-digit ZIP code."
    if values.get("hardship_cause") not in CAUSES:
        errors["hardship_cause"] = "Choose what put you behind."
    if "account_holder_name" not in errors and not names_match(values.get("signature", ""), values["account_holder_name"]):
        # Household members other than the holder need the holder's co-consent, which is not built yet.
        errors["signature"] = "Type your full name to sign. The account must be in your name."
    if not values.get("attest"):
        errors["attest"] = "Confirm the statement to continue."
    return errors


def submit_claim(conn, bill_id, user_id, values: dict):
    bill = conn.execute("select status from bills where id = %s for update", (bill_id,)).fetchone()
    if not bill or bill["status"] not in CLAIMABLE:
        raise trust.TrustError("Eligibility details can only change before a bill is published")
    errors = validate_claim(values)
    if errors:
        raise trust.TrustError(next(iter(errors.values())))
    address = f'{values["service_street"].strip()}\n{values["service_zip"].strip()}'
    row = conn.execute(
        """insert into eligibility_claims (bill_id, requester_user_id, holder_name_ciphertext, service_address_ciphertext,
                                           household_fingerprint, hardship_cause, signature_ciphertext)
           values (%s, %s, %s, %s, %s, %s, %s) returning id, created_at""",
        (bill_id, user_id, vault.encrypt(values["account_holder_name"].strip()), vault.encrypt(address),
         household_fingerprint(values["service_street"], values["service_zip"]), values["hardship_cause"],
         vault.encrypt(values["signature"].strip())),
    ).fetchone()
    trust.audit(conn, None, "eligibility.claimed", bill_id, claim_id=str(row["id"]))
    return row


def _latest_claim(conn, bill_id):
    return conn.execute("select * from eligibility_claims where bill_id = %s order by created_at desc limit 1",
                        (bill_id,)).fetchone()


def _requester_subject(bill):
    # Reusable checks attach to the authenticated requester; web submissions have only the bill.
    return str(bill["requester_user_id"] or bill["id"])


def record_account_holder(conn, actor, bill_id, *, evidence_id, holder_name, service_street, service_zip):
    """The biller (not the requester) states who holds the verified account and where service is."""
    staff.require_role(actor, "reviewer")
    bill = conn.execute("select * from bills where id = %s for update", (bill_id,)).fetchone()
    if not bill or not bill["verification_id"]:
        raise trust.TrustError("Verify the debt with the biller before binding the account holder")
    claim = _latest_claim(conn, bill_id)
    if not claim:
        raise trust.TrustError("The requester has not stated the account holder and service address")
    trust.evidence(conn, evidence_id, "account_holder", bill_id, max_age=timedelta(hours=get_settings().verification_hours))
    # Mismatches are refused, not recorded as passes: the requester must correct the claim or the bill is rejected.
    if not names_match(holder_name, vault.decrypt(claim["holder_name_ciphertext"])):
        raise trust.TrustError("The biller's account holder does not match the requester's claim")
    household = household_fingerprint(service_street, service_zip)
    if household != claim["household_fingerprint"]:
        raise trust.TrustError("The biller's service address does not match the requester's claim")
    row = conn.execute(
        """insert into account_holder_bindings (bill_id, verification_id, claim_id, evidence_id, holder_name_ciphertext,
                                                household_fingerprint, recorded_by)
           values (%s, %s, %s, %s, %s, %s, %s) returning *""",
        (bill_id, bill["verification_id"], claim["id"], evidence_id, vault.encrypt(holder_name.strip()), household, actor["id"]),
    ).fetchone()
    trust.audit(conn, actor, "eligibility.account_holder_bound", bill_id, binding_id=str(row["id"]))
    return row


def record_identity(conn, actor, bill_id, *, evidence_id, legal_name, date_of_birth):
    staff.require_role(actor, "reviewer")
    bill = conn.execute("select * from bills where id = %s", (bill_id,)).fetchone()
    if not bill:
        raise trust.TrustError("Bill not found")
    if len(name_tokens(legal_name)) < 2 or not date(1900, 1, 1) < date_of_birth < date.today() - timedelta(days=365 * 18):
        raise trust.TrustError("Record the full legal name and an adult date of birth from the identity check")
    proof = trust.evidence(conn, evidence_id, "identity", _requester_subject(bill))
    row = conn.execute(
        """insert into identity_checks (bill_id, requester_user_id, evidence_id, legal_name_ciphertext, person_fingerprint,
                                        recorded_by, observed_at, expires_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (bill_id, bill["requester_user_id"], evidence_id, vault.encrypt(legal_name.strip()),
         person_fingerprint(legal_name, date_of_birth), actor["id"], proof["observed_at"],
         proof["observed_at"] + timedelta(days=get_settings().identity_check_days)),
    ).fetchone()
    trust.audit(conn, actor, "eligibility.identity_recorded", bill_id, identity_check_id=str(row["id"]))
    return row


def record_hardship(conn, actor, bill_id, *, method, evidence_id, legal_name=None):
    """biller_program: the biller confirms an account-level hardship status (low-income rate, arrearage plan).
    Person-level methods (benefits, income, shock) must name the person they describe."""
    staff.require_role(actor, "reviewer")
    if method not in METHODS:
        raise trust.TrustError("Unknown hardship method")
    bill = conn.execute("select * from bills where id = %s", (bill_id,)).fetchone()
    if not bill:
        raise trust.TrustError("Bill not found")
    if method == "biller_program":
        if legal_name:
            raise trust.TrustError("Biller program evidence describes the account, not a named person")
        proof = trust.evidence(conn, evidence_id, "hardship", bill_id)
    else:
        if not legal_name or len(name_tokens(legal_name)) < 2:
            raise trust.TrustError("Record the full name shown on the hardship evidence")
        proof = trust.evidence(conn, evidence_id, "hardship", _requester_subject(bill))
    row = conn.execute(
        """insert into hardship_checks (bill_id, requester_user_id, method, evidence_id, legal_name_ciphertext,
                                        recorded_by, observed_at, expires_at)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (bill_id, None if method == "biller_program" else bill["requester_user_id"], method, evidence_id,
         vault.encrypt(legal_name.strip()) if legal_name else None, actor["id"], proof["observed_at"],
         proof["observed_at"] + timedelta(days=get_settings().hardship_check_days)),
    ).fetchone()
    trust.audit(conn, actor, "eligibility.hardship_recorded", bill_id, hardship_check_id=str(row["id"]), method=method)
    return row


def _keys(household, account, user_id, person):
    keys = [f"household:{household}", f"account:{account}"]
    if user_id:
        keys.append(f"user:{user_id}")
    if person:
        keys.append(f"person:{person}")
    return keys


def _bar(conn, keys, *, include_attestation=False):
    scopes = ["all", "attestation"] if include_attestation else ["all"]
    return conn.execute("""select scope from eligibility_bars where fingerprint = any(%s) and scope = any(%s)
                           and barred_until > now() order by scope limit 1""", (keys, scopes)).fetchone()


def _usable_identity(conn, bill, holder):
    rows = conn.execute(
        """select * from identity_checks where expires_at > now()
             and (bill_id = %s or (requester_user_id is not null and requester_user_id = %s))
           order by observed_at desc""", (bill["id"], bill["requester_user_id"])).fetchall()
    return next((r for r in rows if names_match(vault.decrypt(r["legal_name_ciphertext"]), holder)), None)


def _usable_hardship(conn, bill, holder, allowed, check_id=None):
    rows = conn.execute(
        """select * from hardship_checks where expires_at > now() and method = any(%s)
             and (bill_id = %s or (method <> 'biller_program' and requester_user_id is not null and requester_user_id = %s))
             and (%s::uuid is null or id = %s::uuid)
           order by observed_at desc""",
        (list(allowed), bill["id"], bill["requester_user_id"], check_id, check_id)).fetchall()
    for row in rows:
        if row["method"] == "biller_program" or names_match(vault.decrypt(row["legal_name_ciphertext"]), holder):
            return row
    return None


def assess(conn, bill_id, amount_cents=None, *, lock=False):
    """Pure decision about what the bill still needs. Never selects an audit or writes anything."""
    settings = get_settings()
    bill = conn.execute("select * from bills where id = %s", (bill_id,)).fetchone()
    amount = amount_cents or bill["amount_due_cents"] or bill["claimed_amount_cents"]
    result = {"bill_id": bill_id, "amount_cents": amount, "missing": [], "level": None}
    claim = _latest_claim(conn, bill_id)
    if not claim:
        result["missing"].append("claim")
        return result
    verification = conn.execute("select * from bill_verifications where id = %s", (bill["verification_id"],)).fetchone() \
        if bill["verification_id"] else None
    binding = conn.execute(
        """select * from account_holder_bindings where bill_id = %s and verification_id = %s and claim_id = %s
           order by created_at desc limit 1""",
        (bill_id, bill["verification_id"], claim["id"])).fetchone() if verification else None
    if not binding:
        result["missing"].append("account_holder")
        return result
    holder = vault.decrypt(binding["holder_name_ciphertext"])
    identity = _usable_identity(conn, bill, holder)
    person = identity["person_fingerprint"] if identity else None
    keys = _keys(binding["household_fingerprint"], verification["account_fingerprint"], bill["requester_user_id"], person)
    if lock:
        # Serialize publications that share any key so two concurrent approvals cannot both fit under a cap.
        for key in sorted(keys):
            conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 7201001))", (key,))
    if _bar(conn, keys):
        result["missing"].append("barred")
        return result
    trailing = conn.execute(
        """select coalesce(sum(case when b.status = 'expired' then least(b.amount_raised_cents, e.amount_cents)
                                    when b.status = any(%(counted)s) then e.amount_cents else 0 end), 0)::int as n
           from bill_eligibility e join bills b on b.id = e.bill_id
           where e.bill_id <> %(bill)s and e.created_at > now() - interval '365 days'
             and (e.household_fingerprint = %(household)s or e.account_fingerprint = %(account)s
                  or e.requester_user_id = %(user)s or e.person_fingerprint = %(person)s)""",
        {"counted": list(COUNTED), "bill": bill_id, "household": binding["household_fingerprint"],
         "account": verification["account_fingerprint"], "user": bill["requester_user_id"], "person": person},
    ).fetchone()["n"]
    total = trailing + amount
    if total > settings.eligibility_annual_limit_cents:
        result["missing"].append("annual_limit")
        return result
    moved = bill["requester_user_id"] and conn.execute(
        """select 1 from bill_eligibility where requester_user_id = %s and household_fingerprint <> %s
           and created_at > now() - interval '365 days' limit 1""",
        (bill["requester_user_id"], binding["household_fingerprint"])).fetchone()
    if total <= settings.eligibility_attested_limit_cents and not moved:
        level = "attested"
    elif total <= settings.eligibility_identified_limit_cents:
        level = "identified"
    else:
        level = "documented"
    if level != "attested" and not identity:
        result["missing"].append("identity")
    hardship = _usable_hardship(conn, bill, holder, LEVELS[level])
    method = hardship["method"] if hardship else None
    if not hardship:
        if level == "attested" and not _bar(conn, keys, include_attestation=True):
            method = "attestation"
        else:
            result["missing"].append("hardship")
    result.update(level=level, trailing_cents=trailing, claim=claim, binding=binding, verification=verification,
                  identity=identity, hardship=hardship, hardship_method=method, keys=keys, person=person)
    return result


MISSING_MESSAGES = {
    "claim": "The requester must state the account holder, service address, and hardship",
    "account_holder": "Record the biller's confirmation of the account holder for the current verification",
    "barred": "This household, account, or person is barred after a failed hardship audit",
    "annual_limit": "This household has reached the yearly limit",
    "identity": "An identity check that matches the account holder is required at this amount",
    "hardship": "Third-party hardship evidence that matches the account holder is required at this amount",
}


def publish(conn, actor, bill_id, amount_cents):
    """Freezes the bill's eligibility at publication. Called inside bills.approve, under the bill lock."""
    existing = conn.execute("select * from bill_eligibility where bill_id = %s for update", (bill_id,)).fetchone()
    if existing:
        current = conn.execute("""select v.account_fingerprint from bills b join bill_verifications v on v.id = b.verification_id
                                  where b.id = %s""", (bill_id,)).fetchone()
        if existing["audit_status"] in ("failed", "abandoned"):
            raise trust.TrustError("This bill failed or abandoned its hardship audit")
        if amount_cents > existing["amount_cents"] or current["account_fingerprint"] != existing["account_fingerprint"]:
            raise trust.TrustError("A higher balance or different account needs a new request")
        if _bar(conn, _keys(existing["household_fingerprint"], existing["account_fingerprint"],
                            existing["requester_user_id"], existing["person_fingerprint"])):
            raise trust.TrustError(MISSING_MESSAGES["barred"])
        return existing
    result = assess(conn, bill_id, amount_cents, lock=True)
    if result["missing"]:
        raise trust.TrustError("; ".join(MISSING_MESSAGES[m] for m in result["missing"]))
    audit = "none"
    if result["hardship_method"] == "attestation":
        audit = "selected" if secrets.randbelow(10_000) < round(get_settings().eligibility_audit_rate * 10_000) else "none"
    bill = conn.execute("select requester_user_id from bills where id = %s", (bill_id,)).fetchone()
    row = conn.execute(
        """insert into bill_eligibility (bill_id, level, amount_cents, trailing_cents, claim_id, binding_id, identity_check_id,
               hardship_check_id, hardship_method, household_fingerprint, account_fingerprint, person_fingerprint,
               requester_user_id, audit_status, published_by)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *""",
        (bill_id, result["level"], amount_cents, result["trailing_cents"], result["claim"]["id"], result["binding"]["id"],
         result["identity"]["id"] if result["identity"] else None, result["hardship"]["id"] if result["hardship"] else None,
         result["hardship_method"], result["binding"]["household_fingerprint"], result["verification"]["account_fingerprint"],
         result["person"], bill["requester_user_id"], audit, actor["id"]),
    ).fetchone()
    # Audit selection is recorded but never exposed to givers or in a way that predicts selection.
    trust.audit(conn, actor, "eligibility.published", bill_id, level=row["level"], method=row["hardship_method"],
                audit=audit)
    return row


def check(conn, bill_id, *, payout=False):
    row = conn.execute("select * from bill_eligibility where bill_id = %s", (bill_id,)).fetchone()
    if not row:
        raise trust.TrustError("Bill needs person, household, and hardship eligibility before money can move")
    if row["audit_status"] in ("failed", "abandoned"):
        raise trust.TrustError("Bill failed its hardship audit")
    if payout and row["audit_status"] == "selected":
        raise trust.TrustError("Resolve the bill's hardship audit before payment")
    if _bar(conn, _keys(row["household_fingerprint"], row["account_fingerprint"], row["requester_user_id"], row["person_fingerprint"])):
        raise trust.TrustError(MISSING_MESSAGES["barred"])
    return row


def resolve_audit(conn, actor, bill_id, *, outcome, hardship_check_id=None):
    from . import bills

    staff.require_role(actor, "reviewer")
    conn.execute("select id from bills where id = %s for update", (bill_id,))
    row = conn.execute("select * from bill_eligibility where bill_id = %s for update", (bill_id,)).fetchone()
    if not row or row["audit_status"] != "selected":
        raise trust.TrustError("This bill has no pending hardship audit")
    if actor["id"] == row["published_by"]:
        raise trust.TrustError("A different reviewer from the publisher must resolve the audit")
    if outcome == "passed":
        binding = conn.execute("select holder_name_ciphertext from account_holder_bindings where id = %s",
                               (row["binding_id"],)).fetchone()
        bill = conn.execute("select * from bills where id = %s", (bill_id,)).fetchone()
        hardship = _usable_hardship(conn, bill, vault.decrypt(binding["holder_name_ciphertext"]), METHODS,
                                    check_id=hardship_check_id)
        if not hardship:
            raise trust.TrustError("Passing an audit requires current hardship evidence that matches the account holder")
        conn.execute("""update bill_eligibility set audit_status = 'passed', audit_hardship_check_id = %s,
                        audit_resolved_by = %s, audit_resolved_at = now() where bill_id = %s""",
                     (hardship_check_id, actor["id"], bill_id))
        conn.execute("update bills set requester_status_note = null where id = %s", (bill_id,))
    elif outcome == "failed":
        conn.execute("""update bill_eligibility set audit_status = 'failed', audit_resolved_by = %s,
                        audit_resolved_at = now() where bill_id = %s""", (actor["id"], bill_id))
        _bar_keys(conn, row, "all")
        bills.remove(conn, bill_id, "Hardship audit failed", get_settings().max_live_bills)
    else:
        raise trust.TrustError("Unknown audit outcome")
    trust.audit(conn, actor, "eligibility.audit_resolved", bill_id, outcome=outcome)


def _bar_keys(conn, row, scope):
    until = conn.execute("select now() + make_interval(days => %s) as t", (get_settings().eligibility_bar_days,)).fetchone()["t"]
    for key in _keys(row["household_fingerprint"], row["account_fingerprint"], row["requester_user_id"], row["person_fingerprint"]):
        conn.execute("insert into eligibility_bars (fingerprint, scope, bill_id, barred_until) values (%s, %s, %s, %s)",
                     (key, scope, row["bill_id"], until))


def on_withdraw(conn, bill_id):
    """Withdrawing during an audit would let a dishonest attester walk away only when caught."""
    row = conn.execute("select * from bill_eligibility where bill_id = %s for update", (bill_id,)).fetchone()
    if row and row["audit_status"] == "selected":
        conn.execute("update bill_eligibility set audit_status = 'abandoned', audit_resolved_at = now() where bill_id = %s",
                     (bill_id,))
        _bar_keys(conn, row, "attestation")
        trust.audit(conn, None, "eligibility.audit_abandoned", bill_id)


REQUESTER_STEPS = {
    "claim": "Tell us whose name the account is in, the service address, and what put you behind.",
    "account_holder": "We're confirming the account holder with the company. Nothing is needed from you.",
    "identity": "Verify your identity. This takes about two minutes and covers your requests for a year.",
    "hardship": "Share one proof of hardship, such as a benefits letter or a job-loss notice.",
    "annual_limit": "Your household has reached Paylee's yearly limit. You can request again later.",
    "barred": "This household can't post new requests right now.",
}


def requester_view(conn, user_id, bill_id):
    """What the requester still needs to do. Never reveals fingerprints, other bills, or audit odds per bill."""
    bill = conn.execute("select id, status, verification_id from bills where id = %s and requester_user_id = %s",
                        (bill_id, user_id)).fetchone()
    if not bill:
        return None
    published = conn.execute("select level, audit_status from bill_eligibility where bill_id = %s", (bill_id,)).fetchone()
    claim = _latest_claim(conn, bill_id)
    view = {"claim_submitted": claim is not None, "hardship_cause": claim["hardship_cause"] if claim else None,
            "published": published is not None, "steps": []}
    if published:
        if published["audit_status"] == "selected":
            view["steps"].append({"code": "hardship", "message": "Your request was picked for a routine check. "
                                  "Share one proof of hardship before Paylee pays the company."})
        return view
    missing = assess(conn, bill_id)["missing"]
    view["steps"] = [{"code": code, "message": REQUESTER_STEPS[code]} for code in missing]
    return view


def ops_view(conn, bill_id):
    published = conn.execute("""select level, amount_cents, trailing_cents, hardship_method, audit_status, created_at
                                from bill_eligibility where bill_id = %s""", (bill_id,)).fetchone()
    if published:
        return {"published": published}
    result = assess(conn, bill_id)
    return {"missing": result["missing"], "level": result["level"], "trailing_cents": result.get("trailing_cents"),
            "hardship_method": result.get("hardship_method")}
